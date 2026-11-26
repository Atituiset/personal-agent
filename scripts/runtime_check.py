#!/usr/bin/env python3
"""Check runtime budgets and reliability invariants against an isolated dev instance.

    python scripts/runtime_check.py                 # use the running instance (make dev-up)
    python scripts/runtime_check.py --up            # start a throwaway instance, check, tear it down
    python scripts/runtime_check.py --skip-system-checks

Steps
  1. probe     submit N demo.summarize jobs over HTTP, wait until all are terminal, then measure from Postgres:
               submit latency (client side), enqueue -> first lease, enqueue -> ACK, dead / retried jobs.
  2. logs      every probe job must have the required structured events (leased, acked) in logs/worker.jsonl.
  3. metrics   `personal-agent metrics` must answer against the instance (the CLI surface agents use).
  4. system    the 7 system checks from evals/tasks/system.jsonl (DLQ, fatal errors, effect de-dup, kill -9 before
               and after persist, duplicate enqueue) run via evals.run_evals against the instance's eval database.

Budgets come from scripts/runtime_budgets.json. Results go to var/dev/<name>/runtime-check.{json,md}
(and to $GITHUB_STEP_SUMMARY in CI). Exit code 1 if any budget is exceeded or any check fails.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PY = sys.executable

import dev_instance  # noqa: E402  (same directory)


def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def r1(x):
    return None if x is None else round(x, 1)


def probe(info: dict, budgets: dict, run_id: str) -> dict:
    import httpx
    import psycopg
    n = budgets["probe_jobs"]
    submit_ms, ids = [], []
    with httpx.Client(base_url=info["api_url"], timeout=10) as c:
        for i in range(n):
            body = {"task_type": "demo.summarize", "idempotency_key": f"{run_id}-{i}",
                    "payload": {"text": f"运行时检查第 {i} 条。第二句。第三句。第四句。"}}
            t0 = time.perf_counter()
            r = c.post("/jobs", json=body)
            submit_ms.append((time.perf_counter() - t0) * 1000)
            r.raise_for_status()
            ids.append(r.json()["job_id"])
    deadline = time.monotonic() + budgets["probe_timeout_s"]
    with psycopg.connect(info["database_url"], autocommit=True) as conn:
        while True:
            left = conn.execute("SELECT count(*) FROM jobs WHERE job_id = ANY(%s::uuid[]) "
                                "AND state NOT IN ('succeeded', 'dead', 'cancelled')", (ids,)).fetchone()[0]
            if left == 0 or time.monotonic() > deadline:
                break
            time.sleep(0.1)
        rows = conn.execute(
            "SELECT j.job_id::text, j.state, j.attempt, "
            "extract(epoch FROM a.started_at - j.enqueued_at)::float8, "
            "extract(epoch FROM j.finished_at - j.enqueued_at)::float8 "
            "FROM jobs j LEFT JOIN attempts a ON a.job_id = j.job_id AND a.attempt = 1 "
            "WHERE j.job_id = ANY(%s::uuid[])", (ids,)).fetchall()
    start_ms = [r[3] * 1000 for r in rows if r[3] is not None]
    ack_ms = [r[4] * 1000 for r in rows if r[1] == "succeeded" and r[4] is not None]
    states = {}
    for r in rows:
        states[r[1]] = states.get(r[1], 0) + 1
    return {"jobs": n, "job_ids": ids, "unfinished": left, "states": states,
            "retried": sum(1 for r in rows if r[2] > 1), "dead": states.get("dead", 0),
            "submit_ms": {"p50": r1(pct(submit_ms, 50)), "p95": r1(pct(submit_ms, 95)), "max": r1(max(submit_ms))},
            "enqueue_to_start_ms": {"p50": r1(pct(start_ms, 50)), "p95": r1(pct(start_ms, 95)),
                                    "max": r1(max(start_ms, default=None))},
            "enqueue_to_ack_ms": {"p50": r1(pct(ack_ms, 50)), "p95": r1(pct(ack_ms, 95)),
                                  "max": r1(max(ack_ms, default=None))}}


def log_check(info: dict, ids: list[str], required: list[str]) -> dict:
    seen: dict[str, set] = {e: set() for e in required}
    path = Path(info["log_dir"]) / "worker.jsonl"
    bad_lines = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            bad_lines += 1
            continue
        if rec.get("event") in seen and rec.get("job_id"):
            seen[rec["event"]].add(str(rec["job_id"]))
    missing = {e: sorted(set(ids) - s)[:5] for e, s in seen.items() if set(ids) - s}
    return {"file": str(path), "non_json_lines": bad_lines, "missing": missing,
            "covered": {e: len(set(ids) & s) for e, s in seen.items()}}


def metrics_check(info: dict) -> dict:
    env = {**os.environ, "DATABASE_URL": info["database_url"], "PA_LOG_FORMAT": "json"}
    out = subprocess.run([PY, "-m", "personal_agent", "metrics", "--since-minutes", "60"], cwd=ROOT, env=env,
                         capture_output=True, text=True, timeout=60)
    try:
        return {"ok": out.returncode == 0, "snapshot": json.loads(out.stdout)}
    except ValueError:
        return {"ok": False, "stderr": out.stderr[-2000:]}


def system_checks(info: dict, budgets: dict, out_path: Path) -> dict:
    ids = [json.loads(line)["id"] for line in (ROOT / "evals/tasks/system.jsonl").read_text().splitlines() if line.strip()]
    trials = budgets["system_checks_trials"]
    env = {**os.environ, "EVAL_DATABASE_URL": info["eval_database_url"], "PA_AGENTS_TRACING": "off"}
    env.pop("TEST_DATABASE_URL", None)
    proc = subprocess.run([PY, "-m", "evals.run_evals", "--only", *ids, "--trials", str(trials), "--k", "1",
                           "--out", str(out_path)], cwd=ROOT, env=env, capture_output=True, text=True, timeout=900)
    if proc.returncode or not out_path.exists():
        return {"ok": False, "tasks": ids, "error": (proc.stdout + proc.stderr)[-3000:]}
    res = json.loads(out_path.read_text())
    per = {tid: {"passed": e["c"], "trials": e["n"], "first_failure": e.get("first_failure")}
           for tid, e in res["per_task"].items()}
    rate = min((e["c"] / e["n"]) for e in res["per_task"].values())
    return {"ok": rate >= budgets["system_checks_min_pass_rate"] and len(per) == len(ids),
            "tasks": per, "min_pass_rate": rate, "results": str(out_path)}


def evaluate(report: dict, budgets: dict) -> list[str]:
    p, problems = report["probe"], []

    def over(label, value, limit):
        if value is None or value > limit:
            problems.append(f"{label} = {value} exceeds budget {limit}")
    over("submit p95 ms", p["submit_ms"]["p95"], budgets["submit_p95_ms"])
    over("enqueue->start p95 ms", p["enqueue_to_start_ms"]["p95"], budgets["enqueue_to_start_p95_ms"])
    over("enqueue->ack p95 ms", p["enqueue_to_ack_ms"]["p95"], budgets["enqueue_to_ack_p95_ms"])
    if p["unfinished"]:
        problems.append(f"{p['unfinished']} probe job(s) not finished within {budgets['probe_timeout_s']}s")
    if p["dead"] > budgets["probe_max_dead"]:
        problems.append(f"{p['dead']} probe job(s) dead (budget {budgets['probe_max_dead']})")
    if p["retried"] > budgets["probe_max_retried"]:
        problems.append(f"{p['retried']} probe job(s) retried (budget {budgets['probe_max_retried']})")
    if report["logs"]["missing"]:
        problems.append(f"structured log events missing for probe jobs: {report['logs']['missing']}")
    if not report["metrics"]["ok"]:
        problems.append("`personal-agent metrics` failed against the instance")
    sc = report.get("system_checks")
    if sc is not None and not sc["ok"]:
        failing = {t: v for t, v in sc.get("tasks", {}).items() if v["passed"] < v["trials"]} or sc.get("error")
        problems.append(f"system checks failed: {failing}")
    return problems


FIX_HINTS = {   # first key contained in the problem text wins, so specific keys come first
    "system checks": "rerun one task with `python -m evals.run_evals --only <id> --trials 1 --keep-workdir` and read RELIABILITY.md",
    "structured log": "every worker event must go through personal_agent.obs.log_event (see docs/design-docs/golden-principles.md)",
    "metrics` failed": "run `python -m personal_agent metrics` by hand against the instance to see the error",
    "submit p95": "POST /jobs must only write the row and return; look for new work in personal_agent/api/app.py or PgBroker.enqueue",
    "enqueue->start": "workers lease too slowly: check --poll-interval / --concurrency and LEASE_SQL in personal_agent/queue/broker.py",
    "enqueue->ack": "processing got slower: compare processing_ms in `personal-agent metrics`, then the handler and persist/ack path in personal_agent/queue/worker.py",
    "job(s) dead": "inspect `python scripts/logq.py --event failed` and the dead_letters table",
    "job(s) retried": "inspect `python scripts/logq.py --event failed lease_reaped fenced`",
    "not finished": "is the worker alive? `python scripts/dev_instance.py status`, then `python scripts/logq.py --last 20`",
}


def to_markdown(report: dict, budgets: dict, problems: list[str]) -> str:
    p = report["probe"]
    lines = [f"## Runtime check: {'PASSED' if not problems else 'FAILED'}", "",
             f"Instance `{report['instance']}` · {report['recorded_at']} · probe {p['jobs']} `demo.summarize` jobs", "",
             "| measure | p50 | p95 | max | budget (p95) |", "| --- | --- | --- | --- | --- |"]
    for key, budget in (("submit_ms", "submit_p95_ms"), ("enqueue_to_start_ms", "enqueue_to_start_p95_ms"),
                        ("enqueue_to_ack_ms", "enqueue_to_ack_p95_ms")):
        m = p[key]
        lines.append(f"| {key} | {m['p50']} | {m['p95']} | {m['max']} | {budgets[budget]} |")
    lines += ["", f"States: {p['states']}; retried: {p['retried']}; dead: {p['dead']}.",
              f"Structured log coverage: {report['logs']['covered']}."]
    sc = report.get("system_checks")
    if sc:
        lines += ["", "| system check | passed |", "| --- | --- |"]
        for tid, v in sc.get("tasks", {}).items():
            lines.append(f"| `{tid}` | {v['passed']}/{v['trials']} |")
    if problems:
        lines += ["", "### Problems and where to look"]
        for prob in problems:
            hint = next((h for k, h in FIX_HINTS.items() if k in prob), "")
            lines.append(f"- {prob}" + (f"  \n  next: {hint}" if hint else ""))
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--name", default=os.environ.get("PA_DEV_NAME", "default"))
    ap.add_argument("--up", action="store_true", help="start the instance first and tear it down afterwards")
    ap.add_argument("--keep", action="store_true", help="with --up: leave the instance running")
    ap.add_argument("--db", default="auto", choices=["auto", "admin", "initdb", "docker"])
    ap.add_argument("--budgets", default=str(ROOT / "scripts" / "runtime_budgets.json"))
    ap.add_argument("--skip-system-checks", action="store_true")
    a = ap.parse_args(argv)
    budgets = json.loads(Path(a.budgets).read_text())
    started = False
    if a.up and not (dev_instance.load(a.name) and dev_instance.alive(dev_instance.load(a.name)["pids"].get("worker"))):
        rc = dev_instance.main(["up", "--name", a.name, "--db", a.db])
        if rc:
            return rc
        started = True
    info = dev_instance.load(a.name)
    if info is None or not dev_instance.alive(info["pids"].get("worker")) or not info.get("api_url"):
        print(f"no running instance {a.name!r} with an API. fix: `make dev-up` (or pass --up)", file=sys.stderr)
        return 2
    d = dev_instance.instance_dir(a.name)
    try:
        run_id = f"rc-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
        report = {"instance": a.name, "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "budgets": budgets}
        report["probe"] = probe(info, budgets, run_id)
        time.sleep(0.3)   # let the worker flush its last log lines
        report["logs"] = log_check(info, report["probe"]["job_ids"], budgets["log_events_required"])
        report["metrics"] = metrics_check(info)
        if not a.skip_system_checks:
            report["system_checks"] = system_checks(info, budgets, d / "system-checks.json")
        problems = evaluate(report, budgets)
        report["problems"] = problems
        report["probe"].pop("job_ids")
        (d / "runtime-check.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n")
        md = to_markdown(report, budgets, problems)
        (d / "runtime-check.md").write_text(md)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
                f.write(md)
        print(md)
        print(f"wrote {d / 'runtime-check.json'}")
        return 1 if problems else 0
    finally:
        if started and not a.keep:
            dev_instance.main(["down", "--name", a.name])


if __name__ == "__main__":
    sys.exit(main())

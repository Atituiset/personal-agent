#!/usr/bin/env python3
"""personal-agent 评测 runner：真实 worker 进程 + Postgres 队列 + Agents SDK，多次试验，算 pass@k / pass^k。

    .venv/bin/python -m evals.run_evals --trials 3 --k 1 3 --out test-results/evals-results.json

流程：
  1. 连评测库（EVAL_DATABASE_URL，没有就用 TEST_DATABASE_URL；库名必须含 test 或 eval，因为会 TRUNCATE）；
  2. 普通任务：每次试验把全部任务入队（payload 里只有指令、输入、工具白名单和产物引用，没有期望值），
     起一个 worker 跑完；
  3. 场景任务：kill -9（persist 前/后）、重复入队，逐个用独立 worker 跑；
  4. 评分在评测进程里做：读 jobs / attempts / effects / dead_letters 表、结果产物和调用账本（graders.py）；
  5. 汇总成 results.json：每个任务的 pass@1 / pass@k / pass^k、Wilson 95% 区间、token 和执行耗时。

模型：默认 PA_AGENT_MODEL=py:evals.policy:ScriptedAssistantModel（离线脚本策略）。
换真实模型：--model openai，并设置 PA_LLM_BASE_URL / PA_LLM_MODEL / PA_LLM_API_KEY（见 evals/README.md）。
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict

from personal_agent.cli import migrate
from personal_agent.config import load_dotenv
from personal_agent.core.storage import ArtifactStore
from personal_agent.queue.broker import PgBroker

from .graders import grade, leak_check
from .metrics import pass_at_k, pass_hat_k, percentile, wilson_interval

ROOT = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent
DEFAULT_MODEL = "py:evals.policy:ScriptedAssistantModel"
FAST = ["--lease-seconds", "2", "--reap-interval", "0.5", "--poll-interval", "0.05"]
TERMINAL = ("succeeded", "dead", "cancelled")


def load_tasks(only: list[str] | None) -> list[dict]:
    tasks = []
    for path in sorted((HERE / "tasks").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                tasks.append(json.loads(line))
    if only:
        tasks = [t for t in tasks if t["id"] in only]
    return tasks


def sql(db, q, params=()):
    with psycopg.connect(db, autocommit=True) as conn:
        cur = conn.execute(q, params)
        return cur.fetchall() if cur.description else []


class Env:
    def __init__(self, db: str, workdir: Path, model: str, profile: str, seed: str, grader_mode: str):
        self.db, self.dir, self.model, self.profile, self.seed = db, workdir, model, profile, seed
        self.artifacts = workdir / "artifacts"
        self.faults = workdir / "faults"
        self.ledger = workdir / "llm-ledger.jsonl"
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.faults.mkdir(exist_ok=True)
        self.store = ArtifactStore(self.artifacts)
        self.procs: list[subprocess.Popen] = []
        # 有权限的产物和一个「不在白名单里」的金丝雀产物：后者只放进存储，不出现在任何 payload 里
        self.fixtures = {p.stem: self.store.put(p.read_bytes()) for p in (HERE / "fixtures").glob("*.txt")}

    def spawn_worker(self, name: str, fault: str | None = None, concurrency: int = 4,
                     seed: str | None = None) -> subprocess.Popen:
        # 场景任务每次试验起新 worker 进程：把试验号并进种子，noisy 档案的随机数才会逐次不同
        e = {**os.environ, "DATABASE_URL": self.db, "PA_ARTIFACT_DIR": str(self.artifacts),
             "PA_AGENT_MODEL": self.model, "PA_EVAL_POLICY": self.profile, "PA_EVAL_SEED": seed or self.seed,
             "PA_LLM_LEDGER": str(self.ledger), "PYTHONUNBUFFERED": "1", "PA_AGENTS_TRACING": "off"}
        e.pop("PA_FAULT_PAUSE_AT", None)
        if fault:
            e.update(PA_FAULT_PAUSE_AT=fault, PA_FAULT_DIR=str(self.faults))
        log = (self.dir / f"{name}.log").open("w")
        p = subprocess.Popen([sys.executable, "-m", "personal_agent", "worker", "--worker-id", name,
                              "--concurrency", str(concurrency), *FAST],
                             cwd=ROOT, env=e, stdout=log, stderr=subprocess.STDOUT)
        self.procs.append(p)
        return p

    def stop_all(self):
        for p in self.procs:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(5)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
        self.procs.clear()

    def ledger_calls(self, job_id: str) -> int:
        if not self.ledger.exists():
            return 0
        return sum(1 for line in self.ledger.read_text().splitlines()
                   if line.strip() and json.loads(line).get("job_id") == job_id)

    def payload_for(self, task: dict) -> dict:
        p = dict(task["payload"])
        p["artifacts"] = {name: self.fixtures[name] for name in p.get("artifacts", [])}
        return p

    async def enqueue(self, task: dict, key: str) -> tuple[str, bool]:
        broker = await PgBroker(self.db, max_size=2).open()
        try:
            job_opts = task.get("job", {})
            deadline = None
            if "deadline_in_s" in job_opts:
                deadline = datetime.now(timezone.utc) + timedelta(seconds=job_opts["deadline_in_s"])
            job, dedup = await broker.enqueue(task_type="agent.run", payload_ref=self.store.put_json(self.payload_for(task)),
                                              idempotency_key=key, max_attempts=job_opts.get("max_attempts", 3),
                                              deadline_at=deadline)
            return str(job["job_id"]), dedup
        finally:
            await broker.close()


def wait_terminal(db: str, job_ids: list[str], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = sql(db, "SELECT count(*) FROM jobs WHERE job_id = ANY(%s::uuid[]) AND state = ANY(%s)",
                   (job_ids, list(TERMINAL)))
        if rows[0][0] == len(job_ids):
            return
        time.sleep(0.1)
    raise TimeoutError(f"jobs not finished within {timeout}s")


def wait_file(path: Path, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise TimeoutError(f"{path.name} not reached within {timeout}s")


def collect(env: Env, job_id: str) -> dict:
    [(state, attempt, result_ref, last_error)] = sql(
        env.db, "SELECT state, attempt, result_ref, last_error FROM jobs WHERE job_id = %s", (job_id,))
    attempts = []
    for a, outcome, usage, error, started, finished in sql(
            env.db, "SELECT attempt, outcome, usage, error, started_at, finished_at FROM attempts "
                    "WHERE job_id = %s ORDER BY attempt", (job_id,)):
        attempts.append({"attempt": a, "outcome": outcome, "error_kind": (error or {}).get("kind"),
                         "model_calls": len(usage or []),
                         "tokens": sum(u["usage"]["total_tokens"] for u in usage or []),
                         "seconds": (finished - started).total_seconds() if finished and started else None})
    dl = sql(env.db, "SELECT reason, attempts FROM dead_letters WHERE job_id = %s", (job_id,))
    effects = []
    for receipt, in sql(env.db, "SELECT receipt FROM effects WHERE job_id = %s ORDER BY created_at", (job_id,)):
        note = env.store.get_json(receipt["note_ref"]) if receipt and receipt.get("note_ref") else {}
        effects.append({"receipt_attempt": (receipt or {}).get("attempt"), "note": note})
    result = None
    if result_ref:
        stored = env.store.get_json(result_ref)
        result = {"attempt": stored["attempt"], **stored["result"]}
    return {"job_id": job_id, "state": state, "attempt": attempt, "last_error": last_error,
            "dead_reason": dl[0][0] if dl else None, "dead_attempts": dl[0][1] if dl else None,
            "attempts": attempts, "effects": effects, "result": result,
            "ledger_calls": env.ledger_calls(job_id)}


def run_normal(env: Env, tasks: list[dict], trials: int, run_id: str, timeout: float) -> dict:
    jobs = {}
    for trial in range(trials):
        for t in tasks:
            jid, _ = asyncio.run(env.enqueue(t, f"{run_id}-{t['id']}-{trial}"))
            jobs[(t["id"], trial)] = jid
    if jobs:
        env.spawn_worker("eval-worker")
        try:
            wait_terminal(env.db, list(jobs.values()), timeout)
        finally:
            env.stop_all()
    return {k: collect(env, v) for k, v in jobs.items()}


def run_scenario(env: Env, t: dict, trial: int, run_id: str, timeout: float) -> dict:
    sc, key = t["scenario"], f"{run_id}-{t['id']}-{trial}"
    extra = {}
    try:
        if sc in ("kill_before_persist", "kill_after_persist"):
            fault = sc.removeprefix("kill_")
            jid, _ = asyncio.run(env.enqueue(t, key))
            a = env.spawn_worker(f"{t['id']}-{trial}-A", fault=fault, concurrency=1, seed=f"{env.seed}:{trial}")
            wait_file(env.faults / f"{fault}.{jid}.reached", timeout)
            extra["ledger_calls_before_kill"] = env.ledger_calls(jid)
            os.kill(a.pid, signal.SIGKILL)
            a.wait(5)
            env.spawn_worker(f"{t['id']}-{trial}-B", concurrency=1, seed=f"{env.seed}:{trial}")
            wait_terminal(env.db, [jid], timeout)
        elif sc == "enqueue_twice":
            jid, d1 = asyncio.run(env.enqueue(t, key))
            jid2, d2 = asyncio.run(env.enqueue(t, key))
            extra["second_enqueue_deduplicated"] = d2 and jid2 == jid and not d1
            extra["jobs_for_key"] = sql(env.db, "SELECT count(*) FROM jobs WHERE idempotency_key = %s", (key,))[0][0]
            env.spawn_worker(f"{t['id']}-{trial}", concurrency=1, seed=f"{env.seed}:{trial}")
            wait_terminal(env.db, [jid], timeout)
        else:
            raise ValueError(f"unknown scenario {sc}")
    finally:
        env.stop_all()
    facts = collect(env, jid)
    facts["scenario"] = extra
    return facts


def summarize(tasks, facts, mode, ks, trials):
    per_task, trial_rows = {}, []
    for t in tasks:
        rows = []
        for trial in range(trials):
            f = facts[(t["id"], trial)]
            g = grade(t, f, mode)
            tokens = sum(a["tokens"] for a in f["attempts"])
            secs = sum(a["seconds"] or 0 for a in f["attempts"])
            rows.append({"task": t["id"], "trial": trial, "passed": g["passed"], "failures": g["failures"],
                         "violations": g["violations"], "state": f["state"], "attempts": f["attempt"],
                         "model_calls": f["ledger_calls"], "tokens": tokens, "exec_seconds": round(secs, 4),
                         "final_output": (f.get("result") or {}).get("final_output"),
                         "tool_calls": [(c.get("name"), c.get("arguments")) for c in (f.get("result") or {}).get("tool_calls", [])]})
        c = sum(r["passed"] for r in rows)
        lo, hi = wilson_interval(c, trials)
        entry = {"kind": t["kind"], "category": t["category"], "n": trials, "c": c, "pass@1": c / trials,
                 "wilson95": [round(lo, 4), round(hi, 4)],
                 "tokens_mean": round(sum(r["tokens"] for r in rows) / trials, 1),
                 "violations": sum(bool(r["violations"]) for r in rows),
                 "first_failure": next((r["failures"] + r["violations"] for r in rows if not r["passed"]), None)}
        for k in ks:
            entry[f"pass@{k}"] = round(pass_at_k(trials, c, k), 4)
            entry[f"pass^{k}"] = round(pass_hat_k(trials, c, k), 4)
        per_task[t["id"]] = entry
        trial_rows += rows

    suites = {}
    for suite in ("regression", "capability"):
        ids = [tid for tid, e in per_task.items() if e["kind"] == suite]
        if not ids:
            continue
        rows = [r for r in trial_rows if r["task"] in ids]
        c = sum(r["passed"] for r in rows)
        lo, hi = wilson_interval(c, len(rows))
        secs = [r["exec_seconds"] for r in rows]
        s = {"tasks": len(ids), "trials": len(rows), "passed_trials": c, "pass@1": round(c / len(rows), 4),
             "wilson95": [round(lo, 4), round(hi, 4)],
             "tokens_mean": round(sum(r["tokens"] for r in rows) / len(rows), 1),
             "model_calls_mean": round(sum(r["model_calls"] for r in rows) / len(rows), 3),
             "exec_p50_s": round(percentile(secs, 50), 4), "exec_p95_s": round(percentile(secs, 95), 4)}
        for k in ks:
            s[f"pass@{k}"] = round(sum(per_task[i][f"pass@{k}"] for i in ids) / len(ids), 4)
            s[f"pass^{k}"] = round(sum(per_task[i][f"pass^{k}"] for i in ids) / len(ids), 4)
        suites[suite] = s
    summary = {"suites": suites, "trials_with_violations": sum(bool(r["violations"]) for r in trial_rows)}
    return summary, per_task, trial_rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--k", type=int, nargs="+", default=[1, 3])
    ap.add_argument("--out", default="test-results/evals-results.json")
    ap.add_argument("--model", default=os.environ.get("PA_AGENT_MODEL", DEFAULT_MODEL),
                    help="PA_AGENT_MODEL for the worker: py:module:Class, openai, or fake")
    ap.add_argument("--profile", default="baseline", help="PA_EVAL_POLICY for the scripted policy")
    ap.add_argument("--seed", default="0")
    ap.add_argument("--grader", choices=["isolated", "naive"], default="isolated")
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--timeout", type=float, default=180)
    ap.add_argument("--keep-workdir", action="store_true")
    ap.add_argument("--no-trials", action="store_true",
                    help="omit per-trial rows (use for evals/baseline.json; the gate only needs the summary)")
    args = ap.parse_args(argv)
    if any(k > args.trials for k in args.k):
        ap.error("every k must be <= --trials")

    load_dotenv(ROOT / ".env")
    db = os.environ.get("EVAL_DATABASE_URL") or os.environ.get("TEST_DATABASE_URL")
    if not db:
        ap.error("set EVAL_DATABASE_URL (or TEST_DATABASE_URL)")
    dbname = conninfo_to_dict(db).get("dbname", "")
    if "test" not in dbname and "eval" not in dbname:
        ap.error(f"refusing to TRUNCATE database {dbname!r}: its name must contain 'test' or 'eval'")

    tasks = load_tasks(args.only)
    problems = [p for t in tasks for p in leak_check(t)]
    if problems:
        print("task leak check failed:", *problems, sep="\n  ")
        return 2

    # One run per eval database: a second run's TRUNCATE would wipe the first run's jobs.
    lock = psycopg.connect(db, autocommit=True)
    if not lock.execute("SELECT pg_try_advisory_lock(hashtext('personal-agent-evals'))").fetchone()[0]:
        print("another eval run holds this database; wait for it to finish", file=sys.stderr)
        return 2
    migrate(db)
    sql(db, "TRUNCATE jobs, attempts, checkpoints, dead_letters, effects CASCADE")
    workdir = Path(tempfile.mkdtemp(prefix="pa-evals-"))
    env = Env(db, workdir, args.model, args.profile, args.seed, args.grader)
    run_id = f"eval-{int(time.time())}"
    t0 = time.monotonic()
    try:
        normal = [t for t in tasks if not t.get("scenario")]
        facts = run_normal(env, normal, args.trials, run_id, args.timeout)
        for t in tasks:
            if t.get("scenario"):
                for trial in range(args.trials):
                    facts[(t["id"], trial)] = run_scenario(env, t, trial, run_id, args.timeout)
    finally:
        env.stop_all()
    wall = time.monotonic() - t0

    summary, per_task, rows = summarize(tasks, facts, args.grader, args.k, args.trials)
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
                             text=True).stdout.strip() or None
    except OSError:
        sha = None
    meta = {"model": args.model, "profile": args.profile if args.model == DEFAULT_MODEL else None,
            "grader": args.grader, "trials": args.trials, "k": args.k, "tasks": len(tasks),
            "wall_seconds": round(wall, 2), "git": sha, "python": sys.version.split()[0],
            "openai_agents": importlib.metadata.version("openai-agents"),
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    out = {"meta": meta, "summary": summary, "per_task": per_task}
    if not args.no_trials:
        out["trials"] = rows
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"model={args.model} profile={args.profile} grader={args.grader} trials={args.trials} "
          f"tasks={len(tasks)} wall={wall:.1f}s")
    for tid, e in per_task.items():
        ks = " ".join(f"pass^{k}={e[f'pass^{k}']:.2f}" for k in args.k if k > 1)
        mark = "ok " if e["c"] == e["n"] else ("~  " if e["c"] else "XX ")
        print(f"  {mark}{tid:<36} {e['kind']:<10} {e['c']}/{e['n']} {ks}"
              + (f"  <- {e['first_failure'][0]}" if e["first_failure"] else ""))
    for suite, s in summary["suites"].items():
        ks = " ".join(f"pass@{k}={s[f'pass@{k}']:.3f} pass^{k}={s[f'pass^{k}']:.3f}" for k in args.k)
        print(f"{suite}: {s['passed_trials']}/{s['trials']} trials, {ks}, Wilson95={s['wilson95']}, "
              f"tokens/trial={s['tokens_mean']}, exec p50={s['exec_p50_s']}s p95={s['exec_p95_s']}s")
    print(f"trials with process violations: {summary['trials_with_violations']}")
    print(f"wrote {args.out}")
    if not args.keep_workdir:
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)
    else:
        print(f"workdir kept at {workdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

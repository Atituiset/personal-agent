#!/usr/bin/env python3
"""把评测门禁的失败变成下一轮要做的事：机器可读的 JSON + 一份可以直接当执行计划用的 Markdown。

    .venv/bin/python -m evals.followup --results test-results/evals-results.json \\
        --baseline evals/baseline.json --gate-report test-results/gate-report.json \\
        --out-dir test-results/followup [--exec-plan-dir docs/exec-plans/active] [--summary]

- 只在门禁失败时生成（--always 则总是生成）；门禁本身（evals/gate.py、pass^k 判定）不变。
- 每个失败项给出：哪个任务、哪些评分器说了什么（按次数汇总）、过程违规、轨迹指针
  （试验号、job_id、状态、attempt 数、工具调用、最终回答；用 --keep-workdir 跑时还有 worker 日志目录）、
  任务定义在 evals/tasks/*.jsonl 的哪一行、复现命令、建议的下一步（按失败信息匹配到代码位置和 RELIABILITY.md 的不变量）。
- --exec-plan-dir：本地用，写成 docs/exec-plans/active/ 下的执行计划；--summary：CI 用，追加到 $GITHUB_STEP_SUMMARY。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from . import gate

ROOT = Path(__file__).resolve().parents[1]
MAX_TRAJECTORIES = 3

# (pattern on a grader failure / violation / gate problem, next action). First match wins.
NEXT_ACTIONS: list[tuple[str, str]] = [
    (r"claims to have saved but no effect", "反作弊违规：回答声称做了副作用但 effects 表里没有。查模型/策略为什么不调 save_note；"
                                            "不要放宽评分器（evals/graders.py）。"),
    (r"not in payload\.tools|side effect without save_note permission",
     "越权：调用了白名单外的工具或产生了未授权副作用。查 personal_agent/tasks/agent.py 的 _validate / TOOLS（SECURITY.md）。"),
    (r"unlisted artifact content leaked", "泄露：白名单外的产物内容出现在输出里。查 read_artifact 的权限检查（personal_agent/tasks/agent.py）。"),
    (r"effect receipts from attempts|effects != |repeated save_note returned different",
     "副作用账本：同一副作用执行了多次或没执行。查 personal_agent/core/context.py 的 JobContext.effect 和 "
     "_save_note 的 effect key（RELIABILITY.md R7）。"),
    (r"runner rerun|final attempt|result from attempt",
     "崩溃恢复路径：查 personal_agent/queue/worker.py 的 receipt 检查和 PgBroker.find_receipt / persist_receipt（RELIABILITY.md R3、R4）。"),
    (r"jobs for one idempotency key|not deduplicated", "幂等：查 PgBroker.enqueue 的 ON CONFLICT（RELIABILITY.md R2）。"),
    (r"dead_reason|expected \d+ attempts|attempts \d+ != |state \w+ != dead|state \w+ \(want",
     "错误分类 / 死信：查 personal_agent/infra/agents_sdk.py 的 map_error、worker._fail、PgBroker.fail（RELIABILITY.md R8）。"),
    (r"attempts\.usage|ledger has|usage for every model call",
     "用量记账：查 personal_agent/infra/agents_sdk.py 的 LedgerHooks 和 worker 写 attempts.usage 的路径。"),
    (r"required tool .* not called|args .* != |tool order|tool calls >|outputs .* not ≈",
     "工具选择 / 参数：脚本策略看 evals/policy.py，真实模型看指令和工具描述（personal_agent/tasks/agent.py）。"
     "先确认期望本身没写错（evals/tasks/*.jsonl），不要为了通过去改期望。"),
    (r"^output |converted time|current_time returned|answer does not use", "回答内容：对照轨迹里的工具输出和最终回答，看是没用工具结果还是格式不符。"),
    (r"tokens/trial rose", "成本上涨：对比各任务 tokens_mean，查提示、工具输出长度和轮数；确实需要就更新基线并在提交里写明原因。"),
    (r"pass\^\d+ dropped", "可靠性下降（pass^k）：先看下面逐任务的失败；如果逐任务都过但 pass^k 掉了，是偶发失败，用更多 --trials 复现。"),
    (r"pass@\d+ dropped", "能力集下降：看能力集任务的失败信息。"),
    (r"violated process rules", "过程违规：见对应任务的 violations。"),
    (r"missing from current run", "任务缺失：本次运行没有这个任务。检查 --only 参数或任务文件是否被删改。"),
]
DEFAULT_ACTION = "用下面的复现命令（带 --keep-workdir）重跑，再用 scripts/logq.py --log-dir <workdir> --job <job_id> 看 worker 事件。"


def next_action(messages: list[str]) -> str:
    for msg in messages:
        for pattern, action in NEXT_ACTIONS:
            if re.search(pattern, msg):
                return action
    return DEFAULT_ACTION


def task_locations() -> dict[str, dict]:
    out = {}
    for path in sorted((ROOT / "evals" / "tasks").glob("*.jsonl")):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip():
                t = json.loads(line)
                out[t["id"]] = {"file": path.relative_to(ROOT).as_posix(), "line": i, "expect": sorted(t.get("expect", {}))}
    return out


def build(results: dict, baseline: dict, gate_report: dict) -> dict:
    k = gate_report.get("k", 3)
    tol = gate_report.get("max_drop", 0.02)
    locs = task_locations()
    trials = results.get("trials") or []
    workdir = results.get("meta", {}).get("workdir")
    items = []
    for tid, cur in results["per_task"].items():
        base = baseline.get("per_task", {}).get(tid)
        regressed = base is not None and cur["kind"] == "regression" and cur["pass@1"] < base["pass@1"] - tol
        violated = bool(cur.get("violations"))
        if not (regressed or violated):
            continue
        rows = [r for r in trials if r["task"] == tid and not r["passed"]]
        graders = Counter(m for r in rows for m in r.get("failures", []))
        violations = Counter(m for r in rows for m in r.get("violations", []))
        if not rows and cur.get("first_failure"):
            graders.update(cur["first_failure"])
        msgs = list(violations) + list(graders)
        items.append({
            "task": tid, "kind": cur["kind"], "category": cur.get("category"),
            "reason": "violation" if violated else "regression",
            "pass@1": {"baseline": base["pass@1"] if base else None, "current": cur["pass@1"]},
            "passed": f"{cur['c']}/{cur['n']}",
            "graders": [{"message": m, "trials": n} for m, n in graders.most_common()],
            "violations": [{"message": m, "trials": n} for m, n in violations.most_common()],
            "trajectories": [{"trial": r["trial"], "job_id": r.get("job_id"), "state": r.get("state"),
                              "attempts": r.get("attempts"), "model_calls": r.get("model_calls"),
                              "tool_calls": r.get("tool_calls"), "final_output": (r.get("final_output") or "")[:400]}
                             for r in rows[:MAX_TRAJECTORIES]],
            "task_definition": locs.get(tid),
            "next_action": next_action(msgs),
        })
    for tid, base in baseline.get("per_task", {}).items():
        if base["kind"] == "regression" and tid not in results["per_task"]:
            items.append({"task": tid, "kind": "regression", "reason": "missing", "task_definition": locs.get(tid),
                          "next_action": next_action(["missing from current run"])})
    suite_level = [{"problem": p, "next_action": next_action([p])} for p in gate_report.get("problems", [])
                   if not p.startswith("regression task ")]
    failing = [i["task"] for i in items]
    repro = (f"python -m evals.run_evals --only {' '.join(failing)} --trials 5 --k 1 3 5 --keep-workdir "
             f"--out test-results/followup-rerun.json") if failing else \
        "python -m evals.run_evals --trials 5 --k 1 3 5 --keep-workdir --out test-results/followup-rerun.json"
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "gate": {"passed": gate_report.get("passed"), "k": k, "problems": gate_report.get("problems", [])},
        "run": {key: results.get("meta", {}).get(key) for key in ("model", "profile", "grader", "trials", "git", "recorded_at")},
        "workdir": workdir,
        "suite_level": suite_level,
        "tasks": items,
        "repro": repro,
        "verify": ["python -m evals.run_evals --trials 5 --k 1 3 5 --out test-results/evals-results.json",
                   f"python -m evals.gate --baseline evals/baseline.json --current test-results/evals-results.json --k {k}"],
    }


def to_markdown(fu: dict, as_plan: bool) -> str:
    today = datetime.now(timezone.utc).date().isoformat()
    lines = []
    if as_plan:
        paths = sorted({t["task_definition"]["file"] for t in fu["tasks"] if t.get("task_definition")}) or ["evals"]
        lines += ["---", 'owner: "@Atituiset"', f"last_reviewed: {today}", "status: active",
                  f"code_paths: [{', '.join(paths)}]", "---"]
    run = fu["run"]
    lines += [f"# 评测门禁失败跟进（{run.get('git') or 'unknown'}，{today}）", "",
              "## 目的", "",
              f"`evals.gate` 在 {run.get('model')}"
              + (f" / {run.get('profile')}" if run.get("profile") else "")
              + f"（{run.get('trials')} 次试验）上失败。逐项修好，再跑「验证」里的命令让门禁通过。"
              "本文件由 `evals/followup.py` 生成；修完后按 PLANS.md 补决策日志并移到 completed/。", ""]
    lines += ["## 门禁结论", ""] + [f"- {p}" for p in fu["gate"]["problems"]] + [""]
    lines += ["## 进度", ""]
    for t in fu["tasks"]:
        lines.append(f"- [ ] `{t['task']}`（{t['reason']}）：{t['next_action']}")
    for s in fu["suite_level"]:
        lines.append(f"- [ ] {s['problem']}：{s['next_action']}")
    lines.append("")
    if fu["tasks"]:
        lines += ["## 失败任务", ""]
    for t in fu["tasks"]:
        d = t.get("task_definition") or {}
        lines += [f"### `{t['task']}`", ""]
        if t["reason"] == "missing":
            lines += ["基线里有、本次运行缺失。", ""]
            continue
        lines += [f"- 类型：{t['kind']} / {t.get('category')}；通过 {t['passed']}；"
                  f"pass@1 {t['pass@1']['baseline']} → {t['pass@1']['current']}",
                  f"- 任务定义：`{d.get('file')}` 第 {d.get('line')} 行（期望字段：{', '.join(d.get('expect', []))}）"]
        for g in t["graders"]:
            lines.append(f"- 评分器：{g['message']}（{g['trials']} 次）")
        for v in t["violations"]:
            lines.append(f"- **违规**：{v['message']}（{v['trials']} 次）")
        for tr in t["trajectories"]:
            calls = ", ".join(f"{n}({a})" for n, a in (tr.get("tool_calls") or [])) or "无工具调用"
            out = (tr.get("final_output") or "").replace("\n", " ")[:200]
            lines.append(f"- 轨迹：trial {tr['trial']} · job `{tr.get('job_id')}` · {tr.get('state')} · "
                         f"attempts {tr.get('attempts')} · 模型调用 {tr.get('model_calls')} · {calls} · 回答：{out!r}")
        lines.append(f"- 下一步：{t['next_action']}")
        lines.append("")
    if fu.get("workdir"):
        first = next((tr["job_id"] for t in fu["tasks"] for tr in t.get("trajectories", []) if tr.get("job_id")), "<job_id>")
        lines += [f"worker 日志（JSON 行）和产物在 `{fu['workdir']}`；看一个任务的完整事件时间线：", "",
                  "```bash", f"python scripts/logq.py --log-dir {fu['workdir']} --job {first}", "```", ""]
    lines += ["## 复现", "", "```bash", fu["repro"], "```", "",
              "## 验证", "", "```bash", *fu["verify"], "```", "",
              "## 决策日志", "", "- （修复时补充：根因、改了什么、为什么没改期望或基线）", ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--results", default="test-results/evals-results.json")
    ap.add_argument("--baseline", default="evals/baseline.json")
    ap.add_argument("--gate-report", help="output of evals.gate --report-json; recomputed if omitted")
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--out-dir", default="test-results/followup")
    ap.add_argument("--exec-plan-dir", help="also write the follow-up as an exec plan here (e.g. docs/exec-plans/active)")
    ap.add_argument("--summary", action="store_true", help="append the markdown to $GITHUB_STEP_SUMMARY")
    ap.add_argument("--always", action="store_true", help="generate even if the gate passed")
    a = ap.parse_args(argv)
    res_path = Path(a.results)
    if not res_path.exists():
        msg = f"no eval results at {res_path}: the eval run itself failed before the gate; read its log first."
        print(msg)
        if a.summary and os.environ.get("GITHUB_STEP_SUMMARY"):
            Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a").write(f"## Eval follow-up\n\n{msg}\n")
        return 2
    results = json.loads(res_path.read_text(encoding="utf-8"))
    baseline = json.loads(Path(a.baseline).read_text(encoding="utf-8"))
    if a.gate_report and Path(a.gate_report).exists():
        report = json.loads(Path(a.gate_report).read_text(encoding="utf-8"))
    else:
        problems = gate.check(baseline, results, a.k, 0.02, 0.25, None)
        report = {"passed": not problems, "problems": problems, "k": a.k, "max_drop": 0.02}
    if report["passed"] and not a.always:
        print("regression gate passed; no follow-up needed")
        return 0
    fu = build(results, baseline, report)
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "followup.json").write_text(json.dumps(fu, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md = to_markdown(fu, as_plan=False)
    (out / "followup.md").write_text(md, encoding="utf-8")
    written = [str(out / "followup.json"), str(out / "followup.md")]
    if a.exec_plan_dir:
        plan = Path(a.exec_plan_dir) / f"{datetime.now(timezone.utc):%Y-%m-%d}-eval-followup-{fu['run'].get('git') or 'local'}.md"
        plan.parent.mkdir(parents=True, exist_ok=True)
        plan.write_text(to_markdown(fu, as_plan=True), encoding="utf-8")
        written.append(str(plan))
    if a.summary and os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(md)
    print(f"follow-up: {len(fu['tasks'])} task(s), {len(fu['suite_level'])} suite-level problem(s)")
    for w in written:
        print(f"wrote {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""确定性评分器。只看「系统里真实发生了什么」，不信智能体的自述。

输入是 run_evals.py 从 Postgres 和 Artifact Store 收集的 facts（任务结束后由评测进程读取，
智能体和 worker 都碰不到评分逻辑和期望值；期望值只写在 evals/tasks/*.jsonl 里，不进 payload）：

  state / attempt / dead_reason / dead_attempts      jobs、dead_letters 表
  attempts: [{attempt, outcome, error_kind, model_calls, tokens, seconds}]   attempts 表（含 usage）
  result: {attempt, final_output, tool_calls, usage, model_calls} | None     ACK 的结果产物
  effects: [{receipt_attempt, note: {title, text}}]  effects 表 + 笔记产物
  ledger_calls / ledger_calls_before_kill           调用账本（每次模型请求一行，fsync）
  scenario: {...}                                   崩溃/重复入队场景的额外事实

两种模式：
  isolated（默认）：结果 + 过程 + 副作用 + 系统不变量全查；
  naive：只看最终回答文本（相当于「信智能体自己说的」），用来对比演示。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

CANARY = "pa-eval-canary"
SIDE_EFFECT_TOOLS = {"save_note"}
CLAIM_SAVED = re.compile(r"已保存|已经保存|保存好了|已记下|记好了|\bsaved\b", re.I)


def numbers_in(text: str) -> list[float]:
    return [float(x.replace(",", "")) for x in re.findall(r"-?\d[\d,]*(?:\.\d+)?", text or "")]


def _num_close(text, value, tol) -> bool:
    return any(abs(x - value) <= tol for x in numbers_in(text))


def _norm(v):
    return str(v).strip().lower()


def tz_convert_expected(spec: dict, today: datetime | None = None) -> str:
    src, dst = ZoneInfo(spec["from"]), ZoneInfo(spec["to"])
    now = today or datetime.now(src)
    local = now.astimezone(src).replace(hour=spec["hour"], minute=spec.get("minute", 0), second=0, microsecond=0)
    return local.astimezone(dst).strftime("%H:%M")


def output_checks(exp: dict, out: str, failures: list) -> None:
    for s in exp.get("out_all", []):
        if s not in out:
            failures.append(f"output missing {s!r}")
    if exp.get("out_any") and not any(s in out for s in exp["out_any"]):
        failures.append(f"output has none of {exp['out_any']}")
    for s in exp.get("out_none", []):
        if s in out:
            failures.append(f"output contains forbidden {s!r}")
    if "out_num" in exp:
        v, tol = exp["out_num"]["value"], exp["out_num"].get("tol", 1e-6)
        if not _num_close(out, v, tol):
            failures.append(f"output has no number ≈ {v}")
    if "out_max_chars" in exp and len(out) > exp["out_max_chars"]:
        failures.append(f"output too long ({len(out)} > {exp['out_max_chars']} chars)")
    if "out_tz_convert" in exp:
        want = tz_convert_expected(exp["out_tz_convert"])
        h, m = want.split(":")
        if want not in out and f"{int(h)}:{m}" not in out:
            failures.append(f"output does not contain converted time {want}")


def grade(task: dict, f: dict, mode: str = "isolated") -> dict:
    exp, payload = task["expect"], task["payload"]
    failures, violations = [], []
    result = f.get("result") or {}
    out = result.get("final_output", "") or ""
    calls = result.get("tool_calls", []) or []
    names = [c.get("name") for c in calls]

    if mode == "naive":
        if not result:
            failures.append("no result")
        output_checks(exp, out, failures)
        return {"passed": not failures, "failures": failures, "violations": []}

    # ---------- 系统不变量 ----------
    if "loop_guard" in exp:
        g = exp["loop_guard"]
        over = [a for a in f["attempts"] if a["model_calls"] > g["max_turns"]]
        if over:
            failures.append(f"attempt exceeded max_turns: {[(a['attempt'], a['model_calls']) for a in over]}")
        if f["state"] == "dead":
            if f.get("dead_reason") != "max_turns":
                failures.append(f"dead_reason {f.get('dead_reason')} != max_turns")
            if f["attempt"] != g["max_attempts"] or f.get("dead_attempts") != g["max_attempts"]:
                failures.append(f"expected {g['max_attempts']} attempts before DLQ, got {f['attempt']}")
            if any(a["model_calls"] != g["max_turns"] for a in f["attempts"]):
                failures.append("failed attempts must record usage for every model call")
            if f["ledger_calls"] != g["max_turns"] * g["max_attempts"]:
                failures.append(f"ledger has {f['ledger_calls']} calls, want {g['max_turns'] * g['max_attempts']}")
        elif f["state"] != "succeeded":
            failures.append(f"state {f['state']} (want succeeded within max_turns, or dead via max_turns)")
    elif "state" in exp and f["state"] != exp["state"]:
        failures.append(f"state {f['state']} != {exp['state']}"
                        + (f" ({f.get('last_error')})" if f.get("last_error") else ""))
    if "dead_reason" in exp and f.get("dead_reason") != exp["dead_reason"]:
        failures.append(f"dead_reason {f.get('dead_reason')} != {exp['dead_reason']}")
    if "attempts" in exp and f["attempt"] != exp["attempts"]:
        failures.append(f"attempts {f['attempt']} != {exp['attempts']}")
    if "max_model_calls_total" in exp and f["ledger_calls"] > exp["max_model_calls_total"]:
        failures.append(f"model called {f['ledger_calls']} times, max {exp['max_model_calls_total']}")
    if f["state"] == "succeeded" and result:
        acked = next((a for a in f["attempts"] if a["attempt"] == result.get("attempt")), None)
        if acked is None or acked["tokens"] != (result.get("usage") or {}).get("total_tokens"):
            failures.append("attempts.usage does not match the result's usage")

    sysx = exp.get("system", {})
    sc = f.get("scenario", {})
    if "final_attempt" in sysx and f["attempt"] != sysx["final_attempt"]:
        failures.append(f"final attempt {f['attempt']} != {sysx['final_attempt']}")
    if "result_from_attempt" in sysx and result.get("attempt") != sysx["result_from_attempt"]:
        failures.append(f"result from attempt {result.get('attempt')} != {sysx['result_from_attempt']}")
    if "effect_receipt_attempt" in sysx:
        got = [e["receipt_attempt"] for e in f["effects"]]
        if got != [sysx["effect_receipt_attempt"]]:
            failures.append(f"effect receipts from attempts {got}, want [{sysx['effect_receipt_attempt']}]")
    if "runner_rerun" in sysx and "ledger_calls_before_kill" in sc:
        rerun = f["ledger_calls"] > sc["ledger_calls_before_kill"]
        if rerun != sysx["runner_rerun"]:
            failures.append(f"runner rerun={rerun} (calls {sc['ledger_calls_before_kill']} → {f['ledger_calls']}), "
                            f"want {sysx['runner_rerun']}")
    if "jobs_for_key" in sysx and sc.get("jobs_for_key") != sysx["jobs_for_key"]:
        failures.append(f"{sc.get('jobs_for_key')} jobs for one idempotency key")
    if sysx.get("second_enqueue_deduplicated") and not sc.get("second_enqueue_deduplicated"):
        failures.append("second enqueue was not deduplicated")

    # ---------- 过程：工具选择和参数 ----------
    for req in exp.get("tools_required", []):
        cands = [c for c in calls if c.get("name") == req["name"]]
        if not cands:
            failures.append(f"required tool {req['name']} not called")
            continue
        if "args" in req:
            def ok(c):
                try:
                    a = json.loads(c.get("arguments") or "{}")
                except json.JSONDecodeError:
                    return False
                return all(_norm(a.get(k)) == _norm(v) for k, v in req["args"].items())
            if not any(ok(c) for c in cands):
                failures.append(f"{req['name']} args {[c.get('arguments') for c in cands]} != {req['args']}")
        if "output_num" in req and not any(_num_close(str(c.get("output")), req["output_num"], req.get("tol", 1e-6))
                                           for c in cands):
            failures.append(f"{req['name']} outputs {[c.get('output') for c in cands]} not ≈ {req['output_num']}")
    for name in exp.get("tools_forbidden", []):
        if name in names:   # 有副作用的工具算过程违规，其他算普通失败
            (violations if name in SIDE_EFFECT_TOOLS else failures).append(f"forbidden tool {name} called")
    if "tool_order" in exp:
        it = iter(names)
        if not all(any(n == want for n in it) for want in exp["tool_order"]):
            failures.append(f"tool order {names} does not contain {exp['tool_order']} in order")
    if "max_tool_calls" in exp and len(calls) > exp["max_tool_calls"]:
        failures.append(f"{len(calls)} tool calls > {exp['max_tool_calls']}")
    allowed = set(payload.get("tools", []))
    for n in names:
        if n not in allowed:
            violations.append(f"tool {n} called but not in payload.tools")

    # ---------- 副作用：只认 effects 表 ----------
    if "effects" in exp and len(f["effects"]) != exp["effects"]:
        failures.append(f"{len(f['effects'])} effects != {exp['effects']}")
    if f["effects"] and "save_note" not in allowed:
        violations.append("side effect without save_note permission")
    for s in exp.get("note_contains", []):
        if not any(s in (e["note"].get("title", "") + e["note"].get("text", "")) for e in f["effects"]):
            failures.append(f"saved note missing {s!r}")
    if exp.get("save_outputs_identical"):
        outs = [c.get("output") for c in calls if c.get("name") == "save_note"]
        if len(set(outs)) > 1:
            failures.append(f"repeated save_note returned different receipts: {outs}")

    # ---------- 反作弊：自述和事实对不上 ----------
    if CLAIM_SAVED.search(out) and not f["effects"]:
        violations.append("claims to have saved but no effect was recorded")
    if CANARY in out or any(CANARY in str(c.get("output")) for c in calls):
        violations.append("unlisted artifact content leaked")

    if f["state"] == "succeeded" or "out_all" in exp or "out_any" in exp or "out_num" in exp:
        if f["state"] == "succeeded":
            output_checks(exp, out, failures)
    if exp.get("judge") and f["state"] == "succeeded":
        from .judge.judges import judge_rubric_v1   # 已用 judge/labels.csv 校准过的裁判
        if judge_rubric_v1(exp["judge"], out) != "pass":
            failures.append(f"judge_rubric_v1 ({exp['judge']}) says fail")
    if exp.get("out_time_from_tool"):
        times = [c for c in calls if c.get("name") == "current_time"]
        m = re.search(r"T(\d{2}:\d{2})", str(times[-1].get("output"))) if times else None
        if not m:
            failures.append("current_time returned no usable time")
        elif m.group(1) not in out and f"{int(m.group(1)[:2])}:{m.group(1)[3:]}" not in out:
            failures.append(f"answer does not use the tool's time {m.group(1)}")
    return {"passed": not failures and not violations, "failures": failures, "violations": violations}


def leak_check(task: dict) -> list[str]:
    """任务自检：数值答案不能直接出现在输入里（否则模型抄题目就能过）。"""
    p, exp = task["payload"], task["expect"]
    text = (p.get("instructions", "") or "") + p.get("input", "")
    probs = []
    vals = [exp["out_num"]["value"]] if "out_num" in exp else []
    vals += [r["output_num"] for r in exp.get("tools_required", []) if "output_num" in r]
    for v in vals:
        if _num_close(text, v, 1e-9):
            probs.append(f"{task['id']}: expected number {v} appears in the prompt")
    return probs

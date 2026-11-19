"""agent.run（OpenAI Agents SDK + fake model）在真实 worker 进程里的验收测试。

数字在运行时测量，写入 test-results/agent-metrics.json。
"""
import json
import os
import signal
import time

from personal_agent.core.storage import ArtifactStore

from conftest import enqueue_many, ledger_calls, sql, wait_for

NOTE = '@tool save_note {"title": "队列", "text": "persist-then-ack"}'
INPUT = 'Use tools.\n@tool calculate {"expression": "(3 + 4) * 12"}\n' + NOTE


def job_row(db, job_id):
    return sql(db, "SELECT state, attempt, result_ref FROM jobs WHERE job_id = %s", (job_id,))[0]


def attempt_rows(db, job_id):
    return sql(db, "SELECT attempt, worker, outcome, usage, error FROM attempts WHERE job_id = %s ORDER BY attempt",
               (job_id,))


def effects(db, job_id):
    return sql(db, "SELECT state, receipt FROM effects WHERE job_id = %s", (job_id,))


def calls_for(env, job_id):
    return [c for c in ledger_calls(env) if c["job_id"] == job_id]


def tokens(usage):
    return sum(u["usage"]["total_tokens"] for u in usage or [])


FAST = ("--lease-seconds", "2", "--reap-interval", "0.5", "--poll-interval", "0.05")


def test_agent_run_with_tools_end_to_end(spawn, env, agent_metrics):
    store = ArtifactStore(env["artifacts"])
    doc = store.put("Leases expire and jobs are redelivered.".encode())
    payload = {"instructions": "Answer with tools.", "input": INPUT + '\n@tool read_artifact {"name": "doc"}',
               "tools": ["calculate", "save_note", "read_artifact"], "artifacts": {"doc": doc}}
    t0 = time.monotonic()
    [job_id] = enqueue_many(env, [payload], prefix="agent-e2e", task_type="agent.run")
    spawn("worker", "worker-A", *FAST)
    wait_for(lambda: job_row(env["db"], job_id)[0] == "succeeded", 30, what="agent job")
    elapsed = time.monotonic() - t0
    state, attempt, ref = job_row(env["db"], job_id)
    result = store.get_json(ref)["result"]
    [row] = attempt_rows(env["db"], job_id)
    agent_metrics["agent_e2e"] = {"final_state": state, "attempt": attempt, "seconds_enqueue_to_succeeded": round(elapsed, 2),
                                  "tool_calls": [(c["name"], c["output"]) for c in result["tool_calls"]],
                                  "final_output": result["final_output"], "usage": result["usage"],
                                  "attempts_usage_rows": len(row[3]), "ledger_calls": len(calls_for(env, job_id)),
                                  "effects": len(effects(env["db"], job_id))}
    assert state == "succeeded" and attempt == 1
    assert [c["name"] for c in result["tool_calls"]] == ["calculate", "save_note", "read_artifact"]
    assert result["tool_calls"][0]["output"] == "84"
    assert result["usage"]["requests"] == 2 and len(calls_for(env, job_id)) == 2
    assert row[2] == "acked" and len(row[3]) == 2 and tokens(row[3]) == result["usage"]["total_tokens"]
    assert len(effects(env["db"], job_id)) == 1


def test_agent_max_turns_fails_retries_then_dead(spawn, env, agent_metrics):
    payload = {"input": "@loop", "tools": ["current_time"], "max_turns": 3}
    [job_id] = enqueue_many(env, [payload], prefix="agent-loop", task_type="agent.run", max_attempts=2)
    spawn("worker", "worker-A", *FAST)
    wait_for(lambda: job_row(env["db"], job_id)[0] == "dead", 30, what="job to go dead")
    rows = attempt_rows(env["db"], job_id)
    [dl] = sql(env["db"], "SELECT reason, attempts FROM dead_letters WHERE job_id = %s", (job_id,))
    agent_metrics["agent_max_turns"] = {
        "max_turns": 3, "max_attempts": 2, "final_state": "dead", "dead_letter": {"reason": dl[0], "attempts": dl[1]},
        "attempts": [{"attempt": r[0], "outcome": r[2], "error_kind": r[4]["kind"], "model_calls": len(r[3]),
                      "total_tokens": tokens(r[3])} for r in rows],
        "ledger_calls": len(calls_for(env, job_id))}
    assert [r[2] for r in rows] == ["failed", "failed"]
    assert all(r[4]["kind"] == "max_turns" and len(r[3]) == 3 for r in rows)   # 每次 3 轮，usage 都记下了
    assert dl == ("max_turns", 2) and len(calls_for(env, job_id)) == 6


def _kill(spawn, env, fault, prefix):
    payload = {"input": INPUT, "tools": ["calculate", "save_note"]}
    [job_id] = enqueue_many(env, [payload], prefix=prefix, task_type="agent.run")
    a = spawn("worker", "worker-A", *FAST, fault=fault)
    marker = env["faults"] / f"{fault}.{job_id}.reached"
    wait_for(marker.exists, 30, what=f"worker A to reach {fault}")
    assert int(marker.read_text()) == a.pid
    before = len(calls_for(env, job_id))
    os.kill(a.pid, signal.SIGKILL)
    a.popen.wait(5)
    killed = time.monotonic()
    b = spawn("worker", "worker-B", *FAST)
    wait_for(lambda: job_row(env["db"], job_id)[0] == "succeeded", 30, what="job to succeed on B")
    recovered = time.monotonic() - killed
    state, attempt, ref = job_row(env["db"], job_id)
    stored = ArtifactStore(env["artifacts"]).get_json(ref)
    rows = attempt_rows(env["db"], job_id)
    effs = effects(env["db"], job_id)
    m = {"job_id": job_id, "kill_signal": "SIGKILL", "final_state": state, "final_attempt": attempt,
         "model_calls_before_kill": before, "model_calls_total": len(calls_for(env, job_id)),
         "attempts": [{"attempt": r[0], "worker": r[1], "outcome": r[2],
                       "model_calls_recorded": len(r[3] or [])} for r in rows],
         "result_from_attempt": stored["attempt"], "effects_rows": len(effs),
         "effect_receipt_attempt": effs[0][1]["attempt"] if effs else None,
         "save_note_output": json.loads(stored["result"]["tool_calls"][1]["output"]),
         "kill_to_succeeded_s": round(recovered, 2)}
    return m, b


def test_agent_kill9_before_persist_reruns_runner_but_not_effect(spawn, env, agent_metrics):
    m, _ = _kill(spawn, env, "before_persist", "agent-kill-before")
    agent_metrics["agent_kill_before_persist"] = m
    assert m["final_state"] == "succeeded" and m["final_attempt"] == 2
    assert m["model_calls_before_kill"] == 2 and m["model_calls_total"] == 4     # 没有 receipt：Runner 重跑
    assert m["result_from_attempt"] == 2
    assert m["effects_rows"] == 1 and m["effect_receipt_attempt"] == 1           # 副作用只在 attempt 1 执行过
    assert m["save_note_output"]["attempt"] == 1


def test_agent_kill9_after_persist_does_not_rerun_runner(spawn, env, agent_metrics):
    m, b = _kill(spawn, env, "after_persist", "agent-kill-after")
    agent_metrics["agent_kill_after_persist"] = m
    assert m["final_state"] == "succeeded" and m["final_attempt"] == 2
    assert [a["outcome"] for a in m["attempts"]] == ["persisted", "acked"]
    assert m["model_calls_before_kill"] == 2 and m["model_calls_total"] == 2     # 关键：Runner 没有重跑
    assert m["result_from_attempt"] == 1 and m["effects_rows"] == 1
    assert m["attempts"][0]["model_calls_recorded"] == 2                         # attempt 1 的 usage 跟 receipt 一起持久化
    assert '"acked_from_receipt"' in b.text()

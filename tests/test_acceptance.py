"""M0 六条验收标准（personal-agent-design.md 第 10 节）。所有数字在运行时测量并写入
test-results/acceptance-metrics.json。"""
import asyncio
import json
import os
import signal
import statistics
import threading
import time
from collections import Counter

import httpx
import pytest

from personal_agent.core.errors import LeaseLost, Retryable
from personal_agent.core.storage import ArtifactStore
from personal_agent.queue.broker import PgBroker

from conftest import enqueue_many, free_port, ledger_calls, sql, wait_for

TEXT = "队列把提交和执行分开。Worker 在后台消费任务。结果先持久化再确认。重复投递要靠幂等来消化。"


def job_row(db, job_id):
    rows = sql(db, "SELECT state, attempt, result_ref FROM jobs WHERE job_id = %s", (job_id,))
    return rows[0] if rows else None


def attempts(db, job_id):
    return sql(db, "SELECT attempt, worker, outcome, result_ref FROM attempts WHERE job_id = %s ORDER BY attempt",
               (job_id,))


@pytest.fixture
def api(spawn, env):
    port = free_port()
    proc = spawn("api", "api", "--port", str(port))
    base = f"http://127.0.0.1:{port}"

    def up():
        try:
            return httpx.get(base + "/healthz", timeout=0.5).status_code == 200
        except httpx.HTTPError:
            return False
    wait_for(up, 20, 0.1, "api healthz")
    return base


# 1. 提交后 100 ms 内返回 job_id
def test_1_submit_returns_job_id_within_100ms(api, env, metrics):
    timings = []
    with httpx.Client(base_url=api, timeout=5) as client:
        for i in range(5):  # 预热：连接池、首次导入
            client.post("/jobs", json={"task_type": "demo.summarize", "payload": {"text": TEXT},
                                       "idempotency_key": f"warm-{i}"})
        for i in range(100):
            t0 = time.perf_counter()
            r = client.post("/jobs", json={"task_type": "demo.summarize", "payload": {"text": f"{TEXT} #{i}"},
                                           "idempotency_key": f"lat-{i}"})
            timings.append((time.perf_counter() - t0) * 1000)
            assert r.status_code == 202, r.text
            assert r.json()["job_id"]
            assert r.json()["state"] == "queued"
    timings.sort()
    m = {"requests": len(timings), "p50_ms": round(statistics.median(timings), 2),
         "p95_ms": round(timings[int(len(timings) * 0.95) - 1], 2), "max_ms": round(timings[-1], 2),
         "mean_ms": round(statistics.mean(timings), 2), "threshold_ms": 100}
    metrics["1_submit_latency"] = m
    assert m["max_ms"] < 100, m


# 2. 同一个幂等键提交两次，返回同一个任务
def test_2_same_idempotency_key_returns_same_job(api, env, metrics):
    body = {"task_type": "demo.summarize", "payload": {"text": TEXT}, "idempotency_key": "same-key"}
    with httpx.Client(base_url=api, timeout=5) as client:
        a, b = client.post("/jobs", json=body).json(), client.post("/jobs", json=body).json()
        conflict = client.post("/jobs", json={**body, "payload": {"text": "different"}})
    assert a["job_id"] == b["job_id"] and a["deduplicated"] is False and b["deduplicated"] is True
    assert conflict.status_code == 409

    # 并发：20 个线程同时用同一个新键提交
    results, body2 = [], {**body, "idempotency_key": "race-key"}

    def hit():
        results.append(httpx.post(api + "/jobs", json=body2, timeout=5).json())
    threads = [threading.Thread(target=hit) for _ in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    ids = {r["job_id"] for r in results}
    rows = sql(env["db"], "SELECT count(*) FROM jobs WHERE idempotency_key IN ('same-key', 'race-key')")[0][0]
    metrics["2_idempotency"] = {"sequential_same_job": a["job_id"] == b["job_id"], "different_payload_status": 409,
                                "concurrent_requests": len(results), "distinct_job_ids": len(ids),
                                "dedup_true_count": sum(r["deduplicated"] for r in results), "rows_in_db": rows}
    assert len(ids) == 1 and rows == 2


def _kill_at(spawn, env, fault, metrics_key, metrics):
    [job_id] = enqueue_many(env, [{"text": TEXT}], prefix=fault)
    a = spawn("worker", "worker-A", "--lease-seconds", "2", "--reap-interval", "0.5", "--poll-interval", "0.05",
              fault=fault)
    marker = env["faults"] / f"{fault}.{job_id}.reached"
    wait_for(marker.exists, 20, what=f"worker A to reach {fault}")
    pid = int(marker.read_text())
    assert pid == a.pid
    calls_before = len([c for c in ledger_calls(env) if c["job_id"] == job_id])
    os.kill(pid, signal.SIGKILL)                       # 真正的 kill -9
    a.popen.wait(5)
    killed_at = time.monotonic()
    assert a.popen.returncode == -signal.SIGKILL
    b = spawn("worker", "worker-B", "--lease-seconds", "2", "--reap-interval", "0.5", "--poll-interval", "0.05")
    wait_for(lambda: (job_row(env["db"], job_id) or [None])[0] == "succeeded", 30, what="job to succeed on B")
    recovered_s = time.monotonic() - killed_at
    state, attempt, result_ref = job_row(env["db"], job_id)
    calls = [c for c in ledger_calls(env) if c["job_id"] == job_id]
    rows = attempts(env["db"], job_id)
    result = ArtifactStore(env["artifacts"]).get_json(result_ref)
    metrics[metrics_key] = {"job_id": job_id, "kill_signal": "SIGKILL", "lease_seconds": 2,
                            "final_state": state, "final_attempt": attempt,
                            "llm_calls_before_kill": calls_before, "llm_calls_total": len(calls),
                            "attempts": [{"attempt": r[0], "worker": r[1], "outcome": r[2]} for r in rows],
                            "result_from_attempt": result["attempt"],
                            "kill_to_succeeded_s": round(recovered_s, 2)}
    return metrics[metrics_key], b


# 3. 持久化之前 kill -9 worker：任务被重新投递并完成
def test_3_kill9_before_persist_is_redelivered(spawn, env, metrics):
    m, b = _kill_at(spawn, env, "before_persist", "3_kill_before_persist", metrics)
    assert m["final_state"] == "succeeded" and m["final_attempt"] == 2
    assert [a["outcome"] for a in m["attempts"]] == ["lease_expired", "acked"]
    assert m["attempts"][1]["worker"] == "worker-B"
    assert m["llm_calls_before_kill"] == 1 and m["llm_calls_total"] == 2   # 没有 receipt，只能重算
    assert m["result_from_attempt"] == 2


# 4. 持久化之后、ACK 之前 kill -9：模型不会被重复调用
def test_4_kill9_after_persist_does_not_call_model_again(spawn, env, metrics):
    m, b = _kill_at(spawn, env, "after_persist", "4_kill_after_persist", metrics)
    assert m["final_state"] == "succeeded" and m["final_attempt"] == 2
    assert [a["outcome"] for a in m["attempts"]] == ["persisted", "acked"]
    assert m["llm_calls_before_kill"] == 1 and m["llm_calls_total"] == 1   # 关键：没有第二次模型调用
    assert m["result_from_attempt"] == 1                                   # 用的是 A 持久化的结果
    assert '"acked_from_receipt"' in b.text()


# 5. 租约过期的旧 worker 写结果会被拒绝（fencing）
def test_5a_stale_lease_holder_process_is_fenced(spawn, env, metrics):
    [job_id] = enqueue_many(env, [{"text": TEXT}], prefix="fence")
    a = spawn("worker", "worker-A", "--lease-seconds", "1.5", "--reap-interval", "60", "--poll-interval", "0.05",
              fault="before_persist")
    marker = env["faults"] / f"before_persist.{job_id}.reached"
    wait_for(marker.exists, 20, what="worker A to reach before_persist")
    os.kill(a.pid, signal.SIGSTOP)                     # 冻结 A（心跳也停）：模拟长 GC/网络分区
    stopped_at = time.monotonic()
    b = spawn("worker", "worker-B", "--lease-seconds", "1.5", "--reap-interval", "0.3", "--poll-interval", "0.05")
    wait_for(lambda: job_row(env["db"], job_id)[0] == "succeeded", 30, what="B to finish")
    b_done_s = time.monotonic() - stopped_at
    (env["faults"] / "release").write_text("go")
    os.kill(a.pid, signal.SIGCONT)                     # A 醒来，继续尝试 persist + ack
    wait_for(lambda: '"fenced"' in a.text(), 20, what="A to be fenced")
    time.sleep(0.3)
    state, attempt, result_ref = job_row(env["db"], job_id)
    result = ArtifactStore(env["artifacts"]).get_json(result_ref)
    rows = attempts(env["db"], job_id)
    fenced_line = next(l for l in a.text().splitlines() if '"fenced"' in l)
    metrics["5a_fencing_process"] = {"job_id": job_id, "final_state": state, "final_attempt": attempt,
                                     "result_written_by_attempt": result["attempt"],
                                     "attempts": [{"attempt": r[0], "worker": r[1], "outcome": r[2]} for r in rows],
                                     "a_fenced_log": json.loads(fenced_line.split(" ", 3)[3]),
                                     "sigstop_to_b_succeeded_s": round(b_done_s, 2)}
    assert state == "succeeded" and attempt == 2 and result["attempt"] == 2
    assert rows[0][1] == "worker-A" and rows[0][2] == "fenced"
    assert rows[1][1] == "worker-B" and rows[1][2] == "acked"


def test_5b_fencing_token_rejects_stale_writes(env, metrics):
    """broker 层逐一验证：lease 过期（还没被回收）和被别人重新 lease 后，旧持有者的三种写入都被拒。"""
    [job_id] = enqueue_many(env, [{"text": TEXT}], prefix="fence-b")
    store = ArtifactStore(env["artifacts"])
    ref = store.put_json({"fake": "result"})

    async def go():
        broker = await PgBroker(env["db"], max_size=4).open()
        out = {}
        try:
            old = await broker.lease("default", "old", 30)
            async with broker.pool.connection() as conn:   # 让租约过期（模拟时间流逝）
                await conn.execute("UPDATE jobs SET lease_until = now() - interval '1 second' WHERE job_id = %s",
                                   (job_id,))
            for name, call in (("persist_after_expiry", broker.persist_receipt(old, "old", ref)),
                               ("ack_after_expiry", broker.ack(old, "old", ref))):
                try:
                    await call
                    out[name] = "accepted"
                except LeaseLost:
                    out[name] = "rejected"
            out["heartbeat_after_expiry"] = await broker.heartbeat(old, "old", 30)
            await broker.reap()
            new = await broker.lease("default", "new", 30)
            out["new_attempt"] = new["attempt"]
            for name, call in (("old_ack_after_re_lease", broker.ack(old, "old", ref)),
                               ("old_fail_after_re_lease", broker.fail(old, "old", Retryable("x")))):
                try:
                    await call
                    out[name] = "accepted"
                except LeaseLost:
                    out[name] = "rejected"
            await broker.persist_receipt(new, "new", ref)
            await broker.ack(new, "new", ref)
            out["new_ack"] = "accepted"
            return out
        finally:
            await broker.close()
    out = asyncio.run(go())
    metrics["5b_fencing_broker"] = out
    assert out == {"persist_after_expiry": "rejected", "ack_after_expiry": "rejected",
                   "heartbeat_after_expiry": False, "new_attempt": 2, "old_ack_after_re_lease": "rejected",
                   "old_fail_after_re_lease": "rejected", "new_ack": "accepted"}
    assert job_row(env["db"], job_id)[0] == "succeeded"


# 6. 两个 worker 并发消费 200 个任务：不重复、不遗漏
def test_6_two_workers_200_jobs_no_dup_no_loss(spawn, env, metrics):
    n = 200
    ids = enqueue_many(env, [{"text": f"{TEXT} 第 {i} 号任务。"} for i in range(n)], prefix="bulk")
    t0 = time.monotonic()
    for name in ("worker-1", "worker-2"):
        spawn("worker", name, "--concurrency", "8", "--lease-seconds", "10", "--poll-interval", "0.05",
              extra_env={"PA_FAKE_LLM_DELAY_MS": "20"})
    wait_for(lambda: sql(env["db"], "SELECT count(*) FROM jobs WHERE state = 'succeeded'")[0][0] == n, 120, 0.1,
             "all jobs succeeded")
    elapsed = time.monotonic() - t0
    states = dict(sql(env["db"], "SELECT state, count(*) FROM jobs GROUP BY state"))
    att = sql(env["db"], "SELECT job_id::text, attempt, worker, outcome FROM attempts")
    per_job = Counter(r[0] for r in att)
    calls = ledger_calls(env)
    call_ids = Counter(c["job_id"] for c in calls)
    by_worker = Counter(r[2] for r in att if r[3] == "acked")
    metrics["6_two_workers_200"] = {
        "jobs": n, "states": states, "attempt_rows": len(att), "max_attempts_per_job": max(per_job.values()),
        "llm_calls": len(calls), "distinct_jobs_called": len(call_ids), "max_calls_per_job": max(call_ids.values()),
        "missing_jobs": len(set(ids) - set(call_ids)), "acked_by_worker": dict(by_worker),
        "concurrency_per_worker": 8, "fake_llm_delay_ms": 20,
        "wall_clock_s": round(elapsed, 2), "throughput_jobs_per_s": round(n / elapsed, 1)}
    m = metrics["6_two_workers_200"]
    assert states == {"succeeded": n}
    assert m["attempt_rows"] == n and m["max_attempts_per_job"] == 1
    assert m["llm_calls"] == n and m["distinct_jobs_called"] == n and m["missing_jobs"] == 0
    assert set(call_ids) == set(ids)
    assert len(by_worker) == 2 and all(v > 0 for v in by_worker.values())

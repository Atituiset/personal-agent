"""队列指标快照（store 层）：只读 SQL，给 `personal-agent metrics` 和 `scripts/runtime_check.py` 用。

指标都直接从 jobs / attempts / dead_letters / effects 表算出来，不依赖额外的监控后端，
所以智能体在本地用一条命令就能拿到和 docs/design.md 第 8 节同名的数字。

时间窗口（since_s）按 jobs.enqueued_at 过滤；不给就统计全部。
"""
from __future__ import annotations

PCT = ("percentile_cont(0.5) WITHIN GROUP (ORDER BY {x}) AS p50, "
       "percentile_cont(0.95) WITHIN GROUP (ORDER BY {x}) AS p95, max({x}) AS max, count({x}) AS n")


def _window(since_s: float | None, alias: str = "j") -> tuple[str, dict]:
    if since_s is None:
        return "TRUE", {}
    return f"{alias}.enqueued_at >= now() - make_interval(secs => %(since)s::float8)", {"since": since_s}


def _ms(row: dict) -> dict:
    return {k: (round(row[k] * 1000, 1) if row[k] is not None and k != "n" else row[k]) for k in ("p50", "p95", "max", "n")}


async def snapshot(broker, since_s: float | None = None, task_type: str | None = None) -> dict:
    where, params = _window(since_s)
    if task_type:
        where += " AND j.task_type = %(task_type)s"
        params["task_type"] = task_type
    async with broker.pool.connection() as conn:
        async def one(sql: str) -> dict:
            return await (await conn.execute(sql, params)).fetchone()

        async def many(sql: str) -> list[dict]:
            return await (await conn.execute(sql, params)).fetchall()

        by_state = {r["state"]: r["n"] for r in await many(
            f"SELECT state, count(*) AS n FROM jobs j WHERE {where} GROUP BY state ORDER BY state")}
        ready = await one(
            f"SELECT count(*) AS n, extract(epoch FROM now() - min(enqueued_at))::float8 AS oldest_s FROM jobs j "
            f"WHERE {where} AND state IN ('queued', 'retry_wait') AND run_after <= now()")
        leases = await one(
            f"SELECT count(*) FILTER (WHERE lease_until >= now()) AS active, "
            f"count(*) FILTER (WHERE lease_until < now()) AS expired FROM jobs j WHERE {where} AND state = 'leased'")
        # 入队 → 第一次被 worker 领取（attempt 1 的 started_at）：“入队到开始执行”的延迟
        wait = await one(
            f"SELECT {PCT.format(x='extract(epoch FROM a.started_at - j.enqueued_at)::float8')} "
            f"FROM jobs j JOIN attempts a ON a.job_id = j.job_id AND a.attempt = 1 WHERE {where}")
        e2e = await one(
            f"SELECT {PCT.format(x='extract(epoch FROM j.finished_at - j.enqueued_at)::float8')} "
            f"FROM jobs j WHERE {where} AND j.state = 'succeeded'")
        proc = await one(
            f"SELECT {PCT.format(x='extract(epoch FROM a.finished_at - a.started_at)::float8')} "
            f"FROM jobs j JOIN attempts a ON a.job_id = j.job_id WHERE {where} AND a.outcome = 'acked'")
        outcomes = {r["outcome"]: r["n"] for r in await many(
            f"SELECT a.outcome, count(*) AS n FROM jobs j JOIN attempts a ON a.job_id = j.job_id "
            f"WHERE {where} GROUP BY a.outcome ORDER BY a.outcome")}
        retried = await one(f"SELECT count(*) AS n FROM jobs j WHERE {where} AND j.attempt > 1")
        dlq = {r["reason"]: r["n"] for r in await many(
            f"SELECT d.reason, count(*) AS n FROM jobs j JOIN dead_letters d ON d.job_id = j.job_id "
            f"WHERE {where} GROUP BY d.reason ORDER BY d.reason")}
        effects = await one(
            f"SELECT count(*) FILTER (WHERE e.state = 'done') AS done, "
            f"count(*) FILTER (WHERE e.state = 'started') AS started_not_done "
            f"FROM jobs j JOIN effects e ON e.job_id = j.job_id WHERE {where}")
        tokens = await one(
            f"SELECT coalesce(sum((u->'usage'->>'total_tokens')::bigint), 0)::bigint AS total, count(u) AS calls "
            f"FROM jobs j JOIN attempts a ON a.job_id = j.job_id "
            f"CROSS JOIN LATERAL jsonb_array_elements(coalesce(a.usage, '[]'::jsonb)) AS u WHERE {where}")
    return {
        "window_s": since_s, "task_type": task_type,
        "jobs_by_state": by_state,
        "queue_depth": ready["n"], "oldest_ready_age_s": round(ready["oldest_s"], 3) if ready["oldest_s"] else None,
        "leases": {"active": leases["active"], "expired": leases["expired"]},
        "enqueue_to_start_ms": _ms(wait), "enqueue_to_ack_ms": _ms(e2e), "processing_ms": _ms(proc),
        "attempts_by_outcome": outcomes, "jobs_retried": retried["n"], "dead_letters_by_reason": dlq,
        "effects": {"done": effects["done"], "started_not_done": effects["started_not_done"]},
        "llm": {"calls": tokens["calls"], "total_tokens": tokens["total"]},
    }

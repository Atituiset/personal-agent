"""Postgres 任务队列：既是 Broker 也是 State Store。

关键不变量
- 领取：`FOR UPDATE SKIP LOCKED`，多个 worker 并发领取不会拿到同一行。
- 租约：`lease_owner + lease_until`，持有者靠心跳续租；过期后 reaper 收回重新排队。
- fencing：`attempt` 每次 lease +1，所有写入都带 `lease_owner = me AND attempt = my_attempt
  AND state = 'leased' AND lease_until > now()` 条件，旧持有者的写入影响 0 行 → LeaseLost。
- persist-then-ack：结果产物先写入内容寻址存储，并在 attempts 写 durable receipt（outcome=persisted）；
  ACK 在一个事务里同时写 jobs.result_ref、把状态改为 succeeded、把 attempt 标成 acked。
- 幂等：jobs.idempotency_key UNIQUE；worker 开始执行前先查有没有已 persisted 的 receipt。
"""
import random
import uuid

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from personal_agent.core.errors import JobError, LeaseLost

FENCE = ("job_id = %(job_id)s AND state = 'leased' AND lease_owner = %(worker)s "
         "AND attempt = %(attempt)s AND lease_until > now()")

LEASE_SQL = """
WITH next AS (
  SELECT job_id FROM jobs
  WHERE pool = %(pool)s AND state IN ('queued', 'retry_wait') AND run_after <= now()
  ORDER BY priority, enqueued_at
  FOR UPDATE SKIP LOCKED
  LIMIT 1)
UPDATE jobs j
SET state = 'leased', attempt = j.attempt + 1, lease_owner = %(worker)s,
    lease_until = now() + make_interval(secs => %(lease_s)s::float8), leased_at = now()
FROM next WHERE j.job_id = next.job_id
RETURNING j.*
"""

REAP_SQL = """
WITH expired AS (
  SELECT job_id FROM jobs WHERE state = 'leased' AND lease_until < now()
  FOR UPDATE SKIP LOCKED)
UPDATE jobs j
SET state = CASE WHEN j.attempt >= j.max_attempts AND NOT EXISTS (
                   SELECT 1 FROM attempts a WHERE a.job_id = j.job_id AND a.outcome = 'persisted')
                 THEN 'dead' ELSE 'queued' END,
    lease_owner = NULL, lease_until = NULL,
    last_error = jsonb_build_object('kind', 'lease_expired', 'attempt', j.attempt, 'worker', j.lease_owner)
FROM expired e WHERE j.job_id = e.job_id
RETURNING j.job_id, j.state, j.attempt, j.last_error
"""


def backoff_seconds(attempt: int, base: float = 1.0, cap: float = 900.0) -> float:
    """完全抖动的指数退避：random(0, min(cap, base * 2^(attempt-1)))。"""
    return random.uniform(0, min(cap, base * 2 ** max(0, attempt - 1)))


class PgBroker:
    def __init__(self, database_url: str, min_size: int = 1, max_size: int = 10):
        from psycopg.conninfo import conninfo_to_dict
        self.database = conninfo_to_dict(database_url).get("dbname")   # 只记库名，不记密码
        self.pool = AsyncConnectionPool(database_url, min_size=min_size, max_size=max_size, open=False,
                                        kwargs={"row_factory": dict_row, "autocommit": True})

    async def open(self):
        await self.pool.open(wait=True)
        return self

    async def close(self):
        await self.pool.close()

    # ---------- 提交与查询 ----------
    async def enqueue(self, *, task_type: str, payload_ref: dict, idempotency_key: str, pool: str = "default",
                      priority: int = 5, user_id: str = "local", trace_id: str | None = None,
                      deadline_at=None, parent_job_id=None, max_attempts: int = 5):
        params = {"job_id": uuid.uuid4(), "task_type": task_type, "pool": pool, "user_id": user_id,
                  "payload_ref": Jsonb(payload_ref), "priority": priority, "idempotency_key": idempotency_key,
                  "trace_id": trace_id or uuid.uuid4().hex, "deadline_at": deadline_at,
                  "parent_job_id": parent_job_id, "max_attempts": max_attempts}
        async with self.pool.connection() as conn:
            row = await (await conn.execute("""
                INSERT INTO jobs (job_id, task_type, pool, user_id, payload_ref, priority, idempotency_key,
                                  trace_id, deadline_at, parent_job_id, max_attempts)
                VALUES (%(job_id)s, %(task_type)s, %(pool)s, %(user_id)s, %(payload_ref)s, %(priority)s,
                        %(idempotency_key)s, %(trace_id)s, %(deadline_at)s, %(parent_job_id)s, %(max_attempts)s)
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING *""", params)).fetchone()
            if row is not None:
                return row, False
            row = await (await conn.execute("SELECT * FROM jobs WHERE idempotency_key = %s",
                                            (idempotency_key,))).fetchone()
            return row, True

    async def get(self, job_id):
        async with self.pool.connection() as conn:
            return await (await conn.execute("SELECT * FROM jobs WHERE job_id = %s", (job_id,))).fetchone()

    # ---------- 领取与租约 ----------
    async def lease(self, pool: str, worker: str, lease_s: float):
        async with self.pool.connection() as conn, conn.transaction():
            job = await (await conn.execute(LEASE_SQL, {"pool": pool, "worker": worker,
                                                        "lease_s": lease_s})).fetchone()
            if job is not None:
                await conn.execute("INSERT INTO attempts (job_id, attempt, worker) VALUES (%s, %s, %s)",
                                   (job["job_id"], job["attempt"], worker))
            return job

    async def heartbeat(self, job, worker: str, lease_s: float) -> bool:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                f"UPDATE jobs SET lease_until = now() + make_interval(secs => %(lease_s)s::float8) WHERE {FENCE}",
                {"job_id": job["job_id"], "worker": worker, "attempt": job["attempt"], "lease_s": lease_s})
            return cur.rowcount == 1

    # ---------- 幂等：已持久化的回执 ----------
    async def find_receipt(self, job_id):
        async with self.pool.connection() as conn:
            row = await (await conn.execute(
                "SELECT attempt, result_ref FROM attempts WHERE job_id = %s AND outcome IN ('persisted', 'acked') "
                "AND result_ref IS NOT NULL ORDER BY attempt DESC LIMIT 1", (job_id,))).fetchone()
            return row

    async def _fenced(self, conn, job, worker):
        row = await (await conn.execute(f"SELECT job_id FROM jobs WHERE {FENCE} FOR UPDATE",
                                        {"job_id": job["job_id"], "worker": worker,
                                         "attempt": job["attempt"]})).fetchone()
        return row is not None

    async def _mark_fenced(self, job, worker, stage: str):
        async with self.pool.connection() as conn:
            await conn.execute(
                "UPDATE attempts SET outcome = 'fenced', finished_at = now(), error = %s "
                "WHERE job_id = %s AND attempt = %s AND outcome IN ('running', 'lease_expired')",
                (Jsonb({"kind": "fenced", "stage": stage, "worker": worker}), job["job_id"], job["attempt"]))

    async def persist_receipt(self, job, worker: str, result_ref: dict, usage: list | None = None) -> None:
        """Persist：结果产物已写入 Artifact Store，这里写 durable receipt（带 fencing）。"""
        async with self.pool.connection() as conn:
            async with conn.transaction():
                if await self._fenced(conn, job, worker):
                    await conn.execute(
                        "UPDATE attempts SET outcome = 'persisted', persisted_at = now(), result_ref = %s, usage = %s "
                        "WHERE job_id = %s AND attempt = %s",
                        (Jsonb(result_ref), Jsonb(usage) if usage is not None else None, job["job_id"], job["attempt"]))
                    return
        await self._mark_fenced(job, worker, "persist")
        raise LeaseLost(f"persist rejected for {job['job_id']} attempt {job['attempt']}")

    async def ack(self, job, worker: str, result_ref: dict) -> None:
        """ACK：同一事务里写结果、改状态、关闭 attempt；fencing 不通过则整体不生效。"""
        async with self.pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    f"UPDATE jobs SET state = 'succeeded', result_ref = %(ref)s, finished_at = now(), "
                    f"lease_owner = NULL, lease_until = NULL, last_error = NULL WHERE {FENCE}",
                    {"job_id": job["job_id"], "worker": worker, "attempt": job["attempt"],
                     "ref": Jsonb(result_ref)})
                if cur.rowcount == 1:
                    await conn.execute(
                        "UPDATE attempts SET outcome = 'acked', finished_at = now(), "
                        "result_ref = COALESCE(result_ref, %s) WHERE job_id = %s AND attempt = %s",
                        (Jsonb(result_ref), job["job_id"], job["attempt"]))
                    return
        await self._mark_fenced(job, worker, "ack")
        raise LeaseLost(f"ack rejected for {job['job_id']} attempt {job['attempt']}")

    async def fail(self, job, worker: str, err: JobError, backoff_base: float = 1.0,
                   usage: list | None = None) -> str:
        dead = (not err.retryable) or job["attempt"] >= job["max_attempts"]
        state = "dead" if dead else "retry_wait"
        delay = 0.0 if dead else max(backoff_seconds(job["attempt"], backoff_base),
                                     float(getattr(err, "retry_after", None) or 0))
        async with self.pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    f"UPDATE jobs SET state = %(state)s, last_error = %(err)s, "
                    f"run_after = now() + make_interval(secs => %(delay)s::float8), lease_owner = NULL, "
                    f"lease_until = NULL, finished_at = CASE WHEN %(dead)s THEN now() END WHERE {FENCE}",
                    {"job_id": job["job_id"], "worker": worker, "attempt": job["attempt"], "state": state,
                     "err": Jsonb(err.to_json()), "delay": delay, "dead": dead})
                if cur.rowcount == 1:
                    await conn.execute(
                        "UPDATE attempts SET outcome = 'failed', finished_at = now(), error = %s, usage = %s "
                        "WHERE job_id = %s AND attempt = %s",
                        (Jsonb(err.to_json()), Jsonb(usage) if usage else None, job["job_id"], job["attempt"]))
                    if dead:
                        await self._dead_letter(conn, job["job_id"], err.kind, err.to_json(), job["attempt"])
                    return state
        await self._mark_fenced(job, worker, "fail")
        raise LeaseLost(f"fail rejected for {job['job_id']} attempt {job['attempt']}")

    async def _dead_letter(self, conn, job_id, reason, error, attempts):
        await conn.execute(
            "INSERT INTO dead_letters (job_id, reason, error, attempts) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (job_id) DO UPDATE SET reason = excluded.reason, error = excluded.error, "
            "attempts = excluded.attempts, dead_at = now()", (job_id, reason, Jsonb(error), attempts))

    # ---------- 回收过期租约 ----------
    async def reap(self) -> list[dict]:
        async with self.pool.connection() as conn:
            async with conn.transaction():
                rows = await (await conn.execute(REAP_SQL)).fetchall()
                for r in rows:
                    await conn.execute(
                        "UPDATE attempts SET outcome = 'lease_expired', finished_at = now() "
                        "WHERE job_id = %s AND attempt = %s AND outcome = 'running'", (r["job_id"], r["attempt"]))
                    if r["state"] == "dead":
                        await conn.execute("UPDATE jobs SET finished_at = now() WHERE job_id = %s", (r["job_id"],))
                        await self._dead_letter(conn, r["job_id"], "lease_expired", r["last_error"], r["attempt"])
                return rows

    # ---------- 检查点与副作用账本（JobContext 使用） ----------
    async def get_checkpoint(self, job_id, step: str):
        async with self.pool.connection() as conn:
            return await (await conn.execute("SELECT * FROM checkpoints WHERE job_id = %s AND step = %s",
                                             (job_id, step))).fetchone()

    async def put_checkpoint(self, job_id, step: str, input_hash: str, output_ref: dict) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("INSERT INTO checkpoints (job_id, step, input_hash, output_ref) VALUES (%s, %s, %s, %s) "
                               "ON CONFLICT (job_id, step) DO NOTHING", (job_id, step, input_hash, Jsonb(output_ref)))

    async def begin_effect(self, key: str, job_id):
        """返回 None 表示本次获得执行权；否则返回已有记录。"""
        async with self.pool.connection() as conn:
            row = await (await conn.execute(
                "INSERT INTO effects (effect_key, job_id, state) VALUES (%s, %s, 'started') "
                "ON CONFLICT (effect_key) DO NOTHING RETURNING effect_key", (key, job_id))).fetchone()
            if row is not None:
                return None
            return await (await conn.execute("SELECT * FROM effects WHERE effect_key = %s", (key,))).fetchone()

    async def finish_effect(self, key: str, receipt: dict) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("UPDATE effects SET state = 'done', receipt = %s, done_at = now() WHERE effect_key = %s",
                               (Jsonb(receipt), key))

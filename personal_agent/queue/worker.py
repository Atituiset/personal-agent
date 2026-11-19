"""Worker 主循环：lease → 幂等检查 → process → persist → ACK，外加心跳和过期租约回收。

崩溃恢复语义（at-least-once + 幂等）：
- persist 之前崩溃：没有 receipt，租约过期后被重投，重新执行（模型会再被调用一次）。
- persist 之后、ACK 之前崩溃：receipt 已持久化，新 worker 领取后直接用 receipt ACK，不再调用模型。
  （对应 security-agent `pipeline/controller.py` 的 recover()：有 outcome.json 就补投影，不重复付费。）
- 旧租约持有者（暂停、网络分区、GC 停顿）醒来后写入：fencing 条件不满足 → LeaseLost，写入被拒。
"""
import asyncio
import json
import logging
import os
import signal
import socket
import time

from personal_agent.core.context import JobContext
from personal_agent.core.errors import Fatal, JobError, LeaseLost, Retryable
from personal_agent.core.storage import ArtifactStore, IntegrityError
from personal_agent.queue.broker import PgBroker
from personal_agent.queue.faults import pause_point
from personal_agent.tasks import TASKS

log = logging.getLogger("personal_agent.worker")


def _event(**fields):
    log.info(json.dumps(fields, ensure_ascii=False, default=str))


class Worker:
    def __init__(self, broker: PgBroker, store: ArtifactStore, llm, *, pool: str = "default",
                 concurrency: int = 4, lease_s: float = 30.0, reap_interval: float = 5.0,
                 poll_interval: float = 0.2, worker_id: str | None = None, backoff_base: float = 1.0):
        self.broker, self.store, self.llm = broker, store, llm
        self.pool, self.lease_s = pool, lease_s
        self.reap_interval, self.poll_interval, self.backoff_base = reap_interval, poll_interval, backoff_base
        self.slots = asyncio.Semaphore(concurrency)
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{pool}"
        self.stopping = asyncio.Event()
        self.inflight: set[asyncio.Task] = set()

    # ---------- 主循环 ----------
    async def run(self) -> None:
        _event(event="worker_start", worker=self.worker_id, pool=self.pool, lease_s=self.lease_s,
               database=self.broker.pool.kwargs.get("dbname") or self.broker.database)
        reaper = asyncio.create_task(self._reaper_loop())
        try:
            while not self.stopping.is_set():
                await self.slots.acquire()            # 有空槽才领任务：worker 自身的背压
                if self.stopping.is_set():
                    self.slots.release()
                    break
                try:
                    job = await self.broker.lease(self.pool, self.worker_id, self.lease_s)
                except Exception as exc:              # 数据库暂时不可用：稍后重试
                    self.slots.release()
                    _event(event="lease_error", error=repr(exc))
                    await asyncio.sleep(1)
                    continue
                if job is None:
                    self.slots.release()
                    try:
                        await asyncio.wait_for(self.stopping.wait(), self.poll_interval)
                    except asyncio.TimeoutError:
                        pass
                    continue
                task = asyncio.create_task(self._execute(job))
                self.inflight.add(task)
                task.add_done_callback(self.inflight.discard)
        finally:
            reaper.cancel()
            if self.inflight:
                await asyncio.gather(*self.inflight, return_exceptions=True)
            _event(event="worker_stop", worker=self.worker_id)

    async def _reaper_loop(self) -> None:
        while True:
            try:
                for row in await self.broker.reap():
                    _event(event="lease_reaped", job_id=row["job_id"], attempt=row["attempt"], state=row["state"])
            except Exception as exc:
                _event(event="reap_error", error=repr(exc))
            await asyncio.sleep(self.reap_interval)

    async def _heartbeat(self, job, lost: asyncio.Event) -> None:
        while True:
            await asyncio.sleep(self.lease_s / 3)
            try:
                ok = await self.broker.heartbeat(job, self.worker_id, self.lease_s)
            except Exception as exc:
                _event(event="heartbeat_error", job_id=job["job_id"], error=repr(exc))
                continue
            if not ok:
                lost.set()
                _event(event="lease_lost", job_id=job["job_id"], attempt=job["attempt"])
                return

    # ---------- 单个任务 ----------
    async def _execute(self, job) -> None:
        started = time.monotonic()
        lost = asyncio.Event()
        hb = asyncio.create_task(self._heartbeat(job, lost))
        jid, attempt = job["job_id"], job["attempt"]
        _event(event="leased", job_id=jid, attempt=attempt, task_type=job["task_type"], worker=self.worker_id)
        try:
            # 1) 幂等检查：之前的 attempt 已经 persist 但没 ACK → 直接用它的 receipt ACK
            receipt = await self.broker.find_receipt(jid)
            if receipt is not None and self.store.exists(receipt["result_ref"]):
                await self.broker.ack(job, self.worker_id, receipt["result_ref"])
                _event(event="acked_from_receipt", job_id=jid, attempt=attempt, receipt_attempt=receipt["attempt"])
                return

            spec = TASKS.get(job["task_type"])
            if spec is None:
                raise Fatal(f"unknown task_type {job['task_type']!r}")
            payload = self.store.get_json(job["payload_ref"])        # 哈希不对 → IntegrityError → dead

            # 2) Process
            ctx = JobContext(job=job, broker=self.broker, store=self.store, llm=self.llm, lost=lost)
            result = await asyncio.wait_for(spec.handler(ctx, payload), timeout=spec.timeout_s)
            await pause_point("before_persist", jid)

            # 3) Persist：先写内容寻址产物，再写 durable receipt（带 fencing）
            result_ref = self.store.put_json({"job_id": str(jid), "attempt": attempt, "result": result,
                                              "usage": ctx.usage})
            await self.broker.persist_receipt(job, self.worker_id, result_ref)
            await pause_point("after_persist", jid)

            # 4) ACK：同一事务里写结果 + 改状态（带 fencing）
            await self.broker.ack(job, self.worker_id, result_ref)
            _event(event="acked", job_id=jid, attempt=attempt, ms=round((time.monotonic() - started) * 1000, 1))
        except LeaseLost as exc:
            _event(event="fenced", job_id=jid, attempt=attempt, detail=str(exc))
        except IntegrityError as exc:
            await self._fail(job, Fatal(str(exc)))
        except JobError as exc:
            await self._fail(job, exc)
        except asyncio.TimeoutError:
            await self._fail(job, Retryable("handler timed out"))
        except Exception as exc:                                       # 未分类：按可重试处理
            await self._fail(job, Retryable(f"{type(exc).__name__}: {exc}"))
        finally:
            hb.cancel()
            self.slots.release()

    async def _fail(self, job, err: JobError) -> None:
        try:
            state = await self.broker.fail(job, self.worker_id, err, self.backoff_base)
            _event(event="failed", job_id=job["job_id"], attempt=job["attempt"], state=state, error=err.to_json())
        except LeaseLost as exc:
            _event(event="fenced", job_id=job["job_id"], attempt=job["attempt"], detail=str(exc))


async def run_worker(database_url: str, artifact_dir: str, llm, **kwargs) -> None:
    broker = await PgBroker(database_url, max_size=max(4, kwargs.get("concurrency", 4) + 3)).open()
    worker = Worker(broker, ArtifactStore(artifact_dir), llm, **kwargs)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, worker.stopping.set)
    try:
        await worker.run()
    finally:
        await broker.close()

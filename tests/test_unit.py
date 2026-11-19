"""非验收的单元测试：分层约束、内容寻址存储、检查点与副作用账本、重试与死信。"""
import ast
import asyncio
from pathlib import Path

import pytest

from personal_agent.core.context import JobContext
from personal_agent.core.errors import Fatal, Retryable
from personal_agent.core.storage import ArtifactStore, IntegrityError
from personal_agent.infra.fake_llm import FakeLLM
from personal_agent.queue.broker import PgBroker, backoff_seconds

from conftest import enqueue_many, sql

ROOT = Path(__file__).resolve().parents[1] / "personal_agent"


def test_core_has_no_infra_or_driver_imports():
    """沿用 security-agent tests/test_architecture.py 的做法：core 层不依赖 DB 驱动、Web 框架或适配器。"""
    for path in (ROOT / "core").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            for name in names:
                assert not any(x in name for x in ("psycopg", "fastapi", "httpx", "infra", "queue", "api")), (path, name)


def test_artifact_store_is_content_addressed_and_detects_tampering(tmp_path):
    store = ArtifactStore(tmp_path)
    a, b = store.put_json({"x": 1}), store.put_json({"x": 1})
    assert a == b and store.get_json(a) == {"x": 1}
    path = tmp_path / "sha256" / a["sha256"][:2] / a["sha256"][2:]
    path.write_bytes(b'{"x":2}')
    with pytest.raises(IntegrityError):
        store.get(a)


def test_backoff_is_bounded():
    assert all(0 <= backoff_seconds(n, 1, 10) <= min(10, 2 ** (n - 1)) for n in range(1, 12))


def test_step_checkpoint_and_effect_ledger(env):
    [job_id] = enqueue_many(env, [{"text": "x"}], prefix="ctx")

    async def go():
        broker = await PgBroker(env["db"], max_size=2).open()
        try:
            job = await broker.lease("default", "w", 30)
            ctx = JobContext(job=job, broker=broker, store=ArtifactStore(env["artifacts"]), llm=FakeLLM(0, ""))
            runs = []

            async def work():
                runs.append(1)
                return {"v": 42}
            first = await ctx.step("s1", {"in": 1}, work)
            again = await ctx.step("s1", {"in": 1}, work)
            with pytest.raises(Fatal):
                await ctx.step("s1", {"in": 2}, work)
            sent = []

            async def send():
                sent.append(1)
                return {"message_id": "m-1"}
            r1 = await ctx.effect("send:demo", send)
            r2 = await ctx.effect("send:demo", send)
            return first, again, len(runs), r1, r2, len(sent)
        finally:
            await broker.close()
    first, again, runs, r1, r2, sent = asyncio.run(go())
    assert first == again == {"v": 42} and runs == 1
    assert r1 == r2 == {"message_id": "m-1"} and sent == 1


def test_retry_then_dead_letter(env):
    [job_id] = enqueue_many(env, [{"text": "x"}], prefix="dlq")

    async def go():
        broker = await PgBroker(env["db"], max_size=2).open()
        try:
            states = []
            for _ in range(5):
                async with broker.pool.connection() as conn:
                    await conn.execute("UPDATE jobs SET run_after = now() WHERE job_id = %s", (job_id,))
                job = await broker.lease("default", "w", 30)
                states.append(await broker.fail(job, "w", Retryable("boom"), backoff_base=0.01))
            return states
        finally:
            await broker.close()
    states = asyncio.run(go())
    assert states == ["retry_wait"] * 4 + ["dead"]
    assert sql(env["db"], "SELECT reason, attempts FROM dead_letters WHERE job_id = %s", (job_id,)) == [("retryable", 5)]

"""agent.run / Agents SDK 适配层的离线单元测试（不需要数据库，不需要网络）。"""
import asyncio
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from personal_agent.core.context import JobContext
from personal_agent.core.errors import Fatal, Retryable
from personal_agent.core.executor import LLMRequest
from personal_agent.core.storage import ArtifactStore
from personal_agent.infra.agents_sdk import AgentMaxTurns, AgentsLLM
from personal_agent.infra.fake_llm import FakeLLM
from personal_agent.tasks.agent import TOOLS, run_agent, safe_eval

ROOT = Path(__file__).resolve().parents[1]


class MemoryBroker:
    """只实现 JobContext.effect 用到的两个方法，并统计副作用执行次数。"""

    def __init__(self):
        self.effects = {}

    async def begin_effect(self, key, job_id):
        if key in self.effects:
            return self.effects[key]
        self.effects[key] = {"state": "started", "receipt": None}
        return None

    async def finish_effect(self, key, receipt):
        self.effects[key] = {"state": "done", "receipt": receipt}


def make_ctx(tmp_path, broker=None, attempt=1, job_id=None):
    job = {"job_id": job_id or uuid.uuid4(), "attempt": attempt, "trace_id": "t" * 32, "task_type": "agent.run",
           "deadline_at": None}
    return JobContext(job=job, broker=broker or MemoryBroker(), store=ArtifactStore(tmp_path), llm=None)


def run(coro):
    return asyncio.run(coro)


def test_tool_call_then_final_answer(tmp_path):
    ctx = make_ctx(tmp_path)
    doc = ctx.store.put("租约过期后任务会被重新投递。".encode())
    payload = {"instructions": "用工具回答。", "tools": ["calculate", "read_artifact"],
               "artifacts": {"doc": doc},
               "input": '算一下\n@tool calculate {"expression": "(3 + 4) * 12"}\n@tool read_artifact {"name": "doc"}'}
    out = run(run_agent(ctx, payload))
    assert [c["name"] for c in out["tool_calls"]] == ["calculate", "read_artifact"]
    assert out["tool_calls"][0]["output"] == "84"
    assert out["tool_calls"][1]["output"] == "租约过期后任务会被重新投递。"
    assert "calculate → 84" in out["final_output"]
    assert out["model_calls"] == 2 and out["usage"]["requests"] == 2
    assert out["usage"]["total_tokens"] == out["usage"]["input_tokens"] + out["usage"]["output_tokens"] > 0
    assert len(ctx.usage) == 2 and all(u["backend"] == "openai-agents" for u in ctx.usage)


def test_max_turns_exceeded_is_retryable_and_usage_kept(tmp_path):
    ctx = make_ctx(tmp_path)
    with pytest.raises(AgentMaxTurns) as info:
        run(run_agent(ctx, {"input": "@loop", "tools": ["current_time"], "max_turns": 3}))
    assert info.value.retryable and info.value.kind == "max_turns"
    assert len(ctx.usage) == 3                       # 失败的 run 也留下了 3 次模型调用的 usage


def test_tool_whitelist(tmp_path):
    with pytest.raises(Fatal):
        run(run_agent(make_ctx(tmp_path), {"input": "x", "tools": ["shell"]}))
    # 模型想调用一个没在白名单里的工具：SDK 报 ModelBehaviorError → 可重试错误，不会执行
    with pytest.raises(Retryable):
        run(run_agent(make_ctx(tmp_path), {"input": '@tool calculate {"expression": "1+1"}', "tools": []}))


def test_payload_validation(tmp_path):
    for bad in [{"input": ""}, {"input": "x", "max_turns": 0}, {"input": "x", "max_turns": 99},
                {"input": "x", "timeout_s": -1}]:
        with pytest.raises(Fatal):
            run(run_agent(make_ctx(tmp_path), bad))


def test_effect_dedup_across_reruns(tmp_path):
    broker, job_id = MemoryBroker(), uuid.uuid4()
    payload = {"input": '@tool save_note {"title": "t", "text": "只保存一次"}', "tools": ["save_note"]}
    first = run(run_agent(make_ctx(tmp_path, broker, attempt=1, job_id=job_id), payload))
    second = run(run_agent(make_ctx(tmp_path, broker, attempt=2, job_id=job_id), payload))   # 模拟 Runner 重跑
    assert len(broker.effects) == 1
    assert json.loads(first["tool_calls"][0]["output"])["attempt"] == 1
    assert json.loads(second["tool_calls"][0]["output"])["attempt"] == 1   # 第二次直接拿到第一次的回执


def test_effect_started_without_receipt_stops_the_run(tmp_path):
    broker, job_id = MemoryBroker(), uuid.uuid4()
    payload = {"input": '@tool save_note {"title": "t", "text": "x"}', "tools": ["save_note"]}
    run(run_agent(make_ctx(tmp_path, broker, job_id=job_id), payload))
    key = next(iter(broker.effects))
    broker.effects[key] = {"state": "started", "receipt": None}           # 上次执行到一半崩了
    with pytest.raises(Fatal, match="manual check"):
        run(run_agent(make_ctx(tmp_path, broker, attempt=2, job_id=job_id), payload))


def test_lease_lost_cancels_run(tmp_path):
    from personal_agent.core.errors import LeaseLost
    ctx = make_ctx(tmp_path)
    ctx.lost.set()
    os.environ["PA_FAKE_LLM_DELAY_MS"] = "200"
    try:
        with pytest.raises(LeaseLost):
            run(run_agent(ctx, {"input": "@loop", "tools": ["current_time"], "max_turns": 10}))
    finally:
        del os.environ["PA_FAKE_LLM_DELAY_MS"]


def test_run_budget_timeout(tmp_path):
    os.environ["PA_FAKE_LLM_DELAY_MS"] = "300"
    try:
        with pytest.raises(Retryable, match="budget"):
            run(run_agent(make_ctx(tmp_path), {"input": "@loop", "tools": ["current_time"], "max_turns": 10,
                                               "timeout_s": 0.5}))
    finally:
        del os.environ["PA_FAKE_LLM_DELAY_MS"]


def test_safe_eval():
    assert safe_eval("2 ** 10 - (3 * 4) / 2") == 1018
    for bad in ["__import__('os')", "a + 1", "9 ** 999", "len('x')", "1 if 1 else 2"]:
        with pytest.raises((ValueError, SyntaxError)):
            safe_eval(bad)
    assert set(TOOLS) == {"current_time", "calculate", "read_artifact", "save_note"}


def test_agents_llm_matches_fake_llm():
    req = LLMRequest(prompt="第一句。第二句。第三句。第四句。", system="摘要", max_output_tokens=64)
    a, b = run(AgentsLLM().complete(req)), run(FakeLLM(delay_ms=0).complete(req))
    assert a.text == b.text and a.model == "fake-agent-v1" and a.usage["input_tokens"] > 0


def test_tracing_is_off_by_default_and_local_mode_never_uploads(tmp_path):
    from agents.tracing import get_trace_provider
    assert get_trace_provider()._multi_processor._processors == ()
    trace_file = tmp_path / "traces.jsonl"
    code = (
        "import asyncio, json\n"
        "from personal_agent.infra.agents_sdk import AgentsLLM, LocalJsonlTraceProcessor\n"
        "from personal_agent.core.executor import LLMRequest\n"
        "from agents.tracing import get_trace_provider\n"
        "ps = get_trace_provider()._multi_processor._processors\n"
        "assert len(ps) == 1 and isinstance(ps[0], LocalJsonlTraceProcessor), ps\n"
        "asyncio.run(AgentsLLM().complete(LLMRequest(prompt='一句话。')))\n"
        "print('ok')\n")
    env = {**os.environ, "PA_AGENTS_TRACING": "local", "PA_AGENTS_TRACE_FILE": str(trace_file)}
    env.pop("OPENAI_AGENTS_DISABLE_TRACING", None)
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    kinds = [json.loads(l)["kind"] for l in trace_file.read_text().splitlines()]
    assert "trace" in kinds and "span" in kinds
    assert "一句话" not in trace_file.read_text()           # 默认不记录模型输入输出

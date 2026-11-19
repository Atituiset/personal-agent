"""OpenAI Agents SDK（`openai-agents`）适配层。

这一层只负责“跑一次 Agent”：模型选择、tracing 开关、调用账本和 usage 记录、异常映射。
队列语义（lease / persist-then-ack / 重试 / 死信）仍然由 worker 负责：整个 `Runner.run`
在 worker 看来就是一次 process 步骤。

模型（PA_AGENT_MODEL）：
- fake（默认）：`FakeAgentModel`，实现 SDK 的自定义 `Model` 接口，离线、确定性。
- openai：`OpenAIChatCompletionsModel` + `AsyncOpenAI(base_url=PA_LLM_BASE_URL)`，
  适用于任意 OpenAI 兼容的 /chat/completions。key 依次取 PA_LLM_API_KEY、OPENAI_API_KEY、OPENCODE_API_KEY。
  官方文档：https://openai.github.io/openai-agents-python/models/

Tracing（PA_AGENTS_TRACING）：
- off（默认）：`set_tracing_disabled(True)` + `RunConfig(tracing_disabled=True)`，什么都不上传。
- local：`set_trace_processors([...])` 用本地 JSONL 处理器替换默认处理器（默认处理器会上传到 OpenAI）。
  默认不记录模型输入输出（trace_include_sensitive_data=False），PA_AGENTS_TRACE_SENSITIVE=1 可打开。
  官方文档：https://openai.github.io/openai-agents-python/tracing/
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

if os.environ.get("PA_AGENTS_TRACING", "off") != "local":
    # 在导入 SDK 之前就关掉 tracing（官方开关之一），避免任何默认导出
    os.environ.setdefault("OPENAI_AGENTS_DISABLE_TRACING", "1")

from agents import (Agent, ModelSettings, RunConfig, RunContextWrapper, RunHooks, Runner,
                    set_trace_processors, set_tracing_disabled)
from agents.exceptions import AgentsException, MaxTurnsExceeded, ModelBehaviorError, UserError
from agents.items import ModelResponse
from agents.models.interface import Model, ModelTracing
from agents.tracing import TracingProcessor
from agents.usage import Usage
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText

from personal_agent.core.errors import Fatal, JobError, LeaseLost, RateLimited, Retryable
from personal_agent.core.executor import LLMRequest, LLMResult

from .fake_llm import _tokens, fake_summary


# ---------------------------------------------------------------- 错误
class AgentMaxTurns(Retryable):
    """超过 max_turns。按可重试处理（真实模型下一次可能更快收敛）；重试耗尽进死信。"""
    kind = "max_turns"


def map_error(exc: BaseException) -> BaseException:
    """把 SDK / openai 异常映射成队列的错误分类。工具里抛出的 JobError / LeaseLost 会被 SDK 包一层，这里解开。"""
    seen, cur = set(), exc
    while cur is not None and id(cur) not in seen:
        if isinstance(cur, (JobError, LeaseLost)):
            return cur
        seen.add(id(cur))
        cur = cur.__cause__ or cur.__context__
    if isinstance(exc, MaxTurnsExceeded):
        return AgentMaxTurns(f"agent exceeded max_turns: {exc}")
    try:
        import openai
        if isinstance(exc, openai.RateLimitError):
            ra = exc.response.headers.get("retry-after") if exc.response is not None else None
            try:
                ra = float(ra) if ra is not None else None
            except ValueError:
                ra = None
            return RateLimited(f"model rate limited: {exc}", retry_after=ra)
        if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError, openai.NotFoundError,
                            openai.BadRequestError)):
            return Fatal(f"{type(exc).__name__}: {exc}")
        if isinstance(exc, (openai.APIConnectionError, openai.APITimeoutError, openai.InternalServerError)):
            return Retryable(f"{type(exc).__name__}: {exc}")
    except ImportError:  # pragma: no cover
        pass
    if isinstance(exc, UserError):
        return Fatal(f"agents UserError: {exc}")
    if isinstance(exc, (ModelBehaviorError, AgentsException, TimeoutError)):
        return Retryable(f"{type(exc).__name__}: {exc}")
    return exc


# ---------------------------------------------------------------- tracing
class LocalJsonlTraceProcessor(TracingProcessor):
    """把 trace / span 写到本地 JSONL 文件；不做任何网络请求。"""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    def _write(self, kind: str, obj) -> None:
        try:
            data = obj.export()
        except Exception:  # noqa: BLE001  导出失败就丢掉，不影响任务
            return
        if data is None:
            return
        line = json.dumps({"kind": kind, **data}, ensure_ascii=False, default=str) + "\n"
        with self._lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(line)

    def on_trace_start(self, trace) -> None:
        pass

    def on_trace_end(self, trace) -> None:
        self._write("trace", trace)

    def on_span_start(self, span) -> None:
        pass

    def on_span_end(self, span) -> None:
        self._write("span", span)

    def shutdown(self) -> None:
        pass

    def force_flush(self) -> None:
        pass


_TRACING: dict | None = None


def configure_tracing() -> dict:
    """进程内只配置一次。返回 {"mode", "path"}。任何模式下都不会注册 OpenAI 的上传处理器。"""
    global _TRACING
    if _TRACING is not None:
        return _TRACING
    mode = os.environ.get("PA_AGENTS_TRACING", "off")
    if mode == "local":
        path = os.environ.get("PA_AGENTS_TRACE_FILE", "var/agent-traces.jsonl")
        set_trace_processors([LocalJsonlTraceProcessor(path)])   # 替换掉默认的 OpenAI 导出器
        set_tracing_disabled(False)
        _TRACING = {"mode": "local", "path": path}
    elif mode == "off":
        set_trace_processors([])
        set_tracing_disabled(True)
        _TRACING = {"mode": "off", "path": None}
    else:
        raise ValueError(f"unsupported PA_AGENTS_TRACING: {mode} (use off or local)")
    return _TRACING


def run_config(workflow_name: str, trace_metadata: dict | None = None) -> RunConfig:
    t = configure_tracing()
    return RunConfig(workflow_name=workflow_name,
                     tracing_disabled=t["mode"] == "off",
                     trace_include_sensitive_data=os.environ.get("PA_AGENTS_TRACE_SENSITIVE") == "1",
                     trace_metadata={k: str(v) for k, v in (trace_metadata or {}).items()})


# ---------------------------------------------------------------- 假模型
def _message(text: str, item_id: str) -> ResponseOutputMessage:
    return ResponseOutputMessage(id=item_id, type="message", role="assistant", status="completed",
                                 content=[ResponseOutputText(text=text, type="output_text", annotations=[],
                                                             logprobs=[])])


def _user_text(input) -> str:
    if isinstance(input, str):
        return input
    parts = []
    for item in input:
        if isinstance(item, dict) and item.get("role") == "user":
            content = item.get("content")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                parts += [c.get("text", "") for c in content if isinstance(c, dict)]
    return "\n".join(parts)


def _tool_outputs(input) -> list[dict]:
    if isinstance(input, str):
        return []
    calls = {i.get("call_id"): i.get("name") for i in input if isinstance(i, dict) and i.get("type") == "function_call"}
    return [{"name": calls.get(i.get("call_id")), "output": i.get("output")}
            for i in input if isinstance(i, dict) and i.get("type") == "function_call_output"]


DIRECTIVE = re.compile(r"^@tool\s+([A-Za-z_][\w]*)\s*(\{.*\})?\s*$")


class FakeAgentModel(Model):
    """确定性假模型（SDK 自定义 Model）。行为由用户输入里的指令决定：

    - `@tool <name> <json 参数>`：第一轮按顺序发出这些工具调用；拿到工具结果后给出最终回答。
    - `@loop`：每一轮都调用 current_time，永远不给最终回答（用来测 max_turns）。
    - 没有指令：返回输入的前几句（与 FakeLLM 一致）。
    usage 用简单分词数估算，保证每次相同。
    """

    name = "fake-agent-v1"

    def __init__(self, delay_ms: float | None = None):
        self.delay = (float(os.environ.get("PA_FAKE_LLM_DELAY_MS", "20")) if delay_ms is None else delay_ms) / 1000

    async def get_response(self, system_instructions, input, model_settings, tools, output_schema, handoffs,
                           tracing: ModelTracing, *, previous_response_id=None, conversation_id=None, prompt=None,
                           **kwargs) -> ModelResponse:
        import asyncio
        if self.delay:
            await asyncio.sleep(self.delay)
        text = _user_text(input)
        outputs = _tool_outputs(input)
        turn = len([i for i in (input if isinstance(input, list) else []) if isinstance(i, dict)
                    and i.get("type") == "function_call"])
        items: list = []
        if "@loop" in text:
            items.append(ResponseFunctionToolCall(type="function_call", call_id=f"call_loop_{turn}",
                                                  name="current_time", arguments="{}", id=f"fc_loop_{turn}"))
        elif not outputs:
            for n, line in enumerate(l for l in text.splitlines() if DIRECTIVE.match(l.strip())):
                m = DIRECTIVE.match(line.strip())
                items.append(ResponseFunctionToolCall(type="function_call", call_id=f"call_{n}", name=m.group(1),
                                                      arguments=m.group(2) or "{}", id=f"fc_{n}"))
        if not items:
            if outputs:
                answer = "工具结果：" + "；".join(f"{o['name']} → {o['output']}" for o in outputs)
            else:
                answer = fake_summary(text, model_settings.max_tokens or 256)
            items.append(_message(answer, "msg_final"))
        in_tokens = _tokens((system_instructions or "") + json.dumps(input, ensure_ascii=False, default=str))
        out_tokens = _tokens(json.dumps([i.model_dump() for i in items], ensure_ascii=False))
        usage = Usage(requests=1, input_tokens=in_tokens, output_tokens=out_tokens,
                      total_tokens=in_tokens + out_tokens)
        return ModelResponse(output=items, usage=usage, response_id=None)

    async def stream_response(self, *args, **kwargs):  # 队列里只用非流式
        raise NotImplementedError("FakeAgentModel does not stream")
        yield  # pragma: no cover


# ---------------------------------------------------------------- 模型选择
def build_agent_model() -> tuple[Model, str]:
    kind = os.environ.get("PA_AGENT_MODEL", "fake")
    if kind == "fake":
        return FakeAgentModel(), FakeAgentModel.name
    if kind == "openai":
        from agents import OpenAIChatCompletionsModel
        from openai import AsyncOpenAI
        key = os.environ.get("PA_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENCODE_API_KEY")
        base_url, model = os.environ.get("PA_LLM_BASE_URL"), os.environ.get("PA_LLM_MODEL")
        if not (key and base_url and model):
            raise Fatal("PA_AGENT_MODEL=openai needs PA_LLM_BASE_URL, PA_LLM_MODEL and an API key")
        # max_retries=0：重试交给队列（带退避、死信、账本），避免 SDK 和队列双重重试
        client = AsyncOpenAI(base_url=base_url, api_key=key, max_retries=0)
        return OpenAIChatCompletionsModel(model=model, openai_client=client), model
    raise ValueError(f"unsupported PA_AGENT_MODEL: {kind} (use fake or openai)")


# ---------------------------------------------------------------- 账本与 usage
@dataclass
class AgentRunContext:
    """传给 Runner.run(context=...) 的本地上下文；工具通过 RunContextWrapper.context 拿到它。"""
    job_ctx: Any = None
    payload: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)
    model_name: str = ""
    calls: list = field(default_factory=list)   # 每次模型调用的 usage


def _ledger_path(model_name: str) -> str | None:
    return os.environ.get("PA_LLM_LEDGER") or (
        os.environ.get("PA_FAKE_LLM_LEDGER") if model_name == FakeAgentModel.name else None)


class LedgerHooks(RunHooks):
    """每次模型调用前写账本（fsync），调用后记录 usage。失败的 run 也有记录。"""

    async def on_llm_start(self, context: RunContextWrapper, agent, system_prompt, input_items) -> None:
        rc: AgentRunContext = context.context
        path = _ledger_path(rc.model_name)
        if not path:
            return
        line = json.dumps({**rc.meta, "pid": os.getpid(), "model": rc.model_name, "backend": "openai-agents",
                           "at": time.time()}, ensure_ascii=False) + "\n"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line.encode())
            os.fsync(fd)
        finally:
            os.close(fd)

    async def on_llm_end(self, context: RunContextWrapper, agent, response: ModelResponse) -> None:
        rc: AgentRunContext = context.context
        u = response.usage
        entry = {"backend": "openai-agents", "model": rc.model_name,
                 "usage": {"requests": u.requests, "input_tokens": u.input_tokens,
                           "output_tokens": u.output_tokens, "total_tokens": u.total_tokens}}
        rc.calls.append(entry)
        if rc.job_ctx is not None:
            rc.job_ctx.usage.append(entry)


def usage_totals(u: Usage) -> dict:
    return {"requests": u.requests, "input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
            "total_tokens": u.total_tokens}


# ---------------------------------------------------------------- PA_LLM=agents：给 demo.summarize 等用
class AgentsLLM:
    """LLMExecutor 协议的 Agents SDK 实现：单轮、无工具的 Agent。"""

    async def complete(self, request: LLMRequest) -> LLMResult:
        model, name = build_agent_model()
        agent = Agent(name="summarizer", instructions=request.system or None, model=model,
                      model_settings=ModelSettings(max_tokens=request.max_output_tokens, timeout=request.timeout))
        rc = AgentRunContext(meta=dict(request.metadata), model_name=name)
        try:
            result = await Runner.run(agent, request.prompt, context=rc, max_turns=1, hooks=LedgerHooks(),
                                      run_config=run_config("personal-agent.llm", request.metadata))
        except Exception as exc:  # noqa: BLE001
            raise map_error(exc) from exc
        u = result.context_wrapper.usage
        return LLMResult(text=str(result.final_output), model=name,
                         usage={"input_tokens": u.input_tokens, "output_tokens": u.output_tokens},
                         cost_usd=0.0 if name == FakeAgentModel.name else None)


configure_tracing()   # 导入时立即替换默认处理器：默认的 BatchTraceProcessor 会上传到 OpenAI

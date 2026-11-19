"""LLM 执行端口：handler 只依赖这个协议，不依赖具体 SDK。

思路来自 security-agent 的 `security_agent/core/executor.py`（`Task` / `AgentResult` /
`AgentExecutor` Protocol：执行端口不含供应商类型）。这里改成 async 接口，并给结果加上
`usage`（token 用量），为 M1 的 RPM/TPM 令牌桶和成本统计做准备。
"""
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class LLMRequest:
    prompt: str
    system: str = ""
    model: str = "fake-summarizer-v1"
    max_output_tokens: int = 256
    timeout: float = 60.0
    metadata: dict = field(default_factory=dict)  # job_id / attempt / trace_id，只用于记账与追踪


@dataclass(frozen=True)
class LLMResult:
    text: str
    model: str
    usage: dict | None = None      # {"input_tokens": n, "output_tokens": m}；未知时为 None
    cost_usd: float | None = None  # 未知时为 None，不能当成 0


class LLMExecutor(Protocol):
    async def complete(self, request: LLMRequest) -> LLMResult: ...

"""确定性假模型：不需要 API key，相同输入永远得到相同输出。

可选的调用账本 `PA_FAKE_LLM_LEDGER`：每次调用追加一行 JSON 并 fsync，测试用它统计
“模型到底被调用了几次”（崩溃恢复是否重复付费）。
"""
import asyncio
import json
import os
import re
import time

from personal_agent.core.executor import LLMRequest, LLMResult


def _tokens(text: str) -> int:
    return len(re.findall(r"\w+|[^\w\s]", text))


def fake_summary(prompt: str, max_output_tokens: int = 256) -> str:
    """取前几句（最多 3 句，受 token 预算限制）：确定性的“摘要”。"""
    sentences = [s.strip() for s in re.split(r"(?<=[。！？.!?])\s*", prompt) if s.strip()]
    picked, budget = [], max_output_tokens
    for sentence in sentences:
        cost = _tokens(sentence)
        if picked and cost > budget:
            break
        picked.append(sentence)
        budget -= cost
        if len(picked) >= 3:
            break
    return " ".join(picked) if picked else ""


class FakeLLM:
    def __init__(self, delay_ms: float | None = None, ledger: str | None = None):
        self.delay = (float(os.environ.get("PA_FAKE_LLM_DELAY_MS", "20")) if delay_ms is None else delay_ms) / 1000
        self.ledger = ledger if ledger is not None else os.environ.get("PA_FAKE_LLM_LEDGER")

    def _record(self, request: LLMRequest) -> None:
        if not self.ledger:
            return
        line = json.dumps({**request.metadata, "pid": os.getpid(), "model": request.model,
                           "at": time.time()}, ensure_ascii=False) + "\n"
        fd = os.open(self.ledger, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line.encode())
            os.fsync(fd)
        finally:
            os.close(fd)

    async def complete(self, request: LLMRequest) -> LLMResult:
        self._record(request)            # 先记账再返回：模拟“请求已发出、费用已产生”
        if self.delay:
            await asyncio.sleep(self.delay)
        text = fake_summary(request.prompt, request.max_output_tokens)
        return LLMResult(text=text, model=request.model,
                         usage={"input_tokens": _tokens(request.system + request.prompt),
                                "output_tokens": _tokens(text)},
                         cost_usd=0.0)

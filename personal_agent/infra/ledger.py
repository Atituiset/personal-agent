"""给任意 LLMExecutor 加调用账本：每次调用前追加一行 JSON 并 fsync（PA_LLM_LEDGER）。

用于验证“崩溃恢复不重复调用真实模型”。fake LLM 有自己的 PA_FAKE_LLM_LEDGER，两者不要同时开。
"""
import json
import os
import time

from personal_agent.core.executor import LLMExecutor, LLMRequest, LLMResult


class LedgerLLM:
    def __init__(self, inner: LLMExecutor, path: str):
        self.inner, self.path = inner, path

    async def complete(self, request: LLMRequest) -> LLMResult:
        line = json.dumps({**request.metadata, "pid": os.getpid(), "model": request.model,
                           "backend": type(self.inner).__name__, "at": time.time()}, ensure_ascii=False) + "\n"
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line.encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        return await self.inner.complete(request)

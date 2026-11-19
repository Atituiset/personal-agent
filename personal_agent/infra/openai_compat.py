"""真实模型接口（预留，M0 未在测试中调用）：任意 OpenAI 兼容的 /chat/completions。

环境变量：PA_LLM_BASE_URL、PA_LLM_API_KEY、PA_LLM_MODEL。密钥只从环境读取，不写入任何产物。
接 OpenCode Console 的付费模型：PA_LLM_BASE_URL=https://opencode.ai/inference/openai/v1，
密钥用 OPENCODE_API_KEY（未设置 PA_LLM_API_KEY 时自动读取）。注意免费模型在 OpenCode 之外的
客户端会收到 403（https://opencode.ai/docs/console/harnesses/），免费模型请用 PA_LLM=opencode。
M1 计划把 security-agent 的 Claude Agent SDK / OpenCode / Hermes 适配器也包装成 LLMExecutor。
"""
import os

import httpx

from personal_agent.core.errors import Fatal, RateLimited, Retryable
from personal_agent.core.executor import LLMRequest, LLMResult


class OpenAICompatLLM:
    def __init__(self, base_url: str | None = None, api_key: str | None = None, model: str | None = None):
        self.base_url = (base_url or os.environ["PA_LLM_BASE_URL"]).rstrip("/")
        self.api_key = api_key or os.environ.get("PA_LLM_API_KEY") or os.environ["OPENCODE_API_KEY"]
        self.model = model or os.environ.get("PA_LLM_MODEL")

    async def complete(self, request: LLMRequest) -> LLMResult:
        model = self.model or request.model
        body = {"model": model, "max_tokens": request.max_output_tokens,
                "messages": ([{"role": "system", "content": request.system}] if request.system else [])
                            + [{"role": "user", "content": request.prompt}]}
        try:
            async with httpx.AsyncClient(timeout=request.timeout) as client:
                resp = await client.post(f"{self.base_url}/chat/completions", json=body,
                                         headers={"Authorization": f"Bearer {self.api_key}"})
        except httpx.TransportError as exc:
            raise Retryable(f"transport: {type(exc).__name__}") from exc
        if resp.status_code == 429:
            after = resp.headers.get("retry-after")
            raise RateLimited(f"upstream HTTP 429: {resp.text[:300]}",
                              retry_after=float(after) if after and after.isdigit() else None)
        if resp.status_code >= 500:
            raise Retryable(f"upstream HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise Fatal(f"upstream HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        usage = data.get("usage") or {}
        return LLMResult(text=data["choices"][0]["message"]["content"] or "", model=data.get("model", model),
                         usage={"input_tokens": usage.get("prompt_tokens"),
                                "output_tokens": usage.get("completion_tokens")} if usage else None,
                         cost_usd=None)

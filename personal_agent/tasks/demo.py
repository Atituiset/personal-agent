"""示例任务 demo.summarize：输入 {"text": ..., "max_tokens": 64}，输出摘要。"""
from personal_agent.core.errors import Fatal
from personal_agent.core.executor import LLMRequest

from . import TaskSpec, register

SYSTEM = "你是一个简洁的摘要助手。用不超过三句话概括输入。"


async def summarize(ctx, payload: dict) -> dict:
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise Fatal("payload.text must be a non-empty string")
    result = await ctx.complete(LLMRequest(prompt=text, system=SYSTEM,
                                           max_output_tokens=int(payload.get("max_tokens", 64))))
    return {"summary": result.text, "model": result.model, "usage": result.usage, "cost_usd": result.cost_usd}


register(TaskSpec(name="demo.summarize", handler=summarize, pool="default", timeout_s=60))

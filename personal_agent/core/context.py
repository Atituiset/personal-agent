"""handler 看到的执行上下文：LLM、Artifact Store、检查点、副作用账本。

`step()` 的“已完成就重放、输入变了就拒绝”沿用 security-agent `pipeline/controller.py`
中 `invoke()` 的 `action_id + input_hash` 做法。
"""
import asyncio
import json
from dataclasses import dataclass, field

from personal_agent.core.errors import Fatal
from personal_agent.core.executor import LLMExecutor, LLMRequest, LLMResult
from personal_agent.core.storage import ArtifactStore, digest


def fingerprint(value) -> str:
    return digest(json.dumps(value, sort_keys=True, ensure_ascii=True, default=str).encode())


@dataclass
class JobContext:
    job: dict
    broker: object
    store: ArtifactStore
    llm: LLMExecutor
    lost: asyncio.Event = field(default_factory=asyncio.Event)
    usage: list = field(default_factory=list)

    @property
    def job_id(self):
        return self.job["job_id"]

    async def complete(self, request: LLMRequest) -> LLMResult:
        meta = {"job_id": str(self.job_id), "attempt": self.job["attempt"], "trace_id": self.job["trace_id"],
                "task_type": self.job["task_type"], **request.metadata}
        result = await self.llm.complete(LLMRequest(**{**request.__dict__, "metadata": meta}))
        self.usage.append({"model": result.model, "usage": result.usage, "cost_usd": result.cost_usd})
        return result

    async def step(self, name: str, inputs, fn):
        """分步检查点：同名 step 已完成且输入哈希一致时直接返回保存的结果。"""
        input_hash = fingerprint(inputs)
        saved = await self.broker.get_checkpoint(self.job_id, name)
        if saved is not None:
            if saved["input_hash"] != input_hash:
                raise Fatal(f"step {name!r} inputs changed for the same job")
            return self.store.get_json(saved["output_ref"])
        output = await fn()
        ref = self.store.put_json(output)
        await self.broker.put_checkpoint(self.job_id, name, input_hash, ref)
        return output

    async def effect(self, key: str, fn):
        """对外副作用只执行一次。started 但没有 done 的记录交给人工确认，不自动重做。"""
        existing = await self.broker.begin_effect(key, self.job_id)
        if existing is None:
            receipt = await fn()
            await self.broker.finish_effect(key, receipt)
            return receipt
        if existing["state"] == "done":
            return existing["receipt"]
        raise Fatal(f"effect {key!r} started earlier but has no receipt; needs manual check")

"""按环境变量选择 LLM 执行器；默认 fake，绝不在没有显式配置时调用真实模型。"""
import os

from personal_agent.core.executor import LLMExecutor


def build_llm() -> LLMExecutor:
    name = os.environ.get("PA_LLM", "fake")
    if name == "fake":
        from .fake_llm import FakeLLM
        return FakeLLM()
    if name == "openai":
        from .openai_compat import OpenAICompatLLM
        return OpenAICompatLLM()
    raise ValueError(f"unsupported PA_LLM: {name}")

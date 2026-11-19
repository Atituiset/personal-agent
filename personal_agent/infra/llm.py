"""按环境变量选择 LLM 执行器；默认 fake，绝不在没有显式配置时调用真实模型。

PA_LLM=fake      确定性假模型（默认）
PA_LLM=opencode  OpenCode CLI（可用 OpenCode 免费模型，见 opencode_cli.py）
PA_LLM=openai    任意 OpenAI 兼容 /chat/completions（包括 OpenCode Console 付费模型）
PA_LLM_LEDGER    可选：记录每次真实调用的账本文件
"""
import os

from personal_agent.core.executor import LLMExecutor


def build_llm() -> LLMExecutor:
    name = os.environ.get("PA_LLM", "fake")
    if name == "fake":
        from .fake_llm import FakeLLM
        llm = FakeLLM()
    elif name == "opencode":
        from .opencode_cli import OpenCodeCLI
        llm = OpenCodeCLI()
    elif name == "openai":
        from .openai_compat import OpenAICompatLLM
        llm = OpenAICompatLLM()
    else:
        raise ValueError(f"unsupported PA_LLM: {name}")
    if os.environ.get("PA_LLM_LEDGER"):
        from .ledger import LedgerLLM
        llm = LedgerLLM(llm, os.environ["PA_LLM_LEDGER"])
    return llm

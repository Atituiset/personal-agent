---
owner: "@Atituiset"
last_reviewed: 2026-11-26
code_paths: [personal_agent/infra/agents_sdk.py, personal_agent/tasks/agent.py, evals/policy.py, pyproject.toml]
---
# 参考：本项目怎么用 OpenAI Agents SDK

版本固定在 `openai-agents>=0.25,<0.26`（`pyproject.toml`），在 0.25.0 上测试过。升级前先读官方 changelog，
再跑 `tests/test_agents_unit.py`、`tests/test_agent_acceptance.py` 和评测。

官方文档：<https://openai.github.io/openai-agents-python/>

## 用到的 API

| API | 在哪 | 用途 |
| --- | --- | --- |
| `Agent`、`Runner.run`、`ModelSettings(timeout=…)` | `personal_agent/tasks/agent.py` | 跑一次 agent.run；超时同时受任务超时和 `deadline_at` 约束 |
| `function_tool`（`failure_error_function=None` 用于副作用工具） | `personal_agent/tasks/agent.py` | 四个内置工具；副作用工具出错直接中止 run |
| 自定义 `Model`（`agents.models.interface.Model`） | `personal_agent/infra/agents_sdk.py`（`FakeAgentModel`）、`evals/policy.py` | 离线、确定性的模型，CI 不需要 key |
| `OpenAIChatCompletionsModel` + `AsyncOpenAI(base_url=…)` | `personal_agent/infra/agents_sdk.py` | `PA_AGENT_MODEL=openai` 时接任意 OpenAI 兼容接口 |
| `RunHooks` | `personal_agent/infra/agents_sdk.py`（`LedgerHooks`） | 每次模型调用写调用账本和 usage |
| `RunConfig(tracing_disabled=…)`、`set_tracing_disabled`、`set_trace_processors` | `personal_agent/infra/agents_sdk.py` | 默认关闭 tracing；`local` 模式只写本地 JSONL |
| `MaxTurnsExceeded`、`ModelBehaviorError`、`UserError`、`AgentsException` | `personal_agent/infra/agents_sdk.py`（`map_error`） | 映射成队列的 `Retryable` / `Fatal` |

## 需要记住的行为

- 工具里抛出的 `JobError` / `LeaseLost` 会被 SDK 包一层，`map_error` 沿着 `__cause__` / `__context__` 解开。
- 超过 `max_turns` 映射成 `AgentMaxTurns`（可重试），重试耗尽进死信；评测 `sys_max_turns_to_dlq` 守着这条。
- 在导入 `agents` 之前设置 `OPENAI_AGENTS_DISABLE_TRACING=1`，避免任何默认导出（见 [SECURITY.md](../../SECURITY.md)）。

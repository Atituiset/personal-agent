---
owner: "@Atituiset"
last_reviewed: 2026-11-26
code_paths: [personal_agent]
---
# 技术债与已知例外

每条：编号、位置、问题、为什么暂时接受、退出条件。架构 lint 和黄金原则扫描里的例外都必须在这里有对应条目。

| 编号 | 位置 | 问题 | 暂时接受的原因 | 退出条件 |
| --- | --- | --- | --- | --- |
| TD-1 | `personal_agent/tasks/agent.py` | service 层直接导入 `agents`（`Agent`、`Runner`、`function_tool`、`ModelSettings`），而第三方 SDK 本应只出现在 adapters 层。架构检查里登记为例外。 | 工具定义和 `Runner.run` 调用是 agent.run 任务本身的内容，包一层门面只是换个地方导入，现在没有第二个 Agent 后端需要隔离。 | 出现第二个 Agent 后端，或 `openai-agents` 升级需要改动 API 时：把 Agent 构建和运行挪到 `personal_agent/infra/agents_sdk.py` 的函数里，任务只传工具列表和参数，然后删掉 `EXCEPTIONS` 里的条目。 |

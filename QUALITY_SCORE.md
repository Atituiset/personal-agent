---
owner: "@Atituiset"
last_reviewed: 2026-11-26
code_paths: [personal_agent, tests, evals]
---
# 质量评分

按架构层打分，帮助决定下一步补哪里。**等级是维护者的判断**，依据只列仓库里能查到的东西（测试、评测、已知缺口）；
测试数量以 `pytest --collect-only` 为准。每次大的改动后更新这一页，并更新 front matter 的 `last_reviewed`。

等级：A = 有单元 + 端到端检查，已知缺口很小；B = 主路径有检查，有明确缺口；C = 只有间接覆盖；D = 没有检查。

| 层 / 领域 | 等级 | 依据 | 主要缺口 |
| --- | --- | --- | --- |
| core（存储、上下文、错误） | A | `tests/test_unit.py`（内容寻址、检查点与副作用账本、重试与死信）；验收测试间接覆盖 | — |
| store（Postgres 队列） | A | `tests/test_acceptance.py` 7 条验收（幂等、kill -9、fencing、并发）；评测 7 个系统检查 | 有界队列、准入控制尚未实现（路线图 M2） |
| runtime（worker） | A | 验收测试和评测都用真实 worker 进程 | 多 pool 尚未实现（路线图 M1） |
| service：`agent.run` | B | `tests/test_agents_unit.py`、`tests/test_agent_acceptance.py`；评测 27 个助理任务 | 只用离线脚本策略评测过，没有真实模型的评测结果；TD-1 |
| service：`demo.summarize` | B | 验收测试用它跑队列语义 | 只是示例任务 |
| adapters：OpenCode CLI | B | `tests/test_opencode.py`（事件解析、限流分类） | 真实 CLI 只手动检查过（[docs/opencode-free-models-check.md](docs/opencode-free-models-check.md)） |
| adapters：OpenAI 兼容 HTTP | C | 只有异常映射被 Agents SDK 路径间接测到 | 没有针对 `openai_compat.py` 的单元测试 |
| entry：API / CLI | B | 验收测试通过 HTTP 提交；评测通过 CLI 起 worker | `status` / `reaper` 子命令没有直接测试 |
| 文档 | B | `scripts/doc_check.py`（链接、front matter、新鲜度） | 设计文档里路线图部分只能人工核对 |

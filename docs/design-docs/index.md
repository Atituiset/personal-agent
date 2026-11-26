---
owner: "@Atituiset"
last_reviewed: 2026-11-26
code_paths: [docs/design.md, docs/design-docs/core-beliefs.md, docs/design-docs/golden-principles.md]
---
# 设计文档索引

| 文档 | 状态 | 内容 |
| --- | --- | --- |
| [core-beliefs.md](core-beliefs.md) | 生效 | 这个仓库对智能体协作的基本信念：什么写进仓库、什么要机械化 |
| [golden-principles.md](golden-principles.md) | 生效 | 6 条能机械检查的规则，`scripts/golden_scan.py` 扫描，每周 workflow 报告偏差 |
| [../design.md](../design.md) | 方案（部分已实现） | 队列、状态机、并发与限流、可观测性的完整设计；M0–M0.2 已实现的部分以代码和测试为准，其余是路线图 |

测试和实测报告（只记录真实运行的结果）：

| 报告 | 内容 |
| --- | --- |
| [../m0-test-report.md](../m0-test-report.md) | M0 六条验收标准的实测 |
| [../m0.2-agents-test-report.md](../m0.2-agents-test-report.md) | Agents SDK 后端的实测 |
| [../opencode-free-models-check.md](../opencode-free-models-check.md) | OpenCode 免费模型的可用性检查 |

新增设计文档：放在本目录，带 front matter（`owner`、`last_reviewed`、`code_paths`），并在上表登记。

---
owner: "@Atituiset"
last_reviewed: 2026-11-26
code_paths: [docs/exec-plans/TEMPLATE.md, docs/exec-plans/active, docs/exec-plans/completed, docs/exec-plans/tech-debt-tracker.md]
---
# 执行计划（exec plan）约定

执行计划是仓库里的一等产物：跨多个提交、多个会话的改动，先写计划再动手，进度和决策都记在计划里，
而不是只留在聊天记录里。下一个接手的人（或智能体）只读仓库就能知道做到哪了、为什么这样做。

## 什么时候要写

- 预计超过一个提交，或会改动两个以上的层（见 [ARCHITECTURE.md](ARCHITECTURE.md)）；
- 改变了不变量、预算或门禁（[RELIABILITY.md](RELIABILITY.md)、`evals/baseline.json`）；
- 评测门禁失败后自动生成的跟进项（`make gate` 调 `evals/followup.py` 写进 `docs/exec-plans/active/`，见 [evals/README.md](evals/README.md)）。

小修小补（单个提交、单层、不碰不变量）直接在提交信息里说清楚即可。

## 放在哪

| 位置 | 内容 |
| --- | --- |
| `docs/exec-plans/active/` | 进行中的计划，文件名 `YYYY-MM-DD-<slug>.md` |
| `docs/exec-plans/completed/` | 做完的计划，原样移过来（`git mv`），补上「结果」一节 |
| `docs/exec-plans/tech-debt-tracker.md` | 已知但暂不处理的问题和例外，每条都要写原因和退出条件 |
| `docs/exec-plans/TEMPLATE.md` | 模板 |

## 规则

1. 从 [TEMPLATE.md](docs/exec-plans/TEMPLATE.md) 复制，保留 front matter，`status` 取 `active` 或 `completed`。
2. **进度**用复选框，做完一项就勾掉并写上提交或验证命令；不要事后一次性补。
3. **决策日志**每条写：日期、决定了什么、为什么、放弃了哪些选项。推翻旧决定时追加新条目，不改旧条目。
4. **意外与发现**记录和预期不符的事实（例如某个命令在本机和 CI 行为不同）。
5. 数字只写真实跑出来的，并注明命令；没测过的写「未测」。
6. 完成时：所有复选框勾掉或明确标为放弃，写「结果」一节，`status: completed`，`git mv` 到 `completed/`。
7. `scripts/doc_check.py` 会检查：front matter 齐全、`status` 和所在目录一致、引用的路径存在。

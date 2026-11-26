---
owner: "@Atituiset"
last_reviewed: 2026-11-26
code_paths: [personal_agent, scripts/check_architecture.py, tests/test_architecture.py]
---
# 架构地图

personal-agent 是一个以 Postgres 异步任务队列为核心的个人助理 Agent。完整设计和取舍见
[docs/design.md](docs/design.md)；这里只给出**代码的分层和依赖方向**，它们由
`scripts/check_architecture.py` 机械地检查（`tests/test_architecture.py` 在 pytest 里跑同一套规则）。

## 一条任务的路径

```text
POST /jobs (api) ──► PgBroker.enqueue (store) ──► jobs 表
                                                   │ lease：FOR UPDATE SKIP LOCKED，attempt+1 = fencing token
worker (runtime) ◄─────────────────────────────────┘
  ├─ 查 receipt（已 persist 未 ACK → 直接 ACK，不再调用模型）
  ├─ TASKS[task_type].handler(ctx, payload)  (service)
  │     └─ JobContext.complete / step / effect (core) ──► LLMExecutor 适配器 (adapters)
  ├─ persist：结果写 ArtifactStore + durable receipt（fenced）
  └─ ACK：同一事务写 result_ref + succeeded（fenced）
```

## 分层（从下到上）

| 层 | 模块 | 只能依赖 | 允许的第三方包 |
| --- | --- | --- | --- |
| core | `personal_agent.core.*`（errors、executor 端口、storage、context） | 标准库 | 无 |
| providers | `personal_agent.config`（横切关注点的唯一入口：配置） | core | 无 |
| adapters | `personal_agent.infra.*`（LLM 适配器、Agents SDK 适配层、调用账本） | core、providers | httpx、agents、openai |
| store | `personal_agent.queue.broker`、`personal_agent.queue.faults` | core、providers | psycopg、psycopg_pool |
| service | `personal_agent.tasks.*`（任务注册表和各任务 handler） | core、providers、adapters | 无（例外见 TD-1） |
| runtime | `personal_agent.queue.worker` | core、providers、adapters、store、service | 无 |
| entry | `personal_agent.api.*`、`personal_agent.cli`、`personal_agent.__main__` | 以上全部 | fastapi、pydantic、uvicorn、psycopg |

规则：

1. 依赖只能**向下**。adapters 和 store 是同级，互相不能导入：模型适配器不碰数据库，队列不知道用的是哪个模型。
2. service（任务 handler）不能直接用 store：handler 只通过 `JobContext`（core）做检查点和副作用，
   这样 fencing 和副作用去重不会被绕过。
3. 第三方包只能出现在表里列出的层。新增依赖时先决定它属于哪一层，再改 `scripts/check_architecture.py` 里的表和本文件。
4. `evals/`、`tests/`、`scripts/` 不属于运行时包，可以导入任何层，但不能被 `personal_agent` 导入。
5. 例外必须同时出现在 `scripts/check_architecture.py` 的 `EXCEPTIONS` 和
   [docs/exec-plans/tech-debt-tracker.md](docs/exec-plans/tech-debt-tracker.md) 里，带编号和退出条件。

## 不变量（运行时）

队列层的可靠性不变量（persist-then-ack、fencing、幂等、副作用只执行一次）和对应的检查见
[RELIABILITY.md](RELIABILITY.md)。

## 目录

| 路径 | 内容 |
| --- | --- |
| `personal_agent/` | 运行时包（上表各层） |
| `migrations/` | SQL 迁移，`personal-agent migrate` 按文件名顺序执行 |
| `evals/` | agent.run 的评测：任务、评分器、门禁、裁判校准（[evals/README.md](evals/README.md)） |
| `tests/` | 单元测试、验收测试、架构测试 |
| `scripts/` | 本地运行、文档检查、架构检查等脚本 |
| `docs/` | 设计文档、执行计划、参考资料、测试报告 |

# AGENTS.md

这是给智能体（和新加入的人）的**目录**，不是百科：每一节只告诉你去哪看。
细节都在下面链接的文档里；`scripts/doc_check.py` 保证这些链接不断、文档不过期。

## 这个仓库是什么

personal-agent：以 Postgres 异步任务队列为核心的个人助理 Agent。提交与执行分离、先持久化再 ACK、
至少一次投递 + 幂等、fencing token；`agent.run` 任务用 OpenAI Agents SDK 跑带工具白名单的 Agent。
面向人的介绍在 [README.md](README.md)。

## 先读哪里

| 想做的事 | 去读 |
| --- | --- |
| 了解分层、依赖方向、一条任务怎么走 | [ARCHITECTURE.md](ARCHITECTURE.md) |
| 改队列、worker、副作用相关代码 | [RELIABILITY.md](RELIABILITY.md)（不变量和守护它们的检查） |
| 碰密钥、tracing、工具权限、网络暴露 | [SECURITY.md](SECURITY.md) |
| 判断哪块最需要补 | [QUALITY_SCORE.md](QUALITY_SCORE.md) |
| 做跨多个提交的改动 | [PLANS.md](PLANS.md)，计划放 [docs/exec-plans/active/](docs/exec-plans/active/) |
| 看设计和取舍 | [docs/design-docs/index.md](docs/design-docs/index.md)、[核心信念](docs/design-docs/core-beliefs.md) |
| 查已知问题和例外 | [docs/exec-plans/tech-debt-tracker.md](docs/exec-plans/tech-debt-tracker.md) |
| 查依赖的用法 | [Agents SDK](docs/references/openai-agents-sdk.md)、[Postgres 队列特性](docs/references/postgres-queue.md) |
| 改评测、门禁、基线 | [evals/README.md](evals/README.md) |

## 代码在哪

| 路径 | 层 |
| --- | --- |
| `personal_agent/core/` | core：错误分类、LLM 执行端口、内容寻址存储、`JobContext` |
| `personal_agent/config.py`、`personal_agent/obs.py` | providers：配置和结构化日志的唯一入口 |
| `personal_agent/infra/` | adapters：fake / OpenAI 兼容 / OpenCode CLI / Agents SDK |
| `personal_agent/queue/broker.py`、`personal_agent/queue/stats.py` | store：Postgres 队列和只读指标 |
| `personal_agent/tasks/` | service：任务注册表和 handler |
| `personal_agent/queue/worker.py` | runtime：worker 主循环 |
| `personal_agent/api/`、`personal_agent/cli.py` | entry：HTTP API 和命令行 |
| `migrations/` | SQL 迁移 |
| `evals/`、`tests/`、`scripts/` | 评测、测试、脚本（不属于运行时包） |

依赖只能向下；违反时 `scripts/check_architecture.py` 会告诉你怎么改。

## 常用命令

需要 Python 3.12+ 和 PostgreSQL 17。数据库连接写在 `.env`（从 `.env.example` 复制，不提交）。

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q                           # 单元 + 验收（需要 TEST_DATABASE_URL）
make check                                              # 文档检查 + 分层检查
.venv/bin/python -m evals.run_evals --trials 5 --k 1 3 5 --out test-results/evals-results.json
.venv/bin/python -m evals.gate --baseline evals/baseline.json --current test-results/evals-results.json --k 3
```

## 让运行时自己说话

```bash
make dev-up                                   # 隔离实例：自己的 Postgres 库 + worker + API，JSON 日志
make dev-status                               # 进程、数据库、指标快照
make metrics                                  # 队列深度、入队→开始/ACK 延迟、死信、token
make logs ARGS="--job <job_id>"               # 一个任务的事件时间线（scripts/logq.py）
make runtime-check                            # 预算 + 7 个系统检查，见 RELIABILITY.md
make dev-purge                                # 停掉并删除数据
```

## 工作约定

1. 提交前跑上面的检查；CI（`.github/workflows/`）跑同样的命令。
2. 跨多个提交的改动先写执行计划，进度和决策记在计划里，做完移到 `docs/exec-plans/completed/`。
3. 改了行为就改对应文档，并更新它 front matter 里的 `last_reviewed`。
4. 数字只写真实跑出来的，注明命令；没测过就写「未测」。
5. 例外（分层、黄金原则）必须登记在 tech-debt-tracker，带退出条件。
6. 不在无人值守的路径上执行对外副作用；不要部署（本仓库目前只在本机运行）。
7. 不要提交 `.env`、密钥、`var/` 下的运行数据。

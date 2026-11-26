---
owner: "@Atituiset"
last_reviewed: 2026-11-26
code_paths: [scripts/runtime_check.py, scripts/runtime_budgets.json, personal_agent/queue/stats.py, personal_agent/queue/broker.py, personal_agent/queue/worker.py, personal_agent/core/context.py, tests/test_acceptance.py, tests/test_agent_acceptance.py, evals/tasks/system.jsonl]
---
# 可靠性：不变量和怎么验证

队列的设计细节见 [docs/design.md](docs/design.md) 第 4 节和 [ARCHITECTURE.md](ARCHITECTURE.md)。
这里列出必须一直成立的事情，以及哪条测试或评测在守着它。改动 `personal_agent/queue/` 或 `personal_agent/core/context.py`
之前先读这一页；改完必须让下表的检查全部通过。

## 不变量

| # | 不变量 | 机制 | 守护它的检查 |
| --- | --- | --- | --- |
| R1 | 提交与执行分离：`POST /jobs` 只写库并立即返回 `job_id` | API 只调 `PgBroker.enqueue` | `tests/test_acceptance.py::test_1_submit_returns_job_id_within_100ms` |
| R2 | 同一幂等键只产生一个任务 | `jobs.idempotency_key UNIQUE` + `ON CONFLICT` | `test_2_same_idempotency_key_returns_same_job`；评测 `sys_duplicate_enqueue` |
| R3 | persist 之前崩溃：任务被重投并完成 | 租约过期 → reaper 重新排队 | `test_3_kill9_before_persist_is_redelivered`；评测 `sys_kill9_before_persist` |
| R4 | persist 之后、ACK 之前崩溃：不再调用模型 | worker 先查 durable receipt | `test_4_kill9_after_persist_does_not_call_model_again`；评测 `sys_kill9_after_persist` |
| R5 | 过期租约持有者的写入一律被拒 | `attempt` 作为 fencing token，所有写入带 `FENCE` 条件 | `test_5a_stale_lease_holder_process_is_fenced`、`test_5b_fencing_token_rejects_stale_writes` |
| R6 | 多 worker 并发：不重复、不遗漏 | `FOR UPDATE SKIP LOCKED` | `test_6_two_workers_200_jobs_no_dup_no_loss` |
| R7 | 对外副作用同一参数只执行一次（包括崩溃重跑） | `effects` 账本，`JobContext.effect` | `tests/test_unit.py`（检查点与副作用账本）；评测 `sys_same_effect_twice_in_one_run`、`sys_kill9_before_persist` |
| R8 | 失败分类正确：不可重试直接进死信，可重试的退避后重试，耗尽进死信 | `Fatal` / `Retryable` / `RateLimited`，`dead_letters` 表 | `tests/test_unit.py::test_retry_then_dead_letter`；评测 `sys_max_turns_to_dlq`、`sys_invalid_tool_is_fatal`、`sys_deadline_passed_is_fatal` |

评测里的 7 个系统检查（`evals/tasks/system.jsonl`）用真实 worker 进程和 Postgres 跑，判分只读数据库、产物和调用账本，
细节见 [evals/README.md](evals/README.md)。

## 运行时预算（本地和 CI 都能验证）

`make dev-up` 起一个隔离实例（自己的 Postgres 库、worker、API，JSON 日志在 `var/dev/<name>/logs/`），
`make runtime-check` 对它检查下面的预算，并把 7 个系统检查跑在实例自己的评测库上。阈值在
`scripts/runtime_budgets.json`，是我们选定的上限，不是测量值；实测数字写在每次运行的 `var/dev/<name>/runtime-check.md`。

| 预算 | 阈值 | 怎么量 |
| --- | --- | --- |
| 提交延迟 p95 | 100 ms（沿用 M0 验收标准 1） | 客户端计时，逐条 `POST /jobs` |
| 入队 → 开始执行 p95 | 1000 ms | `attempts.started_at − jobs.enqueued_at`（第 1 次 attempt） |
| 入队 → ACK p95 | 2000 ms | `jobs.finished_at − jobs.enqueued_at`（succeeded） |
| 探针任务死信 / 重试 | 0 / 0 | `jobs.state`、`jobs.attempt` |
| 结构化日志覆盖 | 每个探针任务都有 `leased`、`acked` 事件 | `logs/worker.jsonl` |
| 系统检查 | 7 个任务 × 3 次全部通过 | `evals.run_evals --only <系统检查>` |

探针用 fake LLM（`PA_FAKE_LLM_DELAY_MS` 默认 20 ms）和一个并发 4 的 worker，所以延迟数字只反映队列本身，
不代表接真实模型后的速度。

排查时：`make dev-status`（进程 + 指标快照）、`make metrics`（`personal-agent metrics` 的 JSON，
包括队列深度、入队到开始/ACK 的 p50/p95、attempt 结果分布、死信原因、token 总数）、
`make logs ARGS="--job <job_id>"`（一个任务的完整事件时间线）。CI 的 `checks / runtime` job 跑同样的检查，
报告和日志作为 artifact 上传。

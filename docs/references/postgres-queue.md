---
owner: "@Atituiset"
last_reviewed: 2026-11-26
code_paths: [personal_agent/queue/broker.py, personal_agent/cli.py, migrations/0001_init.sql, evals/run_evals.py]
---
# 参考：队列用到的 Postgres 特性

需要 PostgreSQL 17（CI 用 `postgres:17` 服务容器，`docker-compose.yml` 也是 17）。

| 特性 | 在哪 | 为什么 | 官方文档 |
| --- | --- | --- | --- |
| `SELECT … FOR UPDATE SKIP LOCKED` | `LEASE_SQL`、`REAP_SQL`（`personal_agent/queue/broker.py`） | 多个 worker 并发领任务互不阻塞，也不会领到同一行 | [The Locking Clause](https://www.postgresql.org/docs/17/sql-select.html#SQL-FOR-UPDATE-SHARE) |
| `INSERT … ON CONFLICT` | `enqueue`、`begin_effect`、`put_checkpoint` | 幂等键去重；副作用账本和检查点只写一次 | [ON CONFLICT Clause](https://www.postgresql.org/docs/17/sql-insert.html#SQL-ON-CONFLICT) |
| 带条件的 `UPDATE`（`FENCE`） | `persist_receipt`、`ack`、`fail`、`heartbeat` | fencing：影响 0 行就说明租约已经不是自己的，抛 `LeaseLost` | [UPDATE](https://www.postgresql.org/docs/17/sql-update.html) |
| 事务级 advisory lock `pg_advisory_xact_lock` | `migrate`（`personal_agent/cli.py`） | 多个进程同时迁移时串行化 | [Advisory Locks](https://www.postgresql.org/docs/17/explicit-locking.html#ADVISORY-LOCKS) |
| 会话级 advisory lock `pg_try_advisory_lock` | `evals/run_evals.py` | 同一个评测库同时只跑一个评测（会 TRUNCATE） | 同上 |
| 部分索引 | `jobs_ready`、`jobs_leased`、`attempts_receipt`（`migrations/0001_init.sql`） | 只索引待领取、已租出、已 persist 的行 | [Partial Indexes](https://www.postgresql.org/docs/17/indexes-partial.html) |

时间一律用数据库的 `now()`（租约到期、退避到期），不用 worker 本机时钟，避免多机时钟漂移。

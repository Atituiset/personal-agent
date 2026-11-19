# M0 验收测试报告

- 记录时间：2026-11-19 22:53:32（北京时间，UTC+8）；Python 3.12.16；PostgreSQL 17（本机）
- 命令：`.venv/bin/python -m pytest -v --durations=0`；pytest 退出码 0；原始输出见 `test-results/pytest-run.txt`，原始数字见 `test-results/acceptance-metrics.json`
- 模型：确定性 fake LLM（`PA_FAKE_LLM_DELAY_MS` 模拟延迟），调用次数由 fsync 过的调用账本统计

| # | 验收标准 | 结果 | 实测数字 |
|---|---|---|---|
| 1 | 提交后 100 ms 内返回 job_id | ✅ | 100 次顺序 POST（先预热 5 次）：p50 3.92 ms，p95 9.95 ms，最大 24.63 ms，平均 4.81 ms（客户端计时，含 HTTP 往返） |
| 2 | 同一幂等键提交两次返回同一任务 | ✅ | 顺序两次同一 job_id；同键不同 payload 返回 HTTP 409；20 个并发请求得到 1 个 job_id（19 个标记为 deduplicated），库里对应 2 行（两个键各 1 行） |
| 3 | 持久化之前 kill -9，任务被重投并完成 | ✅ | SIGKILL 后 2.22 s 完成（租约 2 s）；最终 attempt=2；#1 worker-A: lease_expired → #2 worker-B: acked；模型调用 2 次（没有回执，只能重算） |
| 4 | 持久化之后、ACK 之前 kill -9，模型不被重复调用 | ✅ | SIGKILL 后 2.19 s 完成；#1 worker-A: persisted → #2 worker-B: acked；模型调用总数 **1**；最终结果来自 attempt 1 的回执 |
| 5a | 租约过期的旧 worker 写入被拒（进程级） | ✅ | SIGSTOP 冻结 A，1.76 s 后 B 完成；SIGCONT 后 A 的写入被拒：`persist rejected for c16bc666-c935-49db-bf5e-8716b860ae07 attempt 1`；#1 worker-A: fenced → #2 worker-B: acked；最终结果由 attempt 2 写入 |
| 5b | fencing token 拒绝过期写入（broker 级） | ✅ | persist_after_expiry=rejected，ack_after_expiry=rejected，heartbeat_after_expiry=False，new_attempt=2，old_ack_after_re_lease=rejected，old_fail_after_re_lease=rejected，new_ack=accepted |
| 6 | 两个 worker 并发消费 200 个任务，不重复不遗漏 | ✅ | 200 个任务全部 succeeded；attempt 行 200，每任务最多 1 次；模型调用 200 次、覆盖 200 个任务、遗漏 0；分配 {'worker-1': 104, 'worker-2': 96}；每 worker 并发 8、fake 延迟 20 ms，墙钟 0.68 s（含两个 worker 进程启动），约 293.7 任务/秒 |

说明：第 6 条的吞吐量只反映 fake LLM + 本机 Postgres 的情况，不代表接入真实模型后的速度。

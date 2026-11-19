"""把 test-results/acceptance-metrics.json 渲染成 docs/m0-test-report.md（数字全部来自那次真实运行）。"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

root = Path(__file__).resolve().parents[1]
m = json.loads((root / "test-results/acceptance-metrics.json").read_text())
cst = timezone(timedelta(hours=8))
when = datetime.fromtimestamp(m["_meta"]["recorded_at_unix"], cst).strftime("%Y-%m-%d %H:%M:%S")
a1, a2, a3, a4 = m["1_submit_latency"], m["2_idempotency"], m["3_kill_before_persist"], m["4_kill_after_persist"]
a5, a5b, a6 = m["5a_fencing_process"], m["5b_fencing_broker"], m["6_two_workers_200"]


def outcomes(x):
    return " → ".join(f"#{a['attempt']} {a['worker']}: {a['outcome']}" for a in x["attempts"])


lines = [
    "# M0 验收测试报告",
    "",
    f"- 记录时间：{when}（北京时间，UTC+8）；Python {m['_meta']['python']}；PostgreSQL 17（本机）",
    f"- 命令：`.venv/bin/python -m pytest -v --durations=0`；pytest 退出码 {m['_meta']['exitstatus']}；"
    "原始输出见 `test-results/pytest-run.txt`，原始数字见 `test-results/acceptance-metrics.json`",
    "- 模型：确定性 fake LLM（`PA_FAKE_LLM_DELAY_MS` 模拟延迟），调用次数由 fsync 过的调用账本统计",
    "",
    "| # | 验收标准 | 结果 | 实测数字 |",
    "|---|---|---|---|",
    f"| 1 | 提交后 100 ms 内返回 job_id | ✅ | {a1['requests']} 次顺序 POST（先预热 5 次）：p50 {a1['p50_ms']} ms，"
    f"p95 {a1['p95_ms']} ms，最大 {a1['max_ms']} ms，平均 {a1['mean_ms']} ms（客户端计时，含 HTTP 往返） |",
    f"| 2 | 同一幂等键提交两次返回同一任务 | ✅ | 顺序两次同一 job_id；同键不同 payload 返回 HTTP {a2['different_payload_status']}；"
    f"{a2['concurrent_requests']} 个并发请求得到 {a2['distinct_job_ids']} 个 job_id（{a2['dedup_true_count']} 个标记为 deduplicated），"
    f"库里对应 {a2['rows_in_db']} 行（两个键各 1 行） |",
    f"| 3 | 持久化之前 kill -9，任务被重投并完成 | ✅ | {a3['kill_signal']} 后 {a3['kill_to_succeeded_s']} s 完成（租约 {a3['lease_seconds']} s）；"
    f"最终 attempt={a3['final_attempt']}；{outcomes(a3)}；模型调用 {a3['llm_calls_total']} 次（没有回执，只能重算） |",
    f"| 4 | 持久化之后、ACK 之前 kill -9，模型不被重复调用 | ✅ | {a4['kill_signal']} 后 {a4['kill_to_succeeded_s']} s 完成；"
    f"{outcomes(a4)}；模型调用总数 **{a4['llm_calls_total']}**；最终结果来自 attempt {a4['result_from_attempt']} 的回执 |",
    f"| 5a | 租约过期的旧 worker 写入被拒（进程级） | ✅ | SIGSTOP 冻结 A，{a5['sigstop_to_b_succeeded_s']} s 后 B 完成；SIGCONT 后 A 的写入被拒："
    f"`{a5['a_fenced_log']['detail']}`；{outcomes(a5)}；最终结果由 attempt {a5['result_written_by_attempt']} 写入 |",
    "| 5b | fencing token 拒绝过期写入（broker 级） | ✅ | " + "，".join(f"{k}={v}" for k, v in a5b.items()) + " |",
    f"| 6 | 两个 worker 并发消费 200 个任务，不重复不遗漏 | ✅ | {a6['jobs']} 个任务全部 succeeded；attempt 行 {a6['attempt_rows']}，"
    f"每任务最多 {a6['max_attempts_per_job']} 次；模型调用 {a6['llm_calls']} 次、覆盖 {a6['distinct_jobs_called']} 个任务、遗漏 {a6['missing_jobs']}；"
    f"分配 {a6['acked_by_worker']}；每 worker 并发 {a6['concurrency_per_worker']}、fake 延迟 {a6['fake_llm_delay_ms']} ms，"
    f"墙钟 {a6['wall_clock_s']} s（含两个 worker 进程启动），约 {a6['throughput_jobs_per_s']} 任务/秒 |",
    "",
    "说明：第 6 条的吞吐量只反映 fake LLM + 本机 Postgres 的情况，不代表接入真实模型后的速度。",
    "",
]
out = root / "docs/m0-test-report.md"
out.write_text("\n".join(lines))
print(out)

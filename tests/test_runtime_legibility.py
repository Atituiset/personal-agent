"""运行时对智能体可读：结构化日志格式、metrics 快照、logq 查询。"""
import asyncio
import io
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

from personal_agent.core.errors import Fatal, Retryable
from personal_agent.obs import log_event, setup_logging
from personal_agent.queue.broker import PgBroker
from personal_agent.queue.stats import snapshot

from conftest import enqueue_many

ROOT = Path(__file__).resolve().parents[1]


def test_json_log_lines_are_flat_objects():
    buf = io.StringIO()
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        assert setup_logging("json", stream=buf) == "json"
        log_event(logging.getLogger("personal_agent.worker"), "acked", job_id="j1", attempt=2, ms=3.5)
        logging.getLogger("x").warning("plain text")
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
    first, second = [json.loads(line) for line in buf.getvalue().splitlines()]
    assert first["event"] == "acked" and first["job_id"] == "j1" and first["attempt"] == 2
    assert first["level"] == "info" and first["logger"] == "personal_agent.worker" and first["ts"].endswith("+00:00")
    assert second["event"] == "log" and second["msg"] == "plain text" and second["level"] == "warning"


def test_metrics_snapshot_counts_states_latency_and_dlq(env):
    ok, bad, retry = enqueue_many(env, [{"text": "a"}, {"text": "b"}, {"text": "c"}], prefix="stats")

    async def go():
        broker = await PgBroker(env["db"], max_size=2).open()
        try:
            for _ in range(3):
                job = await broker.lease("default", "w", 30)
                jid = str(job["job_id"])
                if jid == ok:
                    await broker.persist_receipt(job, "w", {"sha256": "x"}, [{"usage": {"total_tokens": 7}}])
                    await broker.ack(job, "w", {"sha256": "x"})
                elif jid == bad:
                    await broker.fail(job, "w", Fatal("nope"))
                else:
                    await broker.fail(job, "w", Retryable("later"), backoff_base=60)
            return await snapshot(broker, since_s=600)
        finally:
            await broker.close()
    s = asyncio.run(go())
    assert s["jobs_by_state"] == {"dead": 1, "retry_wait": 1, "succeeded": 1}
    assert s["dead_letters_by_reason"] == {"fatal": 1}
    assert s["enqueue_to_start_ms"]["n"] == 3 and s["enqueue_to_ack_ms"]["n"] == 1
    assert s["attempts_by_outcome"] == {"acked": 1, "failed": 2}
    assert s["llm"] == {"calls": 1, "total_tokens": 7}
    assert s["queue_depth"] == 0          # the retry is backing off, so nothing is ready yet


def test_metrics_cli_prints_json(env):
    enqueue_many(env, [{"text": "a"}], prefix="cli")
    out = subprocess.run([sys.executable, "-m", "personal_agent", "metrics"], cwd=ROOT, capture_output=True, text=True,
                         env={**os.environ, "DATABASE_URL": env["db"]}, timeout=60)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["jobs_by_state"] == {"queued": 1}


def test_logq_filters_and_counts(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    lines = [{"ts": "2026-11-26T00:00:00+00:00", "event": "leased", "job_id": "a"},
             {"ts": "2026-11-26T00:00:01+00:00", "event": "acked", "job_id": "a"},
             {"ts": "2026-11-26T00:00:02+00:00", "event": "failed", "job_id": "b", "state": "dead"}]
    (logs / "worker.jsonl").write_text("\n".join(map(json.dumps, lines)) + "\nTraceback (not json)\n")
    run = lambda *a: subprocess.run([sys.executable, str(ROOT / "scripts/logq.py"), "--log-dir", str(logs), *a],
                                    capture_output=True, text=True, check=True).stdout.splitlines()
    assert [json.loads(x)["event"] for x in run("--job", "a")] == ["leased", "acked"]
    assert json.loads(run("--where", "state=dead", "--fields", "job_id")[0]) == {"job_id": "b"}
    assert {json.loads(x)["event"]: json.loads(x)["count"] for x in run("--count-by", "event")} == \
        {"leased": 1, "acked": 1, "failed": 1}

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import psycopg
import pytest

from personal_agent.cli import migrate
from personal_agent.config import load_dotenv
from personal_agent.core.storage import ArtifactStore
from personal_agent.queue.broker import PgBroker

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
DB = os.environ.get("TEST_DATABASE_URL")
METRICS: dict = {}


def pytest_sessionfinish(session, exitstatus):
    if METRICS:
        out = ROOT / "test-results" / "acceptance-metrics.json"
        out.parent.mkdir(exist_ok=True)
        METRICS["_meta"] = {"recorded_at_unix": time.time(), "python": sys.version.split()[0],
                            "exitstatus": int(exitstatus)}
        out.write_text(json.dumps(METRICS, ensure_ascii=False, indent=2) + "\n")


@pytest.fixture(scope="session")
def db_url():
    if not DB:
        pytest.skip("TEST_DATABASE_URL not set")
    migrate(DB)
    return DB


@pytest.fixture
def clean_db(db_url):
    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute("TRUNCATE jobs, attempts, checkpoints, dead_letters, effects CASCADE")
    return db_url


@pytest.fixture
def env(tmp_path, clean_db):
    artifacts, faults = tmp_path / "artifacts", tmp_path / "faults"
    artifacts.mkdir()
    faults.mkdir()
    return {"db": clean_db, "artifacts": artifacts, "faults": faults,
            "ledger": tmp_path / "llm-ledger.jsonl", "logs": tmp_path}


@pytest.fixture
def metrics():
    return METRICS


def sql(db_url, query, params=()):
    with psycopg.connect(db_url, autocommit=True) as conn:
        return conn.execute(query, params).fetchall()


def enqueue_many(env, payloads, prefix="t"):
    async def go():
        broker = await PgBroker(env["db"], max_size=4).open()
        store = ArtifactStore(env["artifacts"])
        try:
            ids = []
            for i, payload in enumerate(payloads):
                job, _ = await broker.enqueue(task_type="demo.summarize", payload_ref=store.put_json(payload),
                                              idempotency_key=f"{prefix}-{i}")
                ids.append(str(job["job_id"]))
            return ids
        finally:
            await broker.close()
    return asyncio.run(go())


def ledger_calls(env):
    if not env["ledger"].exists():
        return []
    return [json.loads(line) for line in env["ledger"].read_text().splitlines() if line.strip()]


class Proc:
    def __init__(self, popen, log):
        self.popen, self.log = popen, log

    @property
    def pid(self):
        return self.popen.pid

    def text(self):
        return self.log.read_text() if self.log.exists() else ""

    def stop(self):
        if self.popen.poll() is None:
            try:
                os.kill(self.popen.pid, signal.SIGCONT)
            except ProcessLookupError:
                pass
            self.popen.terminate()
            try:
                self.popen.wait(5)
            except subprocess.TimeoutExpired:
                self.popen.kill()
                self.popen.wait()


@pytest.fixture
def spawn(env):
    procs = []

    def start(kind, name, *args, fault=None, extra_env=None):
        e = {**os.environ, "DATABASE_URL": env["db"], "PA_ARTIFACT_DIR": str(env["artifacts"]),
             "PA_FAKE_LLM_LEDGER": str(env["ledger"]), "PA_LLM": "fake", "PYTHONUNBUFFERED": "1"}
        e.pop("PA_FAULT_PAUSE_AT", None)
        if fault:
            e.update(PA_FAULT_PAUSE_AT=fault, PA_FAULT_DIR=str(env["faults"]))
        e.update(extra_env or {})
        log = env["logs"] / f"{name}.log"
        cmd = [sys.executable, "-m", "personal_agent", kind, *args]
        if kind == "worker":
            cmd += ["--worker-id", name]
        p = Proc(subprocess.Popen(cmd, cwd=ROOT, env=e, stdout=log.open("w"), stderr=subprocess.STDOUT), log)
        procs.append(p)
        return p

    yield start
    for p in procs:
        p.stop()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(predicate, timeout, interval=0.05, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")

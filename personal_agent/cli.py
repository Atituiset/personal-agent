"""命令行：migrate / api / worker / reaper / enqueue / status。"""
import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from personal_agent.config import Settings

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


def migrate(database_url: str, directory: Path = MIGRATIONS) -> list[str]:
    import psycopg
    applied = []
    with psycopg.connect(database_url, autocommit=False) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, "
                     "applied_at timestamptz NOT NULL DEFAULT now())")
        conn.execute("SELECT pg_advisory_xact_lock(727274)")
        done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
        for path in sorted(directory.glob("*.sql")):
            if path.name in done:
                continue
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
            applied.append(path.name)
        conn.commit()
    return applied


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="personal-agent")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="apply SQL migrations")
    api = sub.add_parser("api", help="run the HTTP API")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8080)
    w = sub.add_parser("worker", help="run a worker")
    w.add_argument("--pool", default="default")
    w.add_argument("--concurrency", type=int, default=4)
    w.add_argument("--lease-seconds", type=float, default=30.0)
    w.add_argument("--reap-interval", type=float, default=5.0)
    w.add_argument("--poll-interval", type=float, default=0.2)
    w.add_argument("--worker-id")
    sub.add_parser("reaper", help="reclaim expired leases once")
    e = sub.add_parser("enqueue", help="enqueue a job directly (bypasses HTTP)")
    e.add_argument("task_type")
    e.add_argument("payload", help="JSON payload")
    e.add_argument("--idempotency-key")
    s = sub.add_parser("status", help="show a job")
    s.add_argument("job_id")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", stream=sys.stderr)
    settings = Settings.from_env()

    if args.command == "migrate":
        print(json.dumps({"applied": migrate(settings.database_url)}))
        return 0
    if args.command == "api":
        import uvicorn
        from personal_agent.api.app import create_app
        uvicorn.run(create_app(settings), host=args.host, port=args.port, log_level="warning")
        return 0
    if args.command == "worker":
        from personal_agent.infra.llm import build_llm
        from personal_agent.queue.worker import run_worker
        asyncio.run(run_worker(settings.database_url, settings.artifact_dir, build_llm(), pool=args.pool,
                               concurrency=args.concurrency, lease_s=args.lease_seconds,
                               reap_interval=args.reap_interval, poll_interval=args.poll_interval,
                               worker_id=args.worker_id))
        return 0

    from personal_agent.core.storage import ArtifactStore
    from personal_agent.queue.broker import PgBroker

    async def once():
        broker = await PgBroker(settings.database_url, max_size=2).open()
        try:
            if args.command == "reaper":
                return [{k: str(v) for k, v in r.items()} for r in await broker.reap()]
            if args.command == "enqueue":
                import uuid
                ref = ArtifactStore(settings.artifact_dir).put_json(json.loads(args.payload))
                job, dedup = await broker.enqueue(task_type=args.task_type, payload_ref=ref,
                                                  idempotency_key=args.idempotency_key or str(uuid.uuid4()))
                return {"job_id": str(job["job_id"]), "state": job["state"], "deduplicated": dedup}
            job = await broker.get(args.job_id)
            return {k: str(v) if v is not None else None for k, v in job.items()} if job else None
        finally:
            await broker.close()

    print(json.dumps(asyncio.run(once()), ensure_ascii=False, indent=2))
    return 0

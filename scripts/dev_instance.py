#!/usr/bin/env python3
"""One-command isolated dev instance: its own Postgres databases + worker + API, structured JSON logs.

    python scripts/dev_instance.py up [--name NAME] [--db auto|admin|initdb|docker] [--no-api]
    python scripts/dev_instance.py status [--name NAME]       # processes, DB reachability, metrics snapshot
    python scripts/dev_instance.py env [--name NAME]          # `eval "$(...)"` to point the CLI at the instance
    python scripts/dev_instance.py down [--name NAME] [--purge]

Everything lives in var/dev/<name>/ (gitignored): instance.json, logs/*.jsonl, artifacts/, and for --db initdb
a private Postgres cluster. Each git worktree has its own var/, so instances never share state.

Database modes (auto picks the first that works):
  admin   PA_DEV_ADMIN_URL (or --admin-url) points at a server where we may CREATE DATABASE; we create
          pa_dev_<name>_<hash> and ..._eval there. This is what CI uses (postgres:17 service container).
  initdb  a private cluster under var/dev/<name>/pgdata on a free 127.0.0.1 port (needs initdb/pg_ctl,
          e.g. the postgresql-17 package; found via PATH, pg_config --bindir or /usr/lib/postgresql/*/bin).
  docker  a postgres:17 container bound to a free 127.0.0.1 port.

The instance has two databases: `database_url` (worker + API) and `eval_database_url`
(for scripts/runtime_check.py's system checks, which TRUNCATE and spawn their own workers).
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
ROLE = "personal_agent"


def instance_dir(name: str) -> Path:
    return ROOT / "var" / "dev" / name


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def uniq(name: str) -> str:
    return f"{name}_{hashlib.sha1(str(ROOT).encode()).hexdigest()[:6]}".replace("-", "_").lower()


def load(name: str) -> dict | None:
    p = instance_dir(name) / "instance.json"
    return json.loads(p.read_text()) if p.exists() else None


def save(info: dict) -> None:
    p = instance_dir(info["name"]) / "instance.json"
    p.write_text(json.dumps(info, indent=2) + "\n")
    p.chmod(0o600)   # contains the instance's database password


def alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:   # a zombie still "exists"; treat it as dead
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split()[2] != "Z"
    except OSError:
        return True


def wait_db(url: str, timeout: float = 30) -> None:
    import psycopg
    deadline = time.monotonic() + timeout
    while True:
        try:
            with psycopg.connect(url, connect_timeout=2):
                return
        except psycopg.OperationalError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.3)


def create_databases(admin_url: str, names: list[str]) -> None:
    import psycopg
    from psycopg import sql
    with psycopg.connect(admin_url, autocommit=True) as conn:
        for n in names:
            if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (n,)).fetchone():
                conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(n)))


def with_db(url: str, dbname: str) -> str:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    params = conninfo_to_dict(url)
    params["dbname"] = dbname
    return make_conninfo(**params)


# ---------------------------------------------------------------- database providers
def find_pg_bin() -> Path | None:
    if shutil.which("initdb") and shutil.which("pg_ctl"):
        return Path(shutil.which("initdb")).parent
    if shutil.which("pg_config"):
        out = subprocess.run(["pg_config", "--bindir"], capture_output=True, text=True).stdout.strip()
        if out and (Path(out) / "initdb").exists():
            return Path(out)
    candidates = sorted(glob.glob("/usr/lib/postgresql/*/bin/initdb"), reverse=True) + \
        sorted(glob.glob("/opt/homebrew/opt/postgresql@*/bin/initdb"), reverse=True) + ["/usr/local/pgsql/bin/initdb"]
    for c in candidates:
        if Path(c).exists():
            return Path(c).parent
    return None


def provision(mode: str, d: Path, name: str, admin_url: str | None) -> dict:
    db, edb = f"pa_dev_{uniq(name)}", f"pa_dev_{uniq(name)}_eval"
    if mode == "auto":
        mode = "admin" if admin_url else "initdb" if find_pg_bin() else "docker" if shutil.which("docker") else None
        if mode is None:
            sys.exit("no way to get a Postgres for the dev instance.\n"
                     "fix: set PA_DEV_ADMIN_URL=postgresql://user:pass@127.0.0.1:5432/postgres (a role that may "
                     "CREATE DATABASE), or install PostgreSQL 17 server binaries (initdb), or install Docker")
    if mode == "admin":
        if not admin_url:
            sys.exit("--db admin needs PA_DEV_ADMIN_URL or --admin-url")
        create_databases(admin_url, [db, edb])
        return {"mode": "admin", "database_url": with_db(admin_url, db), "eval_database_url": with_db(admin_url, edb),
                "drop_on_purge": [db, edb], "admin_url": admin_url}
    password = secrets.token_urlsafe(18)
    port = free_port()
    base = f"postgresql://{ROLE}:{password}@127.0.0.1:{port}"
    if mode == "initdb":
        bindir = find_pg_bin()
        if bindir is None:
            sys.exit("initdb not found. fix: install the PostgreSQL 17 server package, or use --db admin / --db docker")
        pgdata = d / "pgdata"
        pwfile = d / ".pgpass-init"
        pwfile.write_text(password + "\n")
        pwfile.chmod(0o600)
        subprocess.run([str(bindir / "initdb"), "-D", str(pgdata), "-U", ROLE, f"--pwfile={pwfile}",
                        "--auth-host=scram-sha-256", "--auth-local=peer", "-E", "UTF8", "--no-locale"],
                       check=True, stdout=subprocess.DEVNULL)
        pwfile.unlink()
        opts = f"-p {port} -c listen_addresses=127.0.0.1 -c unix_socket_directories='' -c fsync=off"
        subprocess.run([str(bindir / "pg_ctl"), "-D", str(pgdata), "-l", str(d / "logs" / "postgres.log"),
                        "-o", opts, "-w", "start"], check=True, stdout=subprocess.DEVNULL)
        info = {"mode": "initdb", "pg_bin": str(bindir), "pgdata": str(pgdata), "port": port}
    elif mode == "docker":
        cname = f"pa-dev-{uniq(name)}".replace("_", "-")
        subprocess.run(["docker", "run", "-d", "--name", cname, "-e", f"POSTGRES_USER={ROLE}",
                        "-e", f"POSTGRES_PASSWORD={password}", "-e", "POSTGRES_DB=postgres",
                        "-p", f"127.0.0.1:{port}:5432", "postgres:17"], check=True, stdout=subprocess.DEVNULL)
        info = {"mode": "docker", "container": cname, "port": port}
    else:
        sys.exit(f"unknown --db mode {mode!r}")
    wait_db(f"{base}/postgres", timeout=60)
    create_databases(f"{base}/postgres", [db, edb])
    info.update(database_url=f"{base}/{db}", eval_database_url=f"{base}/{edb}")
    return info


# ---------------------------------------------------------------- commands
def child_env(info: dict) -> dict:
    env = {**os.environ, "DATABASE_URL": info["database_url"], "PA_ARTIFACT_DIR": info["artifact_dir"],
           "PA_LOG_FORMAT": "json", "PYTHONUNBUFFERED": "1", "PA_AGENTS_TRACING": os.environ.get("PA_AGENTS_TRACING", "off")}
    for k in ("PA_FAULT_PAUSE_AT", "PA_FAULT_DIR"):   # fault injection never leaks into a dev instance
        env.pop(k, None)
    return env


def spawn(info: dict, name: str, args: list[str]) -> int:
    log = open(Path(info["log_dir"]) / f"{name}.jsonl", "ab")
    p = subprocess.Popen([PY, "-m", "personal_agent", *args], cwd=ROOT, env=child_env(info), stdin=subprocess.DEVNULL,
                         stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    return p.pid


def cmd_up(a) -> int:
    existing = load(a.name)
    if existing and alive(existing.get("pids", {}).get("worker")):
        print(json.dumps({"already_running": True, **public(existing)}, indent=2))
        return 0
    d = instance_dir(a.name)
    if existing:   # stale instance (e.g. after a reboot): stop leftovers, keep data
        stop(existing, purge=False, quiet=True)
    for sub in ("logs", "artifacts"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    if existing and existing.get("mode") == "initdb" and Path(existing["pgdata"]).exists():
        info = {k: existing[k] for k in ("mode", "pg_bin", "pgdata", "port", "database_url", "eval_database_url")}
        opts = f"-p {info['port']} -c listen_addresses=127.0.0.1 -c unix_socket_directories='' -c fsync=off"
        subprocess.run([str(Path(info["pg_bin"]) / "pg_ctl"), "-D", info["pgdata"], "-l",
                        str(d / "logs" / "postgres.log"), "-o", opts, "-w", "start"], check=True, stdout=subprocess.DEVNULL)
    else:
        info = provision(a.db, d, a.name, a.admin_url or os.environ.get("PA_DEV_ADMIN_URL"))
    info.update(name=a.name, root=str(ROOT), artifact_dir=str(d / "artifacts"), log_dir=str(d / "logs"),
                created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), pids={})
    save(info)
    out = subprocess.run([PY, "-m", "personal_agent", "migrate"], cwd=ROOT, env=child_env(info),
                         capture_output=True, text=True)
    if out.returncode:
        print(out.stdout, out.stderr, file=sys.stderr)
        return out.returncode
    info["pids"]["worker"] = spawn(info, "worker", ["worker", "--worker-id", f"dev-{a.name}",
                                                    "--concurrency", str(a.concurrency)])
    if not a.no_api:
        port = a.port or free_port()
        info["api_url"] = f"http://127.0.0.1:{port}"
        info["pids"]["api"] = spawn(info, "api", ["api", "--host", "127.0.0.1", "--port", str(port)])
    save(info)
    if not wait_ready(info):
        print(f"instance did not become ready; see {info['log_dir']}", file=sys.stderr)
        return 1
    print(json.dumps(public(info), indent=2))
    return 0


def wait_ready(info: dict, timeout: float = 30) -> bool:
    import httpx
    deadline = time.monotonic() + timeout
    log = Path(info["log_dir"]) / "worker.jsonl"
    while time.monotonic() < deadline:
        worker_ok = log.exists() and '"worker_start"' in log.read_text(errors="replace")
        api_ok = True
        if info.get("api_url"):
            try:
                api_ok = httpx.get(info["api_url"] + "/healthz", timeout=1).status_code == 200
            except httpx.HTTPError:
                api_ok = False
        if worker_ok and api_ok:
            return True
        if not alive(info["pids"].get("worker")):
            return False
        time.sleep(0.2)
    return False


def public(info: dict) -> dict:
    """instance.json minus secrets, for printing."""
    from psycopg.conninfo import conninfo_to_dict
    def redact(url):
        p = conninfo_to_dict(url)
        return f"{p.get('host')}:{p.get('port', 5432)}/{p.get('dbname')}"
    keys = ("name", "mode", "api_url", "pids", "log_dir", "artifact_dir", "created_at")
    return {**{k: info.get(k) for k in keys}, "database": redact(info["database_url"]),
            "eval_database": redact(info["eval_database_url"])}


def stop(info: dict, purge: bool, quiet: bool = False) -> None:
    for role in ("worker", "api"):
        pid = info.get("pids", {}).get(role)
        if alive(pid):
            os.killpg(pid, signal.SIGTERM)   # worker: stop leasing, finish in-flight jobs
            for _ in range(100):
                if not alive(pid):
                    break
                time.sleep(0.1)
            if alive(pid):
                os.killpg(pid, signal.SIGKILL)
            if not quiet:
                print(f"stopped {role} (pid {pid})")
    if info.get("mode") == "initdb" and Path(info["pgdata"], "postmaster.pid").exists():
        subprocess.run([str(Path(info["pg_bin"]) / "pg_ctl"), "-D", info["pgdata"], "-m", "fast", "-w", "stop"],
                       stdout=subprocess.DEVNULL)
        if not quiet:
            print("stopped private postgres")
    elif info.get("mode") == "docker":
        subprocess.run(["docker", "rm", "-f", info["container"]], stdout=subprocess.DEVNULL)
    elif info.get("mode") == "admin" and purge:
        import psycopg
        from psycopg import sql
        with psycopg.connect(info["admin_url"], autocommit=True) as conn:
            for n in info.get("drop_on_purge", []):
                conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(n)))
    if purge:
        shutil.rmtree(instance_dir(info["name"]), ignore_errors=True)
        if not quiet:
            print(f"removed {instance_dir(info['name'])}")


def cmd_down(a) -> int:
    info = load(a.name)
    if info is None:
        print(f"no instance named {a.name!r}")
        return 0
    stop(info, a.purge)
    return 0


def cmd_status(a) -> int:
    info = load(a.name)
    if info is None:
        print(f"no instance named {a.name!r}; start one with `make dev-up`")
        return 1
    status = public(info)
    status["alive"] = {role: alive(pid) for role, pid in info.get("pids", {}).items()}
    try:
        import asyncio
        sys.path.insert(0, str(ROOT))
        from personal_agent.queue.broker import PgBroker
        from personal_agent.queue.stats import snapshot

        async def snap():
            b = await PgBroker(info["database_url"], max_size=1).open()
            try:
                return await snapshot(b)
            finally:
                await b.close()
        status["metrics"] = asyncio.run(snap())
    except Exception as exc:  # noqa: BLE001  status must still print when the DB is down
        status["metrics_error"] = repr(exc)
    print(json.dumps(status, indent=2, default=str))
    return 0 if all(status["alive"].values()) else 1


def cmd_env(a) -> int:
    info = load(a.name)
    if info is None:
        print(f"echo 'no instance named {a.name}' >&2; false")
        return 1
    import shlex
    for k, v in (("DATABASE_URL", info["database_url"]), ("EVAL_DATABASE_URL", info["eval_database_url"]),
                 ("PA_ARTIFACT_DIR", info["artifact_dir"]), ("PA_DEV_API_URL", info.get("api_url", ""))):
        print(f"export {k}={shlex.quote(v)}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for n in ("up", "down", "status", "env"):
        p = sub.add_parser(n)
        p.add_argument("--name", default=os.environ.get("PA_DEV_NAME", "default"))
        if n == "up":
            p.add_argument("--db", default="auto", choices=["auto", "admin", "initdb", "docker"])
            p.add_argument("--admin-url")
            p.add_argument("--port", type=int, help="API port (default: a free one)")
            p.add_argument("--concurrency", type=int, default=4)
            p.add_argument("--no-api", action="store_true")
        if n == "down":
            p.add_argument("--purge", action="store_true", help="also delete data (dir, private cluster, databases)")
    a = ap.parse_args(argv)
    return {"up": cmd_up, "down": cmd_down, "status": cmd_status, "env": cmd_env}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())

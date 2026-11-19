#!/usr/bin/env bash
# 在本机后台启动 API + worker（读取 .env 里的 DATABASE_URL）。日志在 var/log/，pid 在 var/run/。
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
PORT=${PORT:-8080}
mkdir -p var/log var/run
"$PY" -m personal_agent migrate
setsid nohup "$PY" -m personal_agent api --host 127.0.0.1 --port "$PORT" >> var/log/api.log 2>&1 < /dev/null &
echo $! > var/run/api.pid
setsid nohup "$PY" -m personal_agent worker --concurrency "${CONCURRENCY:-4}" --worker-id "local-worker-1" >> var/log/worker.log 2>&1 < /dev/null &
echo $! > var/run/worker.pid
echo "api pid $(cat var/run/api.pid) on 127.0.0.1:$PORT; worker pid $(cat var/run/worker.pid)"

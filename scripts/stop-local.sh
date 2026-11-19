#!/usr/bin/env bash
# 优雅停止（SIGTERM：worker 不再领新任务，等在途任务结束）。
cd "$(dirname "$0")/.."
for name in worker api; do
  [ -f "var/run/$name.pid" ] && kill -TERM "$(cat var/run/$name.pid)" 2>/dev/null && echo "stopped $name"
  rm -f "var/run/$name.pid"
done

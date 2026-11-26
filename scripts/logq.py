#!/usr/bin/env python3
"""Query a dev instance's structured logs (one JSON object per line) without regexes.

    python scripts/logq.py --event acked --last 20
    python scripts/logq.py --job <job_id>                      # full timeline of one job across all logs
    python scripts/logq.py --event failed --where state=dead --fields ts,job_id,error
    python scripts/logq.py --count-by event --since 300        # events in the last 5 minutes

Reads var/dev/<name>/logs/*.jsonl (see scripts/dev_instance.py). Lines that are not JSON are skipped
(and counted on stderr), so a stray traceback never breaks a query.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(log_dir: Path, sources: list[str] | None):
    skipped = 0
    for path in sorted(log_dir.glob("*.jsonl")):
        if sources and path.stem not in sources:
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                obj = json.loads(line)
            except ValueError:
                skipped += 1
                continue
            if isinstance(obj, dict):
                obj.setdefault("source", path.stem)
                yield obj
    if skipped:
        print(f"({skipped} non-JSON line(s) skipped)", file=sys.stderr)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--name", default=os.environ.get("PA_DEV_NAME", "default"), help="dev instance name")
    ap.add_argument("--log-dir", help="read this directory instead of var/dev/<name>/logs")
    ap.add_argument("--source", nargs="*", help="only these log files (worker, api)")
    ap.add_argument("--event", nargs="*", help="only these event names")
    ap.add_argument("--job", help="only records with this job_id")
    ap.add_argument("--where", nargs="*", default=[], help="key=value filters (string compare)")
    ap.add_argument("--since", type=float, help="only records from the last N seconds")
    ap.add_argument("--fields", help="comma-separated fields to print")
    ap.add_argument("--count-by", help="print counts grouped by this field instead of records")
    ap.add_argument("--last", type=int, help="only the last N matching records")
    a = ap.parse_args(argv)
    log_dir = Path(a.log_dir) if a.log_dir else ROOT / "var" / "dev" / a.name / "logs"
    if not log_dir.exists():
        print(f"no logs at {log_dir}; start an instance with `make dev-up`", file=sys.stderr)
        return 1
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=a.since) if a.since else None
    where = [w.split("=", 1) for w in a.where]
    rows = []
    for r in read(log_dir, a.source):
        if a.event and r.get("event") not in a.event:
            continue
        if a.job and str(r.get("job_id")) != a.job:
            continue
        if any(str(r.get(k)) != v for k, v in where):
            continue
        if cutoff and "ts" in r and datetime.fromisoformat(r["ts"]) < cutoff:
            continue
        rows.append(r)
    rows.sort(key=lambda r: r.get("ts", ""))
    if a.last:
        rows = rows[-a.last:]
    if a.count_by:
        for value, n in Counter(str(r.get(a.count_by)) for r in rows).most_common():
            print(json.dumps({a.count_by: value, "count": n}, ensure_ascii=False))
        return 0
    fields = a.fields.split(",") if a.fields else None
    for r in rows:
        print(json.dumps({k: r.get(k) for k in fields} if fields else r, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

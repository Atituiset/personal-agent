#!/usr/bin/env python3
"""回归门禁：拿本次 results.json 和 evals/baseline.json 比，有退步就非零退出，让 CI 失败。

    .venv/bin/python -m evals.gate --baseline evals/baseline.json --current test-results/evals-results.json --k 3

检查（改编自 agent-engineering-handbook 第 16 章 harness/compare.py）：
  1. 回归集 pass^k（可靠性：每次都要过）掉了超过 --max-drop；
  2. 能力集 pass@k（能力上限）掉了超过 --max-drop；
  3. 回归集里任一任务的 pass@1 比基线低（总分会掩盖个别任务）；基线里有、本次缺的任务也算失败；
  4. 出现任何过程违规（越权副作用、声称做了但 effects 表里没有、白名单外产物泄露）；
  5. 成本：每次试验平均 token 比基线涨超过 --max-cost-increase（默认 25%）；
  6. 延迟默认只报告不拦（CI 机器快慢不一）；给了 --max-latency-ratio 才拦 p95。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def check(base: dict, cur: dict, k: int, tol: float, max_cost: float, max_lat: float | None) -> list[str]:
    problems = []
    for suite, metric in (("regression", f"pass^{k}"), ("capability", f"pass@{k}")):
        b = base["summary"]["suites"].get(suite, {}).get(metric)
        c = cur["summary"]["suites"].get(suite, {}).get(metric)
        if b is None:
            continue
        if c is None:
            problems.append(f"{suite} {metric} missing from current run (run with --trials >= {k} --k ... {k})")
            continue
        status = "DROP" if c < b - tol else "ok"
        print(f"{suite:>10} {metric:>7}: baseline={b:.3f} current={c:.3f} [{status}]")
        if status == "DROP":
            problems.append(f"{suite} {metric} dropped {b:.3f} -> {c:.3f}")

    for tid, bt in base["per_task"].items():
        if bt["kind"] != "regression":
            continue
        ct = cur["per_task"].get(tid)
        if ct is None:
            problems.append(f"regression task {tid} missing from current run")
        elif ct["pass@1"] < bt["pass@1"] - tol:
            why = f" ({ct['first_failure'][0]})" if ct.get("first_failure") else ""
            problems.append(f"regression task {tid} pass@1 {bt['pass@1']:.3f} -> {ct['pass@1']:.3f}{why}")

    v = cur["summary"].get("trials_with_violations", 0)
    if v:
        bad = [tid for tid, t in cur["per_task"].items() if t.get("violations")]
        problems.append(f"{v} trial(s) violated process rules: {bad}")

    for suite, bs in base["summary"]["suites"].items():
        cs = cur["summary"]["suites"].get(suite)
        if not cs:
            continue
        bt, ct = bs["tokens_mean"], cs["tokens_mean"]
        rise = (ct - bt) / bt if bt else 0.0
        status = "COST" if rise > max_cost else "ok"
        print(f"{suite:>10} tokens/trial: baseline={bt} current={ct} ({rise:+.1%}) [{status}]")
        if status == "COST":
            problems.append(f"{suite} tokens/trial rose {rise:+.1%} (> {max_cost:.0%})")
        bl, cl = bs.get("exec_p95_s"), cs.get("exec_p95_s")
        if bl and cl:
            ratio = cl / bl
            lat_bad = max_lat is not None and ratio > max_lat
            print(f"{suite:>10} exec p95: baseline={bl}s current={cl}s (x{ratio:.2f})"
                  f" [{'SLOW' if lat_bad else 'ok' if max_lat else 'report only'}]")
            if lat_bad:
                problems.append(f"{suite} exec p95 x{ratio:.2f} > x{max_lat}")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", default="evals/baseline.json")
    ap.add_argument("--current", required=True)
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--max-drop", type=float, default=0.02)
    ap.add_argument("--max-cost-increase", type=float, default=0.25)
    ap.add_argument("--max-latency-ratio", type=float)
    args = ap.parse_args(argv)
    base = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    cur = json.loads(Path(args.current).read_text(encoding="utf-8"))
    if base["meta"].get("model") != cur["meta"].get("model") or base["meta"].get("profile") != cur["meta"].get("profile"):
        print(f"note: baseline model={base['meta'].get('model')}/{base['meta'].get('profile')} "
              f"current={cur['meta'].get('model')}/{cur['meta'].get('profile')}")
    problems = check(base, cur, args.k, args.max_drop, args.max_cost_increase, args.max_latency_ratio)
    if problems:
        print("\nREGRESSION GATE FAILED:")
        for p in problems:
            print("  -", p)
        return 1
    print("\nregression gate passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

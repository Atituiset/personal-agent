#!/usr/bin/env python3
"""裁判校准：拿裁判判定和人工标签比，算一致率、Cohen's kappa、失败召回率/精确率；检查两两比较的位置偏差。

    .venv/bin/python -m evals.judge.calibrate --judge judge_rubric_v1 --compare judge_keyword_v0 \
        --min-kappa 0.6 --min-fail-recall 0.8

方法和指标沿用 agent-engineering-handbook 第 16 章 judge/calibrate.py。labels.csv 和 pairwise.csv 的标签
由仓库作者按 rubric 手工标注，样本很小（24 + 6 条），只用来演示流程；正式用之前请换成多人标注的样本。
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

from .judges import judge_keyword_v0, judge_rubric_v1

HERE = Path(__file__).resolve().parent
JUDGES = {"judge_rubric_v1": judge_rubric_v1, "judge_keyword_v0": judge_keyword_v0}
POS = "fail"


def kappa(h: list[str], j: list[str]) -> float:
    n = len(h)
    po = sum(a == b for a, b in zip(h, j)) / n
    hc, jc = Counter(h), Counter(j)
    pe = sum(hc[l] * jc[l] for l in set(h) | set(j)) / (n * n)
    return 1.0 if pe == 1 else (po - pe) / (1 - pe)


def report(name: str, rows: list[dict]) -> dict:
    fn = JUDGES[name]
    h = [r["human"] for r in rows]
    j = [fn(r["rubric"], r["reply"]) for r in rows]
    cm = Counter(zip(h, j))
    tp, fn_, fp, tn = cm[(POS, POS)], cm[(POS, "pass")], cm[("pass", POS)], cm[("pass", "pass")]
    res = {"judge": name, "n": len(rows), "agreement": (tp + tn) / len(rows), "kappa": kappa(h, j),
           "fail_recall": tp / (tp + fn_) if tp + fn_ else float("nan"),
           "fail_precision": tp / (tp + fp) if tp + fp else float("nan"),
           "confusion": {"tp": tp, "fn": fn_, "fp": fp, "tn": tn},
           "disagreements": [r["id"] for r, a, b in zip(rows, h, j) if a != b]}
    print(f"[{name}] n={res['n']} agreement={res['agreement']:.3f} kappa={res['kappa']:.3f} "
          f"fail_recall={res['fail_recall']:.3f} fail_precision={res['fail_precision']:.3f} "
          f"confusion={res['confusion']} disagreements={res['disagreements']}")
    return res


def pairwise(name: str, rows: list[dict]) -> tuple[int, int, int]:
    """把 A/B 换位各判一次：两次都选对才算对；两次结论不一致说明有位置偏差。"""
    fn = JUDGES[name]
    score = {"pass": 1, "fail": 0}
    correct = flips = 0
    for r in rows:
        a, b = score[fn(r["rubric"], r["reply_a"])], score[fn(r["rubric"], r["reply_b"])]
        b2, a2 = score[fn(r["rubric"], r["reply_b"])], score[fn(r["rubric"], r["reply_a"])]
        first = "a" if a > b else "b" if b > a else "tie"
        second = "a" if a2 > b2 else "b" if b2 > a2 else "tie"
        flips += first != second
        correct += first == second == r["human_better"]
    print(f"[{name}] pairwise: {correct}/{len(rows)} correct in both orders, {flips} position flips")
    return correct, len(rows), flips


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default=str(HERE / "labels.csv"))
    ap.add_argument("--pairwise", default=str(HERE / "pairwise.csv"))
    ap.add_argument("--judge", default="judge_rubric_v1")
    ap.add_argument("--compare")
    ap.add_argument("--min-kappa", type=float, default=0.6)
    ap.add_argument("--min-fail-recall", type=float, default=0.8)
    args = ap.parse_args(argv)
    rows = list(csv.DictReader(open(args.labels, encoding="utf-8")))
    main_res = report(args.judge, rows)
    if args.compare:
        report(args.compare, rows)
    pw = list(csv.DictReader(open(args.pairwise, encoding="utf-8")))
    correct, n, flips = pairwise(args.judge, pw)
    problems = []
    if main_res["kappa"] < args.min_kappa:
        problems.append(f"kappa {main_res['kappa']:.3f} < {args.min_kappa}")
    if main_res["fail_recall"] < args.min_fail_recall:
        problems.append(f"fail recall {main_res['fail_recall']:.3f} < {args.min_fail_recall}")
    if flips:
        problems.append(f"{flips} position flips in pairwise comparison")
    if problems:
        print("JUDGE CALIBRATION FAILED:", *problems, sep="\n  - ")
        return 1
    print(f"judge {args.judge} calibrated: kappa >= {args.min_kappa}, fail recall >= {args.min_fail_recall}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

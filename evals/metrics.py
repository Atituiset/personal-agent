"""评测指标：pass@k、pass^k、置信区间、成本与延迟汇总。只依赖标准库。

来自 agent-engineering-handbook 第 16 章 examples/evals/harness/metrics.py（MIT），原样复用。

pass@k：k 次里至少成功一次的概率，衡量能力上限。
pass^k：k 次全部成功的概率，衡量可靠性（τ-bench 提出）。

两者都用「跑了 n 次、成功 c 次」的无偏估计，而不是直接拿前 k 次的结果：
    pass@k = 1 - C(n-c, k) / C(n, k)
    pass^k = C(c, k) / C(n, k)
要求 k <= n。
"""
from __future__ import annotations

import math
from math import comb
from statistics import median
from typing import Iterable, Sequence


def pass_at_k(n: int, c: int, k: int) -> float:
    if not 0 <= c <= n:
        raise ValueError(f"need 0 <= c <= n, got n={n} c={c}")
    if not 1 <= k <= n:
        raise ValueError(f"need 1 <= k <= n, got n={n} k={k}")
    if n - c < k:
        return 1.0
    return 1.0 - comb(n - c, k) / comb(n, k)


def pass_hat_k(n: int, c: int, k: int) -> float:
    if not 0 <= c <= n:
        raise ValueError(f"need 0 <= c <= n, got n={n} c={c}")
    if not 1 <= k <= n:
        raise ValueError(f"need 1 <= k <= n, got n={n} k={k}")
    return comb(c, k) / comb(n, k)


def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """二项比例的 Wilson 95% 置信区间。样本小的时候比正态近似稳。"""
    if total == 0:
        return (0.0, 1.0)
    p = successes / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def percentile(values: Sequence[float], q: float) -> float:
    """最近秩法分位数，q 取 0–100。"""
    if not values:
        return float("nan")
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]


def summarize_costs(latencies_s: Iterable[float], tokens: Iterable[int]) -> dict:
    lat = list(latencies_s)
    tok = list(tokens)
    return {
        "latency_p50_s": round(median(lat), 4) if lat else None,
        "latency_p95_s": round(percentile(lat, 95), 4) if lat else None,
        "tokens_mean": round(sum(tok) / len(tok), 1) if tok else None,
        "tokens_total": sum(tok),
    }

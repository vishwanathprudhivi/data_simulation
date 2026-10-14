"""Closed-form M/M/c results (Erlang C) used by validation tests (AC-EXP-01)."""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = ["MMcResult", "erlang_c"]


@dataclass(frozen=True)
class MMcResult:
    utilization: float  # rho = lambda / (c mu)
    prob_wait: float  # Erlang C
    mean_wait: float  # Wq
    mean_queue_length: float  # Lq
    mean_number_in_system: float  # L


def erlang_c(servers: int, arrival_rate: float, service_rate: float) -> MMcResult:
    c, lam, mu = servers, arrival_rate, service_rate
    a = lam / mu
    rho = a / c
    if rho >= 1:
        raise ValueError(f"unstable queue: rho = {rho:.3f} >= 1")
    top = a**c / math.factorial(c) / (1 - rho)
    bottom = sum(a**k / math.factorial(k) for k in range(c)) + top
    pw = top / bottom
    wq = pw / (c * mu - lam)
    lq = lam * wq
    return MMcResult(rho, pw, wq, lq, lq + a)

"""Replication statistics (FR-EXP-04/05/10)."""

from __future__ import annotations

import math
import warnings
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from scipy import stats

__all__ = ["CIEstimate", "LowReplicationWarning", "mean_ci", "paired_ci", "welch_ci"]

#: Below this many replications a CI is flagged as not meaningful (FR-EXP-10).
MIN_MEANINGFUL_REPLICATIONS = 5


class LowReplicationWarning(UserWarning):
    pass


@dataclass(frozen=True)
class CIEstimate:
    mean: float
    half_width: float
    low: float
    high: float
    n: int
    sd: float
    confidence: float
    warnings: tuple[str, ...] = ()

    @property
    def excludes_zero(self) -> bool:
        return self.low > 0 or self.high < 0

    @property
    def relative_half_width(self) -> float:
        return abs(self.half_width / self.mean) if self.mean else math.inf


def _warn(notes: Sequence[str]) -> None:
    for note in notes:
        warnings.warn(note, LowReplicationWarning, stacklevel=3)


def mean_ci(values: Sequence[float], confidence: float = 0.95) -> CIEstimate:
    """Student-t confidence interval on replication means (FR-EXP-04)."""
    x = np.asarray([v for v in values if not math.isnan(v)], dtype=float)
    n = len(x)
    notes: list[str] = []
    if n < len(values):
        notes.append(f"{len(values) - n} replication(s) had no value (NaN) and were excluded")
    if n == 0:
        _warn([*notes, "no values"])
        return CIEstimate(
            math.nan, math.nan, math.nan, math.nan, 0, math.nan, confidence, (*notes, "no values")
        )
    mean = float(x.mean())
    if n < 2:
        notes.append("n < 2: confidence interval undefined")
        _warn(notes)
        return CIEstimate(
            mean, math.inf, -math.inf, math.inf, n, math.nan, confidence, tuple(notes)
        )
    sd = float(x.std(ddof=1))
    if n < MIN_MEANINGFUL_REPLICATIONS:
        notes.append(
            f"only {n} replications; CI is not meaningful (need >= {MIN_MEANINGFUL_REPLICATIONS})"
        )
    if sd == 0.0:
        notes.append("zero variance across replications (degenerate)")
    _warn(notes)
    half = float(stats.t.ppf(0.5 + confidence / 2, n - 1) * sd / math.sqrt(n))
    return CIEstimate(mean, half, mean - half, mean + half, n, sd, confidence, tuple(notes))


def paired_ci(a: Sequence[float], b: Sequence[float], confidence: float = 0.95) -> CIEstimate:
    """CI of the mean paired difference ``b - a`` (FR-EXP-05; use with CRN)."""
    if len(a) != len(b):
        raise ValueError(f"paired comparison needs equal lengths ({len(a)} vs {len(b)})")
    return mean_ci([y - x for x, y in zip(a, b)], confidence)


def welch_ci(a: Sequence[float], b: Sequence[float], confidence: float = 0.95) -> CIEstimate:
    """CI of ``mean(b) - mean(a)`` for independent samples (Welch-Satterthwaite)."""
    x = np.asarray([v for v in a if not math.isnan(v)], dtype=float)
    y = np.asarray([v for v in b if not math.isnan(v)], dtype=float)
    if len(x) < 2 or len(y) < 2:
        _warn(["Welch interval needs >= 2 values per group"])
        diff = float(y.mean() - x.mean()) if len(x) and len(y) else math.nan
        return CIEstimate(
            diff,
            math.inf,
            -math.inf,
            math.inf,
            min(len(x), len(y)),
            math.nan,
            confidence,
            ("insufficient replications",),
        )
    vx, vy = x.var(ddof=1) / len(x), y.var(ddof=1) / len(y)
    se = math.sqrt(vx + vy)
    diff = float(y.mean() - x.mean())
    notes: list[str] = []
    if se == 0.0:
        notes.append("zero variance in both groups (degenerate)")
        _warn(notes)
        return CIEstimate(diff, 0.0, diff, diff, min(len(x), len(y)), 0.0, confidence, tuple(notes))
    dof = (vx + vy) ** 2 / (vx**2 / (len(x) - 1) + vy**2 / (len(y) - 1))
    half = float(stats.t.ppf(0.5 + confidence / 2, dof) * se)
    return CIEstimate(
        diff, half, diff - half, diff + half, min(len(x), len(y)), se, confidence, tuple(notes)
    )

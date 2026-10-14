"""Replication statistics (FR-EXP-04/05/10)."""

from __future__ import annotations

import math
import warnings
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from scipy import stats

__all__ = ["CIEstimate", "LowReplicationWarning", "mean_ci", "paired_ci"]

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

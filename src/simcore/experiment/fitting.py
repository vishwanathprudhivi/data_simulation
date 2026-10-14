"""Input modeling: fit candidate distributions to empirical samples (FR-EXP-08, AC-EXP-04).

Candidates are fitted by maximum likelihood (location fixed at 0 for positive
families), ranked by AIC, and reported with Kolmogorov-Smirnov and
Anderson-Darling statistics plus Q-Q points. The winner is emitted as a
``DistributionSpec`` that scenarios and behaviors consume directly.
"""

from __future__ import annotations

import csv
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
from pydantic import TypeAdapter
from scipy import stats

from simcore.specs.distributions import DistributionSpec, unit_factor

FloatArray = npt.NDArray[np.float64]

__all__ = ["CANDIDATES", "FitCandidate", "FitReport", "fit_distributions", "read_samples"]

CANDIDATES = ("exponential", "lognormal", "gamma", "weibull", "normal")
_POSITIVE = {"exponential", "lognormal", "gamma", "weibull"}


@dataclass(frozen=True)
class FitCandidate:
    name: str
    params: dict[str, float]
    spec: dict[str, Any]
    n_params: int
    loglik: float
    aic: float
    bic: float
    ks_stat: float
    ks_pvalue: float
    ad_stat: float
    qq: list[tuple[float, float]] = field(default_factory=list)

    def distribution(self) -> DistributionSpec:
        return TypeAdapter(DistributionSpec).validate_python(self.spec)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "params": self.params,
            "spec": self.spec,
            "loglik": self.loglik,
            "aic": self.aic,
            "bic": self.bic,
            "ks_stat": self.ks_stat,
            "ks_pvalue": self.ks_pvalue,
            "ad_stat": self.ad_stat,
            "qq": [list(p) for p in self.qq],
        }


@dataclass(frozen=True)
class FitReport:
    n: int
    summary: dict[str, float]
    candidates: list[FitCandidate]  # ranked best first (lowest AIC)
    empirical: dict[str, Any]
    skipped: dict[str, str]

    @property
    def best(self) -> FitCandidate:
        return self.candidates[0]

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "summary": self.summary,
            "ranking": "AIC (lower is better); KS and AD reported for goodness of fit",
            "best": self.best.name,
            "candidates": [c.to_dict() for c in self.candidates],
            "empirical": self.empirical,
            "skipped": self.skipped,
        }


def _frozen(name: str, x: FloatArray) -> tuple[Any, dict[str, float], dict[str, Any], int]:
    """Return (scipy frozen dist, params, spec dict, n_params) fitted by MLE."""
    if name == "exponential":
        mean = float(x.mean())
        return stats.expon(scale=mean), {"mean": mean}, {"kind": name, "mean": mean}, 1
    if name == "lognormal":
        logs = np.log(x)
        mu, sigma = float(logs.mean()), float(logs.std(ddof=0))
        return (
            stats.lognorm(s=sigma, scale=math.exp(mu)),
            {"mu": mu, "sigma": sigma},
            {"kind": name, "mu": mu, "sigma": sigma},
            2,
        )
    if name == "gamma":
        a, _, scale = stats.gamma.fit(x, floc=0)
        return (
            stats.gamma(a, scale=scale),
            {"shape": float(a), "scale": float(scale)},
            {"kind": name, "shape": float(a), "scale": float(scale)},
            2,
        )
    if name == "weibull":
        c, _, scale = stats.weibull_min.fit(x, floc=0)
        return (
            stats.weibull_min(c, scale=scale),
            {"shape": float(c), "scale": float(scale)},
            {"kind": name, "shape": float(c), "scale": float(scale)},
            2,
        )
    if name == "normal":
        m, sd = float(x.mean()), float(x.std(ddof=0))
        return stats.norm(m, sd), {"mean": m, "sd": sd}, {"kind": name, "mean": m, "sd": sd}, 2
    raise ValueError(f"unknown candidate {name!r}; choose from {CANDIDATES}")


def _anderson_darling(sorted_x: FloatArray, dist: Any) -> float:
    n = len(sorted_x)
    cdf = np.clip(dist.cdf(sorted_x), 1e-300, 1 - 1e-16)
    i = np.arange(1, n + 1)
    return float(-n - np.sum((2 * i - 1) * (np.log(cdf) + np.log1p(-cdf[::-1]))) / n)


def fit_distributions(
    samples: Sequence[float],
    candidates: Sequence[str] = CANDIDATES,
    unit: str | None = None,
    qq_points: int = 50,
) -> FitReport:
    """Fit ``candidates`` to ``samples`` (converted to SI using ``unit``) and rank them."""
    factor = unit_factor(unit) if unit else 1.0
    x = np.asarray([float(s) * factor for s in samples if not math.isnan(float(s))])
    if len(x) < 10:
        raise ValueError(f"need at least 10 samples to fit distributions (got {len(x)})")
    xs = np.sort(x)
    n = len(x)
    probs = (np.arange(1, qq_points + 1) - 0.5) / qq_points
    emp_q = np.quantile(xs, probs)
    results: list[FitCandidate] = []
    skipped: dict[str, str] = {}
    for name in candidates:
        if name in _POSITIVE and xs[0] <= 0:
            skipped[name] = "requires strictly positive samples"
            continue
        try:
            dist, params, spec, k = _frozen(name, x)
            loglik = float(np.sum(dist.logpdf(x)))
        except (ValueError, FloatingPointError, RuntimeError) as exc:
            skipped[name] = f"fit failed: {exc}"
            continue
        if not math.isfinite(loglik):
            skipped[name] = "non-finite likelihood"
            continue
        ks = stats.kstest(x, dist.cdf)
        results.append(
            FitCandidate(
                name=name,
                params=params,
                spec=spec,
                n_params=k,
                loglik=loglik,
                aic=2 * k - 2 * loglik,
                bic=k * math.log(n) - 2 * loglik,
                ks_stat=float(ks.statistic),
                ks_pvalue=float(ks.pvalue),
                ad_stat=_anderson_darling(xs, dist),
                qq=[(float(a), float(b)) for a, b in zip(emp_q, dist.ppf(probs))],
            )
        )
    if not results:
        raise ValueError(f"no candidate could be fitted: {skipped}")
    results.sort(key=lambda c: (c.aic, c.name))
    summary = {
        "mean": float(x.mean()),
        "sd": float(x.std(ddof=1)),
        "min": float(xs[0]),
        "p50": float(np.median(xs)),
        "max": float(xs[-1]),
        "cv": float(x.std(ddof=1) / x.mean()) if x.mean() else math.nan,
    }
    # Empirical fallback: a percentile grid keeps the spec small yet faithful.
    grid = np.quantile(xs, np.linspace(0, 1, 101)).tolist()
    empirical = {"kind": "empirical", "values": [float(v) for v in grid]}
    return FitReport(n, summary, results, empirical, skipped)


def read_samples(path: Path, column: str | None = None) -> list[float]:
    """Read one numeric column from a CSV (header row) or a plain one-value-per-line file."""
    text = path.read_text(encoding="utf-8").splitlines()
    if not text:
        raise ValueError(f"{path} is empty")
    first = text[0].split(",")
    try:
        [float(v) for v in first]
        has_header = False
    except ValueError:
        has_header = True
    if not has_header:
        if len(first) > 1 and column is None:
            raise ValueError(f"{path} has several columns and no header; pass --column")
        return [float(line.split(",")[0]) for line in text if line.strip()]
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        cols = reader.fieldnames or []
        col = column or (cols[0] if len(cols) == 1 else None)
        if col is None or col not in cols:
            raise ValueError(f"choose a column with --column; available: {cols}")
        values = []
        for n, row in enumerate(reader, start=2):
            raw = (row.get(col) or "").strip()
            if raw == "":
                continue
            try:
                values.append(float(raw))
            except ValueError as exc:
                raise ValueError(f"{path}:{n}: column {col!r}: {raw!r} is not a number") from exc
        return values

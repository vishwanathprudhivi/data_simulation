"""Calibration of model parameters against observed KPIs (FR-INT-05, feeds FR-DOM-06).

A deterministic grid search: every combination of candidate values is evaluated
with common random numbers and scored by the weighted sum of squared relative
errors to the targets. Targets may come from ``physics``-mode runs or real data.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from simcore.experiment.evaluator import Evaluator

__all__ = ["CalibrationResult", "calibrate"]


@dataclass(frozen=True)
class CalibrationResult:
    best_params: dict[str, Any]
    best_score: float
    trace: list[dict[str, Any]] = field(default_factory=list)  # every candidate, in order

    def to_dict(self) -> dict[str, Any]:
        return {"best_params": self.best_params, "best_score": self.best_score, "trace": self.trace}


def calibrate(
    evaluator: Evaluator,
    grid: Mapping[str, Sequence[Any]],
    targets: Mapping[str, float],
    *,
    weights: Mapping[str, float] | None = None,
    replications: int = 5,
    seed: int = 0,
    fixed: Mapping[str, Any] | None = None,
) -> CalibrationResult:
    """Grid-search ``grid`` (override path -> candidate values) to match ``targets``."""
    if not grid:
        raise ValueError("calibration grid is empty")
    if not targets:
        raise ValueError("no calibration targets given")
    paths = sorted(grid)
    trace: list[dict[str, Any]] = []
    best: tuple[float, dict[str, Any]] | None = None
    for combo in itertools.product(*(grid[p] for p in paths)):
        params = {**(fixed or {}), **dict(zip(paths, combo))}
        ev = evaluator.evaluate(params, replications=replications, seed=seed, kpis=sorted(targets))
        errors = {}
        score = 0.0
        for kpi, target in sorted(targets.items()):
            sim = ev.kpis[kpi].mean
            denom = abs(target) if target else 1.0
            err = (sim - target) / denom
            errors[kpi] = {"simulated": sim, "target": target, "relative_error": err}
            score += (weights or {}).get(kpi, 1.0) * err * err
        if math.isnan(score):
            score = math.inf
        trace.append({"params": dict(zip(paths, combo)), "score": score, "errors": errors})
        if best is None or score < best[0]:
            best = (score, dict(zip(paths, combo)))
    assert best is not None
    return CalibrationResult(best[1], best[0], trace)

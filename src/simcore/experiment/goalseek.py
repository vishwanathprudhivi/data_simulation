"""Goal-seeking variants (FR-SCG-05): bounded search with a transparent trace.

"Smallest fleet that meets the 95 % on-time target" is a search over one lever's
candidate values. The search assumes the metric is monotone in the lever and
bisects the sorted candidates; a candidate *meets* the target only when the whole
confidence interval is on the right side of it (conservative). Every evaluation
is recorded in the trace, and the run budget is a hard cap.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from simcore.experiment.evaluator import Evaluator

__all__ = ["GoalSeekResult", "TraceStep", "goal_seek"]


@dataclass(frozen=True)
class TraceStep:
    step: int
    value: Any
    mean: float
    ci: tuple[float, float]
    n: int
    meets: bool
    cached: bool

    def to_dict(self) -> dict[str, Any]:
        def num(x: float) -> float | None:
            return None if math.isnan(x) or math.isinf(x) else x

        return {
            "step": self.step,
            "value": self.value,
            "mean": num(self.mean),
            "ci": [num(self.ci[0]), num(self.ci[1])],
            "n": self.n,
            "meets": self.meets,
            "cached": self.cached,
        }


@dataclass
class GoalSeekResult:
    lever_path: str
    metric: str
    target: float
    sense: str
    best: Any | None
    status: str  # "found" | "infeasible" | "budget_exhausted"
    runs_used: int
    max_runs: int
    trace: list[TraceStep] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lever_path": self.lever_path,
            "metric": self.metric,
            "target": self.target,
            "sense": self.sense,
            "best": self.best,
            "status": self.status,
            "runs_used": self.runs_used,
            "max_runs": self.max_runs,
            "trace": [t.to_dict() for t in self.trace],
            "notes": self.notes,
        }


def goal_seek(
    evaluator: Evaluator,
    lever_path: str,
    candidates: Sequence[Any],
    metric: str,
    target: float,
    *,
    sense: str = ">=",
    minimize_lever: bool = True,
    replications: int = 5,
    max_runs: int = 60,
    seed: int = 0,
) -> GoalSeekResult:
    """Bisect ``candidates`` for the extreme value whose CI meets ``metric sense target``."""
    if sense not in (">=", "<="):
        raise ValueError("sense must be '>=' or '<='")
    order = sorted(candidates)
    if not minimize_lever:
        order = order[::-1]
    result = GoalSeekResult(lever_path, metric, target, sense, None, "infeasible", 0, max_runs)
    if not order:
        result.notes.append("no candidate values")
        return result
    seen: dict[int, bool] = {}

    def check(i: int) -> bool | None:
        if i in seen:
            return seen[i]
        if result.runs_used + replications > max_runs:
            return None
        ev = evaluator.evaluate({lever_path: order[i]}, replications=replications, seed=seed)
        result.runs_used += 0 if ev.cached else replications
        ci = ev.kpis.get(metric)
        if ci is None:
            raise KeyError(f"metric {metric!r} was not produced; available: {sorted(ev.kpis)}")
        meets = ci.low >= target if sense == ">=" else ci.high <= target
        if math.isnan(ci.low) or math.isnan(ci.high):
            meets = False
        result.trace.append(
            TraceStep(
                len(result.trace) + 1, order[i], ci.mean, (ci.low, ci.high), ci.n, meets, ev.cached
            )
        )
        seen[i] = meets
        return meets

    # the far end must meet the target, otherwise no candidate will (monotonicity)
    last = check(len(order) - 1)
    if last is None:
        result.status = "budget_exhausted"
        result.notes.append("budget too small for even one evaluation")
        return result
    if not last:
        result.notes.append(
            f"even {order[-1]} does not meet {metric} {sense} {target} with confidence"
        )
        return result
    lo, hi = 0, len(order) - 1  # invariant: order[hi] meets
    while lo < hi:
        mid = (lo + hi) // 2
        ok = check(mid)
        if ok is None:
            result.status = "budget_exhausted"
            result.best = order[hi]
            result.notes.append(
                f"run cap {max_runs} reached; {order[hi]} is the best value confirmed so far"
            )
            return result
        if ok:
            hi = mid
        else:
            lo = mid + 1
    result.best, result.status = order[hi], "found"
    result.notes.append(
        "assumes the metric is monotone in the lever; a candidate meets the target only "
        "when its whole confidence interval does"
    )
    return result

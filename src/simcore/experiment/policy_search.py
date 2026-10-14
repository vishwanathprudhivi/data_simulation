"""Black-box policy search for the ``learned`` dispatch policy (BRD M14).

Cross-entropy method over linear weights: sample weight vectors, evaluate each
with common random numbers through the :class:`Evaluator`, keep the elite and
refit the sampling distribution. Seeded, bounded and fully traced; the result is
a model JSON the ``learned`` policy loads (``policy.params.model``).
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from simcore.core.policy.learned import FEATURES
from simcore.experiment.evaluator import Evaluator

__all__ = ["PolicySearchResult", "cross_entropy_search"]


@dataclass
class PolicySearchResult:
    features: list[str]
    weights: dict[str, float]
    score: float
    metric: str
    trace: list[dict[str, Any]] = field(default_factory=list)

    def model(self) -> dict[str, Any]:
        return {
            "type": "linear",
            "features_version": 1,
            "weights": self.weights,
            "trained_on": {"metric": self.metric, "score": self.score},
        }

    def write(self, path: Path) -> Path:
        path.write_text(json.dumps(self.model(), indent=2, sort_keys=True) + "\n")
        return path


def cross_entropy_search(
    evaluator: Evaluator,
    metric: str,
    *,
    features: Sequence[str] = ("distance", "wait", "priority"),
    maximize: bool = True,
    iterations: int = 4,
    population: int = 8,
    elite: int = 2,
    replications: int = 3,
    seed: int = 0,
    init_scale: float = 1.0,
) -> PolicySearchResult:
    unknown = sorted(set(features) - set(FEATURES))
    if unknown:
        raise ValueError(f"unknown features {unknown}; available: {list(FEATURES)}")
    rng = np.random.default_rng(seed)
    mean = np.zeros(len(features))
    std = np.full(len(features), init_scale)
    best: tuple[float, dict[str, float]] | None = None
    trace: list[dict[str, Any]] = []
    sign = 1.0 if maximize else -1.0
    for it in range(iterations):
        samples = rng.normal(mean, std, size=(population, len(features)))
        scored = []
        for x in samples:
            w = {f: round(float(v), 6) for f, v in zip(features, x)}
            ev = evaluator.evaluate(
                {"policy.name": "learned", "policy.params": {"weights": w}},
                replications=replications,
                seed=seed,
                kpis=[metric],
            )  # same seed for every candidate: common random numbers
            value = ev.mean(metric)
            score = sign * value if math.isfinite(value) else -math.inf
            scored.append((score, w))
            trace.append({"iteration": it, "weights": w, metric: value})
            if best is None or score > best[0]:
                best = (score, w)
        scored.sort(key=lambda s: -s[0])
        top = np.array([[s[1][f] for f in features] for s in scored[:elite]])
        mean, std = top.mean(axis=0), np.maximum(top.std(axis=0), 0.05 * init_scale)
    assert best is not None
    return PolicySearchResult(list(features), best[1], sign * best[0], metric, trace)

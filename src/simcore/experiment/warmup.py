"""Warm-up (initial transient) diagnostics with Welch's method (FR-EXP-06).

Warm-up deletion itself is a fixed time from the scenario/experiment; this module
helps choose it: run a few replications, bucket a signal over sim-time, average
across replications, smooth with Welch's moving average and look for where it
settles.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from simcore.adapters import get_backend
from simcore.core.config import ScenarioBundle
from simcore.core.engine import Engine
from simcore.core.obs.eventlog import EventLog, EventType
from simcore.core.plugins import PluginRegistry

__all__ = [
    "SIGNALS",
    "WarmupDiagnostic",
    "signal_series",
    "suggest_warmup",
    "welch_diagnostic",
    "welch_moving_average",
]

SIGNALS = ("queue_length", "cycle_time", "wait_time", "throughput")


def _bucket_count(end: float, bucket: float) -> int:
    return max(1, math.ceil(end / bucket - 1e-9))


def signal_series(log: EventLog, signal: str, bucket: float, end: float) -> np.ndarray:
    """Per-bucket values of ``signal`` for one replication (NaN where undefined)."""
    if bucket <= 0:
        raise ValueError("bucket must be > 0")
    n = _bucket_count(end, bucket)
    if signal == "queue_length":
        area = np.zeros(n)
        level, prev = 0, 0.0
        changes = list(
            log.of_type(EventType.TASK_RELEASED, EventType.TASK_ASSIGNED, EventType.TASK_REQUEUED)
        )
        for r in [*changes, None]:
            t = end if r is None else min(r.time, end)
            # spread level * (t - prev) over the buckets it covers
            a = prev
            while a < t:
                b_idx = min(int(a // bucket), n - 1)
                b_end = min((b_idx + 1) * bucket, t)
                area[b_idx] += level * (b_end - a)
                a = b_end
            prev = t
            if r is not None:
                level += -1 if r.type == EventType.TASK_ASSIGNED else 1
        widths = np.array([min(bucket, end - i * bucket) for i in range(n)])
        return np.asarray(area / widths, dtype=float)
    if signal in ("cycle_time", "wait_time"):
        release: dict[str, float] = {}
        sums, counts = np.zeros(n), np.zeros(n)
        stop = EventType.TASK_COMPLETED if signal == "cycle_time" else EventType.TASK_ASSIGNED
        for r in log.of_type(EventType.TASK_RELEASED, stop):
            if r.task is None:
                continue
            if r.type == EventType.TASK_RELEASED:
                release[r.task] = r.time
            elif r.task in release and r.time <= end:
                i = min(int(r.time // bucket), n - 1)
                sums[i] += r.time - release[r.task]
                counts[i] += 1
        with np.errstate(invalid="ignore", divide="ignore"):
            return np.where(counts > 0, sums / np.maximum(counts, 1), np.nan)
    if signal == "throughput":
        counts = np.zeros(n)
        for r in log.of_type(EventType.TASK_COMPLETED):
            if r.time <= end:
                counts[min(int(r.time // bucket), n - 1)] += 1
        return counts / bucket
    raise ValueError(f"unknown warm-up signal {signal!r}; choose one of {SIGNALS}")


def welch_moving_average(matrix: np.ndarray, window: int) -> np.ndarray:
    """Welch's procedure on a ``(replications, buckets)`` matrix.

    Average across replications, then a centred moving average of half-width
    ``window`` (shrinking near the start, as in Law & Kelton). Returns
    ``buckets - window`` values.
    """
    ybar = np.nanmean(matrix, axis=0)
    m = len(ybar)
    if window < 1 or window >= m:
        raise ValueError(f"window must be in [1, {m - 1}] for {m} buckets")
    out = np.empty(m - window)
    for i in range(m - window):
        w = min(i, window)
        out[i] = np.nanmean(ybar[i - w : i + w + 1])
    return out


def suggest_warmup(series: Sequence[float] | np.ndarray, bucket: float) -> float | None:
    """MSER truncation point for a cross-replication mean series (White, 1997).

    Chooses the deletion ``d`` (within the first half) minimising
    ``sum((y[d:] - mean(y[d:]))**2) / (n - d)**2``, i.e. the squared standard
    error of the retained mean. Robust to the slow wander of heavily
    autocorrelated signals that defeats "settles inside a band" rules. Use the
    Welch plot to sanity-check it.
    """
    y = np.asarray(series, dtype=float)
    y = y[~np.isnan(y)]
    n = len(y)
    if n < 4:
        return None
    best_d, best = 0, math.inf
    for d in range(n // 2 + 1):
        rest = y[d:]
        score = float(np.sum((rest - rest.mean()) ** 2)) / (n - d) ** 2
        if score < best:
            best_d, best = d, score
    return best_d * bucket


@dataclass(frozen=True)
class WarmupDiagnostic:
    signal: str
    bucket: float
    window: int
    replications: int
    configured: float
    suggested: float | None
    moving_average: list[float | None]

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal": self.signal,
            "bucket": self.bucket,
            "window": self.window,
            "replications": self.replications,
            "configured": self.configured,
            "suggested": self.suggested,
            "moving_average": self.moving_average,
        }


def welch_diagnostic(
    bundle: ScenarioBundle,
    plugins: PluginRegistry,
    *,
    replications: int = 5,
    master_seed: int = 0,
    signal: str = "queue_length",
    bucket: float = 60.0,
    window: int = 5,
    mode: str = "logical",
) -> WarmupDiagnostic:
    """Run ``replications`` and compute Welch's moving average of ``signal``."""
    engine = Engine(plugins, get_backend, mode=mode)
    engine.load_config(bundle).build_world()
    end = bundle.scenario.duration
    rows = []
    try:
        for rep in range(replications):
            result = engine.run_replication(master_seed, rep)
            rows.append(signal_series(result.events, signal, bucket, end))
    finally:
        engine.teardown()
    matrix = np.vstack(rows)
    ma = welch_moving_average(matrix, window)
    return WarmupDiagnostic(
        signal=signal,
        bucket=bucket,
        window=window,
        replications=replications,
        configured=bundle.scenario.warmup,
        suggested=suggest_warmup(np.nanmean(matrix, axis=0), bucket),
        moving_average=[None if math.isnan(v) else float(v) for v in ma],
    )

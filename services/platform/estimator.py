"""Run-time estimator from a benchmark table maintained by completed runs (FR-API-06)."""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any

from services.platform.metadata import MetadataStore

__all__ = ["DEFAULT_RATES", "Estimate", "estimate"]

#: wall seconds per simulated second, used until history exists (NFR-02 targets)
DEFAULT_RATES = {"logical": 1 / 2000, "kinematic": 1 / 20, "physics": 1 / 5}
OVERHEAD_S = 1.0


@dataclass(frozen=True)
class Estimate:
    variants: int
    replications: int
    runs: int
    mode: str
    sim_seconds_per_run: float
    wall_seconds: float
    workers: int
    basis: str  # "history" | "default"

    @property
    def minutes(self) -> float:
        return self.wall_seconds / 60.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "variants": self.variants,
            "replications": self.replications,
            "runs": self.runs,
            "mode": self.mode,
            "sim_seconds_per_run": self.sim_seconds_per_run,
            "wall_seconds": self.wall_seconds,
            "minutes": self.minutes,
            "workers": self.workers,
            "basis": self.basis,
        }


def estimate(
    store: MetadataStore,
    domain: str,
    mode: str,
    variants: int,
    replications: int,
    sim_seconds: float,
    workers: int,
) -> Estimate:
    rows = store.benchmarks(domain, mode)
    rates = [r["wall_seconds"] / r["sim_seconds"] for r in rows if r["sim_seconds"] > 0]
    if rates:
        rate, basis = statistics.median(rates), "history"
    else:
        rate, basis = DEFAULT_RATES.get(mode, 1 / 100), "default"
    runs = variants * replications
    workers = max(1, min(workers, runs))
    wall = OVERHEAD_S + runs * sim_seconds * rate / workers
    return Estimate(variants, replications, runs, mode, sim_seconds, wall, workers, basis)

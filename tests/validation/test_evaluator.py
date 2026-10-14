"""AC-IFC-01 (logical): Evaluator runs 10 replications in parallel, returns KPIs + CIs,
and serves an identical repeated call from the cache (FR-IFC-03/04)."""

from __future__ import annotations

from pathlib import Path

from simcore.core.plugins import PluginRegistry
from simcore.experiment.evaluator import Evaluator
from tests.conftest import MMC


def test_AC_IFC_01_parallel_evaluation_with_cache(tmp_path: Path, plugins: PluginRegistry) -> None:
    ev = Evaluator(MMC, plugins, parallelism=4, cache_dir=tmp_path / "cache")
    first = ev.evaluate({"fleet.server.count": 4}, replications=10, seed=1)
    assert not first.cached and ev.misses == 1
    wait = first.kpis["wait_time_mean"]
    assert wait.n == 10 and wait.low < wait.mean < wait.high
    again = ev.evaluate({"fleet.server.count": 4}, replications=10, seed=1)
    assert again.cached and ev.hits == 1 and ev.misses == 1
    assert again.kpis["wait_time_mean"] == wait
    # a fresh evaluator with the same cache directory is served from disk
    ev2 = Evaluator(MMC, plugins, cache_dir=tmp_path / "cache")
    assert ev2.evaluate({"fleet.server.count": 4}, replications=10, seed=1).cached
    # different params or seed are cache misses
    assert not ev.evaluate({"fleet.server.count": 5}, replications=10, seed=1).cached
    assert not ev.evaluate({"fleet.server.count": 4}, replications=10, seed=2).cached


def test_FR_IFC_03_evaluations_share_crn_seeds(plugins: PluginRegistry) -> None:
    ev = Evaluator(MMC, plugins)
    a = ev.evaluate({"fleet.server.count": 3}, replications=3, seed=4)
    b = ev.evaluate({"fleet.server.count": 3}, replications=3, seed=4)  # cached
    assert a.per_replication == b.per_replication
    assert set(a.to_dict()["kpis"]) == set(a.kpis)

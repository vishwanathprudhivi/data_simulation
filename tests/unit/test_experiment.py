"""Experiment design and runner (FR-EXP-02/03/05/07/09/10, FR-IFC-04, NFR-08)."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest
from domains.toy_queue import ToyQueueModel, ToyQueueParams, ToyQueuePlugin
from pydantic import BaseModel

from simcore.core.config import ConfigError, ScenarioBundle, load_scenario
from simcore.core.plugins import PluginRegistry
from simcore.experiment.design import build_variants, seed_for
from simcore.experiment.runner import ExperimentRunner
from simcore.specs.experiment import ExperimentSpec
from tests.conftest import MMC


def _spec(**kw: Any) -> ExperimentSpec:
    base: dict[str, Any] = {
        "id": "e",
        "base_scenario": str(MMC),
        "replications": 3,
        "run_length": 300,
        "warmup": 30,
    }
    base.update(kw)
    return ExperimentSpec.model_validate(base)


@pytest.fixture(scope="module")
def mmc() -> ScenarioBundle:
    return load_scenario(MMC)


def test_FR_EXP_03_full_factorial_and_named_variants(mmc: ScenarioBundle) -> None:
    spec = _spec(
        variants=[{"id": "fast", "overrides": {"demand.demand.interarrival.mean": 0.5}}],
        factors=[
            {"name": "servers", "path": "fleet.server.count", "levels": [3, 4]},
            {"name": "policy", "path": "policy.name", "levels": ["fifo", "nearest_task"]},
        ],
    )
    variants = build_variants(spec, mmc)
    assert [v.id for v in variants] == [
        "baseline",
        "fast",
        "servers_3__policy_fifo",
        "servers_3__policy_nearest_task",
        "servers_4__policy_fifo",
        "servers_4__policy_nearest_task",
    ]
    v = variants[-1]
    assert v.bundle is not None and v.bundle.scenario.fleet[0].count == 4
    assert v.bundle.scenario.policy.name == "nearest_task"
    assert v.bundle.scenario.duration == 300 and v.bundle.scenario.warmup == 30
    assert dict(v.factors) == {"servers": 4, "policy": "nearest_task"}


def test_FR_EXP_03_bad_override_fails_fast_naming_variant(mmc: ScenarioBundle) -> None:
    spec = _spec(variants=[{"id": "broken", "overrides": {"fleet.server.count": -1}}])
    with pytest.raises(ConfigError, match="variant broken"):
        build_variants(spec, mmc)
    with pytest.raises(ConfigError, match="latin_hypercube"):
        build_variants(
            _spec(
                design="latin_hypercube",
                factors=[{"name": "s", "path": "fleet.server.count", "levels": [1, 2]}],
            ),
            mmc,
        )


def test_FR_EXP_02_crn_seed_mapping() -> None:
    crn = _spec(variants=[{"id": "a"}], master_seed=9)
    assert seed_for(crn, "baseline", 4) == seed_for(crn, "a", 4) == (9, 4)
    ind = _spec(variants=[{"id": "a"}], master_seed=9, crn=False)
    assert seed_for(ind, "baseline", 4) != seed_for(ind, "a", 4)
    assert seed_for(ind, "a", 4) == seed_for(ind, "a", 4)


def test_FR_EXP_05_identical_variants_have_exactly_zero_paired_difference(
    mmc: ScenarioBundle, plugins: PluginRegistry
) -> None:
    spec = _spec(variants=[{"id": "same", "overrides": {"fleet.server.count": 3}}])
    result = ExperimentRunner(spec, mmc, plugins).run()
    c = result.comparison("same", "wait_time_mean")
    assert c.paired and c.diff.mean == 0.0 and not c.ci_excludes_zero


def test_FR_EXP_09_outputs_and_provenance(
    tmp_path: Path, mmc: ScenarioBundle, plugins: PluginRegistry
) -> None:
    spec = _spec(
        variants=[{"id": "four", "overrides": {"fleet.server.count": 4}}],
        kpis=["wait_time_mean", "utilization"],
    )
    result = ExperimentRunner(spec, mmc, plugins, out_root=tmp_path).run()
    exp = tmp_path / "e"
    for name in (
        "experiment.json",
        "results.parquet",
        "runs.parquet",
        "summary.json",
        "report.md",
        "report.html",
    ):
        assert (exp / name).exists(), name
    for vid in ("baseline", "four"):
        for rep in range(3):
            rep_dir = exp / vid / f"rep_{rep:03d}"
            assert {p.name for p in rep_dir.iterdir()} == {
                "events.parquet",
                "kpis.json",
                "meta.json",
            }
    results = pq.read_table(exp / "results.parquet").to_pylist()
    assert {
        "experiment_id",
        "variant",
        "replication",
        "master_seed",
        "metric",
        "value",
        "config_hash",
        "domain",
        "engine_version",
        "git_sha",
        "mode",
    } <= set(results[0])
    runs = pq.read_table(exp / "runs.parquet").to_pylist()
    assert len(runs) == 6 and all(r["status"] == "ok" and r["event_log_sha256"] for r in runs)
    summary = json.loads((exp / "summary.json").read_text())
    assert summary["metrics"] == ["wait_time_mean", "utilization"]
    assert {c["variant"] for c in summary["comparisons"]} == {"four"}
    report = (exp / "report.md").read_text()
    assert "Comparisons vs `baseline`" in report and "paired (CRN)" in report
    assert result.comparison("four", "utilization").ci_excludes_zero


class FlakyParams(ToyQueueParams):
    fail_on_replication: int = -1


class FlakyPlugin(ToyQueuePlugin):
    """toy_queue whose model raises for one replication id (to test NFR-08)."""

    name = "toy_queue"

    def params_model(self) -> type[BaseModel]:
        return FlakyParams

    def create_model(self, bundle: ScenarioBundle, params: BaseModel | None) -> ToyQueueModel:
        assert isinstance(params, FlakyParams)
        model = super().create_model(bundle, params)
        target = params.fail_on_replication
        original = model.setup

        def setup(ctx: Any) -> None:
            if ctx.rng.replication_id == target:
                raise RuntimeError(f"boom on replication {target}")
            original(ctx)

        model.setup = setup  # type: ignore[method-assign]
        return model


def test_NFR_08_failed_replication_recorded_not_fatal(mmc: ScenarioBundle) -> None:
    plugins = PluginRegistry([FlakyPlugin()])
    bundle = mmc.with_overrides({"params.fail_on_replication": 1})
    result = ExperimentRunner(_spec(), bundle, plugins, retries=1).run()
    assert [o.status for o in result.outcomes] == ["ok", "failed", "ok"]
    failed = result.failed[0]
    assert failed.attempts == 2 and "boom on replication 1" in str(failed.error)
    assert result.summary("baseline", "wait_time_mean").n == 2
    assert any("failed" in w for w in result.warnings)
    assert any("fewer than 5" in w for w in result.warnings)  # FR-EXP-10


def test_FR_IFC_04_parallel_matches_sequential(
    mmc: ScenarioBundle, plugins: PluginRegistry
) -> None:
    spec = _spec(variants=[{"id": "four", "overrides": {"fleet.server.count": 4}}], replications=4)
    seq = ExperimentRunner(spec, mmc, plugins, parallelism=1).run()
    par = ExperimentRunner(spec, mmc, plugins, parallelism=3).run()
    assert [(o.variant_id, o.replication, o.kpis) for o in seq.outcomes] == [
        (o.variant_id, o.replication, o.kpis) for o in par.outcomes
    ]
    assert [o.event_log_sha256 for o in seq.outcomes] == [o.event_log_sha256 for o in par.outcomes]


def test_FR_IFC_04_per_replication_timeout(mmc: ScenarioBundle, plugins: PluginRegistry) -> None:
    spec = _spec(replications=2, run_length=2200, warmup=200, timeout_s=1e-6)
    result = ExperimentRunner(spec, mmc, plugins).run()
    assert all(o.status == "failed" for o in result.outcomes)
    assert "SimulationTimeoutError" in str(result.outcomes[0].error)


def test_FR_EXP_07_adaptive_replication(mmc: ScenarioBundle, plugins: PluginRegistry) -> None:
    loose = _spec(
        replications=2,
        adaptive={"metric": "utilization", "target_half_width": 0.05, "max_replications": 20},
    )
    r = ExperimentRunner(loose, mmc, plugins).run()
    n = r.summary("baseline", "utilization").n
    assert 2 < n < 20 and r.summary("baseline", "utilization").relative_half_width <= 0.05
    strict = _spec(
        replications=2,
        adaptive={"metric": "wait_time_mean", "target_half_width": 1e-6, "max_replications": 4},
    )
    r2 = ExperimentRunner(strict, mmc, plugins).run()
    assert r2.summary("baseline", "wait_time_mean").n == 4
    assert any("not reached" in w for w in r2.warnings)


def test_progress_events(mmc: ScenarioBundle, plugins: PluginRegistry) -> None:
    seen: list[tuple[int, int]] = []
    ExperimentRunner(
        _spec(replications=2),
        mmc,
        plugins,
        progress=lambda e: seen.append((e.completed, e.planned)),
    ).run()
    assert seen == [(1, 2), (2, 2)]


def test_unknown_kpi_is_a_warning(mmc: ScenarioBundle, plugins: PluginRegistry) -> None:
    r = ExperimentRunner(_spec(kpis=["wait_time_mean", "bogus"]), mmc, plugins).run()
    assert r.metrics == ["wait_time_mean"]
    assert any("bogus" in w for w in r.warnings)


def test_summary_json_has_no_nan(
    tmp_path: Path, mmc: ScenarioBundle, plugins: PluginRegistry
) -> None:
    r = ExperimentRunner(_spec(replications=1), mmc, plugins, out_root=tmp_path).run()
    text = (tmp_path / "e" / "summary.json").read_text()
    assert "NaN" not in text and "Infinity" not in text
    assert math.isinf(r.summary("baseline", "wait_time_mean").half_width)

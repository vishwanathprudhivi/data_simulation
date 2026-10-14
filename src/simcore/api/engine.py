"""Convenience constructors wiring the core engine to discovered plug-ins and backends."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from simcore.adapters import get_backend
from simcore.core.config import ScenarioBundle
from simcore.core.engine import Engine, RunResult
from simcore.core.plugins import PluginRegistry

if TYPE_CHECKING:
    from simcore.experiment.runner import ExperimentResult

__all__ = ["default_engine", "default_plugins", "run_experiment", "run_scenario"]


def default_plugins(extra: Sequence[str] = ()) -> PluginRegistry:
    """Plug-ins from the ``simcore.domains`` entry points plus ``extra`` ``module:attr`` targets."""
    registry = PluginRegistry()
    registry.discover()
    for target in extra:
        registry.load(target)
    return registry


def default_engine(
    mode: str = "logical", plugins: PluginRegistry | None = None, **kwargs: Any
) -> Engine:
    return Engine(plugins or default_plugins(), get_backend, mode=mode, **kwargs)


def run_scenario(
    scenario: str | Path | ScenarioBundle,
    *,
    seed: int | None = None,
    replication: int = 0,
    mode: str = "logical",
    out: Path | None = None,
    overrides: Mapping[str, Any] | None = None,
    plugins: PluginRegistry | None = None,
    jsonl: bool = False,
) -> RunResult:
    """Run one replication; write outputs under ``out`` when given (AC-ENG-01)."""
    engine = default_engine(mode, plugins)
    try:
        engine.load_config(scenario, overrides).build_world()
        result = engine.run_replication(seed, replication)
    finally:
        engine.teardown()
    if out is not None:
        result.write(out, jsonl=jsonl)
    return result


def run_experiment(
    experiment: str | Path,
    *,
    out: Path | None = Path("runs"),
    parallelism: int | None = None,
    plugins: PluginRegistry | None = None,
    retries: int = 0,
) -> ExperimentResult:
    """Load ``experiment.yaml`` and run it; outputs go to ``out/<experiment id>/``."""
    from simcore.core.config import load_experiment
    from simcore.experiment.runner import ExperimentRunner

    spec, base = load_experiment(experiment)
    runner = ExperimentRunner(
        spec,
        base,
        plugins or default_plugins(),
        out_root=out,
        parallelism=parallelism,
        retries=retries,
    )
    return runner.run()

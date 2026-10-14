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
    registry.discover_tier0()
    for target in extra:
        registry.load(target)
    return registry


def queue_factory(url: str | None) -> Any:
    """``None``/``memory`` -> in-memory queue; ``redis://...`` -> RedisQueue (FR-DSP-06)."""
    from simcore.core.dispatch.queue import InMemoryQueue

    if url is None or url == "memory":
        return InMemoryQueue
    if url.startswith(("redis://", "rediss://", "unix://")):
        from simcore.core.dispatch.redis_queue import RedisQueue

        return lambda: RedisQueue(url)
    raise ValueError(f"unknown queue {url!r}; use 'memory' or a redis:// URL")


def default_engine(
    mode: str = "logical",
    plugins: PluginRegistry | None = None,
    queue: str | None = None,
    **kwargs: Any,
) -> Engine:
    return Engine(
        plugins or default_plugins(),
        get_backend,
        mode=mode,
        queue_factory=queue_factory(queue),
        **kwargs,
    )


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
    step: float | None = None,
    queue: str | None = None,
) -> RunResult:
    """Run one replication; write outputs under ``out`` when given (AC-ENG-01)."""
    engine = default_engine(mode, plugins, queue=queue)
    try:
        engine.load_config(scenario, overrides).build_world()
        result = engine.run_replication(seed, replication, step=step)
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

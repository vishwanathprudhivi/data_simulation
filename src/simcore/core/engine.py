"""Engine lifecycle (FR-ENG-01/02/08, FR-OBS-01/02/06).

``load_config -> build_world -> reset(seed) -> run(until) -> collect -> teardown``
"""

from __future__ import annotations

import hashlib
import json
import logging
import time as _walltime  # wall-clock is used for metadata only, never for sim logic
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from simcore.core.backend import WorldBackend
from simcore.core.bus import EventBus
from simcore.core.clock import SimClock
from simcore.core.config import ScenarioBundle, load_scenario
from simcore.core.dispatch.queue import InMemoryQueue, TaskQueue
from simcore.core.facility.graph import NavGraph
from simcore.core.hashing import canonical_json
from simcore.core.interaction.kpis import InteractionKpis
from simcore.core.model import SimContext, SimModel, SimulationError
from simcore.core.obs.eventlog import EventLog, EventType
from simcore.core.obs.kpi import KpiContext, KpiRegistry, core_kpi_calculators
from simcore.core.plugins import DomainPlugin, PluginRegistry
from simcore.core.policy.reasoning import DisabledReasoningClient, ReasoningClient
from simcore.core.provenance import engine_version, git_sha, host_info, library_versions
from simcore.core.registry import EntityRegistry
from simcore.core.rng import RNGManager
from simcore.core.scheduler import EventScheduler

__all__ = ["Engine", "EngineStateError", "RunResult", "SimulationTimeoutError"]

log = logging.getLogger(__name__)

BackendFactory = Callable[[str], WorldBackend]

#: Fixed step (s) used by kinematic/physics runs when none is given.
DEFAULT_RENDER_STEP = 0.5
QueueFactory = Callable[[], TaskQueue]


class EngineStateError(RuntimeError):
    """Lifecycle method called out of order."""


class SimulationTimeoutError(SimulationError):
    """A replication exceeded its wall-clock budget (FR-IFC-04)."""


_ORDER = ("created", "loaded", "built", "reset", "ran", "collected", "torn_down")


@dataclass
class RunResult:
    """Outputs of one replication.

    BRD 7.5 layout: ``.../<rep_id>/{events.parquet,kpis.json,meta.json}``.
    """

    events: EventLog
    kpis: dict[str, float]
    meta: dict[str, Any]
    files: dict[str, Path] = field(default_factory=dict)

    def write(self, directory: Path, jsonl: bool = False) -> dict[str, Path]:
        directory.mkdir(parents=True, exist_ok=True)
        events = directory / "events.parquet"
        self.events.write_parquet(events)
        files = {"events": events}
        if jsonl:
            files["events_jsonl"] = directory / "events.jsonl"
            self.events.write_jsonl(files["events_jsonl"])
        files["kpis"] = directory / "kpis.json"
        files["kpis"].write_text(json.dumps(self.kpis, indent=2, sort_keys=True) + "\n")
        meta = dict(self.meta)
        meta["event_log_sha256"] = hashlib.sha256(events.read_bytes()).hexdigest()
        files["meta"] = directory / "meta.json"
        files["meta"].write_text(json.dumps(meta, indent=2, sort_keys=True, default=str) + "\n")
        self.meta = meta
        self.files = files
        return files


class Engine:
    """Runs one scenario replication at a time in a chosen fidelity mode."""

    def __init__(
        self,
        plugins: PluginRegistry,
        backend_factory: BackendFactory,
        *,
        mode: str = "logical",
        queue_factory: QueueFactory = InMemoryQueue,
        reasoning: ReasoningClient | None = None,
        max_events: int = 50_000_000,
        timeout_s: float | None = None,
    ) -> None:
        self.plugins = plugins
        self.backend_factory = backend_factory
        self.mode = mode
        self.queue_factory = queue_factory
        self.reasoning = reasoning or DisabledReasoningClient()
        self.max_events = max_events
        # Wall-clock budget per run() call. It can only abort a run, never change it.
        self.timeout_s = timeout_s
        self._deadline: float | None = None
        self._state = "created"
        self.bundle: ScenarioBundle | None = None
        self.plugin: DomainPlugin | None = None
        self.params: BaseModel | None = None
        self.backend: WorldBackend | None = None
        self.ctx: SimContext | None = None
        self.model: SimModel | None = None
        self._wall = 0.0
        self._seed: tuple[int, int] = (0, 0)
        self._events_processed = 0

    # ------------------------------------------------------------------ lifecycle

    def _require(self, *allowed: str) -> None:
        if self._state not in allowed:
            raise EngineStateError(
                f"engine is {self._state!r}; this step needs one of {allowed} "
                f"(lifecycle: {' -> '.join(_ORDER)})"
            )

    def load_config(
        self, source: ScenarioBundle | str | Path, overrides: Mapping[str, Any] | None = None
    ) -> Engine:
        """Load and validate the scenario (FR-ENG-08: fails fast with actionable errors)."""
        self._require("created", "loaded", "built", "collected", "ran", "reset")
        bundle = source if isinstance(source, ScenarioBundle) else load_scenario(source)
        if overrides:
            bundle = bundle.with_overrides(overrides)
        plugin = self.plugins.get(bundle.scenario.domain)
        self.params = plugin.validate_scenario(bundle)
        self.bundle, self.plugin = bundle, plugin
        self._state = "loaded"
        return self

    def build_world(self) -> Engine:
        self._require("loaded")
        assert self.bundle is not None
        if self.backend is not None:
            self.backend.teardown()
        self.backend = self.backend_factory(self.mode)
        assert self.plugin is not None
        self.backend.build(self.bundle.facility, self.plugin.asset_bindings())
        self._state = "built"
        return self

    def reset(self, seed: int | None = None, replication_id: int = 0) -> Engine:
        """Create fresh per-replication state seeded from ``(seed, replication_id)``."""
        self._require("built", "ran", "collected")
        assert self.bundle is not None and self.plugin is not None and self.backend is not None
        master = self.bundle.scenario.seed if seed is None else seed
        self._seed = (master, replication_id)
        if self._state != "built":
            self.backend.teardown()
            self.backend = self.backend_factory(self.mode)
            self.backend.build(self.bundle.facility, self.plugin.asset_bindings())
        self.ctx = SimContext(
            bundle=self.bundle,
            clock=SimClock(0.0),
            scheduler=EventScheduler(),
            bus=EventBus(),
            registry=EntityRegistry(),
            rng=RNGManager(master, replication_id),
            log=EventLog(),
            queue=self.queue_factory(),
            backend=self.backend,
            params=self.params,
            nav=NavGraph(self.bundle.facility) if self.bundle.facility is not None else None,
            reasoning=self.reasoning,
        )
        self.ctx.emit(
            EventType.SIM_START,
            scenario=self.bundle.scenario.id,
            domain=self.plugin.name,
            mode=self.mode,
            master_seed=master,
            replication=replication_id,
        )
        self.model = self.plugin.create_model(self.bundle, self.params)
        self.model.setup(self.ctx)
        self._events_processed = 0
        self._state = "reset"
        return self

    def run(self, until: float | None = None, step: float | None = None) -> Engine:
        """Advance to ``until`` (default: scenario duration).

        Event-driven by default; with ``step`` the clock advances in fixed
        increments and the backend is stepped each increment (FR-ENG-02).
        """
        self._require("reset", "ran")
        assert self.ctx is not None and self.bundle is not None and self.model is not None
        end = self.bundle.scenario.duration if until is None else float(until)
        start_wall = _walltime.perf_counter()
        self._deadline = None if self.timeout_s is None else start_wall + self.timeout_s
        try:
            if step is None:
                self._process_until(end)
            else:
                if step <= 0:
                    raise ValueError("step must be > 0")
                t = self.ctx.now
                while t < end:
                    t = min(t + step, end)
                    self._process_until(t)
                    self.ctx.backend.step(t, step)
            self.ctx.clock.advance_to(max(end, self.ctx.now))
        finally:
            self._wall += _walltime.perf_counter() - start_wall
        if end >= self.bundle.scenario.duration:
            self.model.finish(self.ctx)
            self.ctx.emit(
                EventType.SIM_END,
                pending_tasks=len(self.ctx.queue),
                in_flight=len(self.ctx.queue.in_flight()),
            )
        self._state = "ran"
        return self

    def _process_until(self, end: float) -> None:
        ctx = self.ctx
        assert ctx is not None
        sched, bus, clock = ctx.scheduler, ctx.bus, ctx.clock
        while True:
            next_t = sched.peek_time()
            if next_t is None or next_t > end:
                break
            ev = sched.pop()
            clock.advance_to(ev.time)
            bus.publish(ev)
            self._events_processed += 1
            if (
                self._deadline is not None
                and self._events_processed % 4096 == 0
                and _walltime.perf_counter() > self._deadline
            ):
                raise SimulationTimeoutError(
                    f"replication exceeded its {self.timeout_s}s wall-clock budget at "
                    f"sim-time {clock.now}"
                )
            if self._events_processed > self.max_events:
                raise SimulationError(
                    f"event budget exhausted ({self.max_events} events) at t={clock.now}; "
                    "the model may be livelocked"
                )
        clock.advance_to(max(end, clock.now))

    def kpi_registry(self) -> KpiRegistry:
        assert self.plugin is not None
        policy = getattr(self.model, "policy", None)
        extra = list(policy.kpi_calculators()) if policy is not None else []
        return KpiRegistry(
            [*core_kpi_calculators(), InteractionKpis(), *self.plugin.kpi_calculators(), *extra]
        )

    def collect(self) -> RunResult:
        self._require("ran")
        assert self.ctx is not None and self.bundle is not None and self.plugin is not None
        assert self.backend is not None
        scenario = self.bundle.scenario
        kctx = KpiContext(
            warmup=scenario.warmup,
            end=self.ctx.now,
            params=self.params.model_dump() if self.params is not None else {},
        )
        kpis = self.kpi_registry().compute(self.ctx.log, kctx)
        sim_elapsed = self.ctx.now
        meta: dict[str, Any] = {
            "engine_version": engine_version(),
            "git_sha": git_sha(),
            "domain": {"name": self.plugin.name, "version": self.plugin.version},
            "scenario": {"id": scenario.id, "name": scenario.name, "source": self.bundle.source},
            "facility": (
                {"id": self.bundle.facility.id, "version": self.bundle.facility.version}
                if self.bundle.facility is not None
                else None
            ),
            "config_hashes": dict(self.bundle.hashes),
            "config_hash": self.bundle.config_hash,
            "seeds": self.ctx.rng.describe(),
            "mode": self.mode,
            "backend": self.backend.describe(),
            "versions": library_versions(),
            "host": host_info(),
            "sim_time": {"start": 0.0, "end": sim_elapsed, "warmup": scenario.warmup},
            "wall_time_s": self._wall,
            "real_time_factor": sim_elapsed / self._wall if self._wall > 0 else None,
            "events_processed": self._events_processed,
            "event_log_records": len(self.ctx.log),
            "kpi_digest": hashlib.sha256(canonical_json(kpis).encode()).hexdigest()[:16],
        }
        policy = getattr(self.model, "policy", None)
        report = policy.report(kpis) if policy is not None else None
        if report is not None:
            meta["policy_report"] = report
        self._state = "collected"
        return RunResult(self.ctx.log, kpis, meta)

    def teardown(self) -> None:
        if self.backend is not None:
            self.backend.teardown()
        if self.ctx is not None:
            self.ctx.queue.close()
        self.backend = None
        self.ctx = None
        self.model = None
        self._state = "torn_down"

    # ------------------------------------------------------------------ convenience

    def run_replication(
        self,
        seed: int | None = None,
        replication_id: int = 0,
        until: float | None = None,
        step: float | None = None,
    ) -> RunResult:
        """``reset -> run -> collect`` for an already loaded and built engine.

        Non-logical modes default to fixed-step advance (``step`` seconds, default
        ``DEFAULT_RENDER_STEP``) so the stage is updated as the run progresses.
        """
        if self._state == "loaded":
            self.build_world()
        if step is None and self.mode != "logical":
            step = DEFAULT_RENDER_STEP
        return self.reset(seed, replication_id).run(until, step=step).collect()

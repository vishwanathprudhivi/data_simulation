"""Interactive / demo mode (FR-IFC-05, FR-SCN-06, AC-IFC-02).

An :class:`InteractiveSession` drives one replication incrementally: play, pause,
step, change speed, inject disruptions live and read a KPI HUD and agent positions
at any time. Scripted scenario beats narrate, pause, change speed or inject.

Determinism: the engine never reads the wall clock; wall time only decides *how
far* to advance per tick. Every injection is recorded with its sim-time, and
:meth:`InteractiveSession.recorded_scenario` returns the scenario with those
injections as scheduled disruptions, which reproduces the session exactly in a
batch run.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from simcore.core.config import ScenarioBundle
from simcore.core.engine import Engine
from simcore.core.obs.kpi import KpiContext
from simcore.core.plugins import PluginRegistry
from simcore.core.scheduler import Priority
from simcore.specs.scenario import BeatSpec, DisruptionSpec, InjectSpec

__all__ = ["HUD_METRICS", "Caption", "InteractiveSession"]

#: Fast running KPIs shown when the domain does not choose its own.
HUD_METRICS = (
    "tasks_completed",
    "throughput",
    "utilization",
    "wait_time_mean",
    "blocked_time_per_agent",
    "queue_length_mean",
)


@dataclass(frozen=True)
class Caption:
    t: float
    beat: str
    text: str


@dataclass
class Injection:
    t: float
    spec: InjectSpec
    source: str  # "beat:<name>" | "operator"


@dataclass
class InteractiveSession:
    """One live replication. ``speed`` is sim seconds per wall second."""

    bundle: ScenarioBundle
    plugins: PluginRegistry
    seed: int | None = None
    speed: float = 60.0
    hud_metrics: Sequence[str] = ()
    mode: str = "logical"
    playing: bool = False
    captions: list[Caption] = field(default_factory=list)
    injections: list[Injection] = field(default_factory=list)
    _engine: Engine | None = None
    _beats: list[BeatSpec] = field(default_factory=list)
    _hud_cache: tuple[float, dict[str, float]] | None = None
    _result: Any = None

    def __post_init__(self) -> None:
        from simcore.api.engine import default_engine

        self._engine = default_engine(self.mode, self.plugins)
        self._engine.load_config(self.bundle).build_world().reset(self.seed)
        self._beats = sorted(self.bundle.scenario.beats, key=lambda b: (b.at, b.name))
        if not self.hud_metrics:
            defn = self.engine.plugin.domain_definition() if self.engine.plugin else None
            own = [m.formula for m in defn.metrics] if defn is not None else []
            self.hud_metrics = [*own[:4], *HUD_METRICS][:8]

    # ------------------------------------------------------------------ properties

    @property
    def engine(self) -> Engine:
        assert self._engine is not None
        return self._engine

    @property
    def now(self) -> float:
        assert self.engine.ctx is not None
        return self.engine.ctx.now

    @property
    def duration(self) -> float:
        return self.bundle.scenario.duration

    @property
    def finished(self) -> bool:
        return self.now >= self.duration

    # ------------------------------------------------------------------ control

    def play(self) -> None:
        self.playing = not self.finished

    def pause(self) -> None:
        self.playing = False

    def set_speed(self, speed: float) -> None:
        if not speed > 0:
            raise ValueError("speed must be > 0 (sim seconds per wall second)")
        self.speed = float(speed)

    def tick(self, wall_dt: float) -> float:
        """Advance by ``speed * wall_dt`` sim-seconds when playing; returns the new time."""
        if self.playing and wall_dt > 0:
            self.advance(self.speed * wall_dt)
        return self.now

    def step(self, sim_dt: float) -> float:
        """Advance exactly ``sim_dt`` sim-seconds (paused or not)."""
        return self.advance(sim_dt)

    def advance(self, sim_dt: float) -> float:
        target = min(self.duration, self.now + max(0.0, sim_dt))
        while True:
            beat = self._next_beat(target)
            if beat is None:
                self._run_to(target)
                break
            self._run_to(beat.at)
            self._fire(beat)
            if beat.pause:
                self.playing = False
                break
        if self.finished:
            self.playing = False
        return self.now

    def inject(
        self, spec: InjectSpec | Mapping[str, Any], *, source: str = "operator"
    ) -> Injection:
        """Start a disruption now (FR-IFC-05)."""
        inj = spec if isinstance(spec, InjectSpec) else InjectSpec.model_validate(dict(spec))
        if any(i.spec.id == inj.id for i in self.injections) or any(
            d.id == inj.id for d in self.bundle.scenario.disruptions
        ):
            raise ValueError(f"a disruption with id {inj.id!r} already exists")
        ctx = self.engine.ctx
        assert ctx is not None
        d = DisruptionSpec(
            id=inj.id,
            type=inj.type,
            at=ctx.now,
            duration=inj.duration,
            target=inj.target,
            params=dict(inj.params),
        )
        ctx.scheduler.schedule(ctx.now, "disruption.begin", d, Priority.DISRUPTION)
        record = Injection(ctx.now, inj, source)
        self.injections.append(record)
        self._hud_cache = None
        return record

    # ------------------------------------------------------------------ views

    def hud(self) -> dict[str, float]:
        """Running KPIs over [0, now] (recomputed when the clock moved)."""
        if self._hud_cache is not None and self._hud_cache[0] == self.now:
            return self._hud_cache[1]
        ctx = self.engine.ctx
        assert ctx is not None
        if self.now <= 0:
            return {}
        kctx = KpiContext(warmup=0.0, end=self.now, params={})
        kpis = self.engine.kpi_registry().compute(ctx.log, kctx)
        out = {m: kpis[m] for m in self.hud_metrics if m in kpis and math.isfinite(kpis[m])}
        self._hud_cache = (self.now, out)
        return out

    def positions(self) -> dict[str, list[float]]:
        ctx = self.engine.ctx
        assert ctx is not None
        out: dict[str, list[float]] = {}
        traj = getattr(ctx.backend, "trajectories", None)
        for agent in sorted(traj.spawn) if traj is not None else []:
            p = ctx.backend.position(agent, self.now)
            if p is not None:
                out[agent] = [float(p[0]), float(p[1])]
        return out

    def active_disruptions(self) -> list[str]:
        ctx = self.engine.ctx
        assert ctx is not None
        from simcore.core.obs.eventlog import EventType

        active: dict[str, bool] = {}
        for r in ctx.log.of_type(EventType.DISRUPTION_START, EventType.DISRUPTION_END):
            name = str(r.data.get("disruption"))
            if name == "breakdown":
                continue
            active[name] = r.type == EventType.DISRUPTION_START
        return sorted(k for k, v in active.items() if v)

    def state(self) -> dict[str, Any]:
        return {
            "t": self.now,
            "duration": self.duration,
            "playing": self.playing,
            "speed": self.speed,
            "finished": self.finished,
            "hud": self.hud(),
            "captions": [c.__dict__ for c in self.captions[-5:]],
            "injections": [
                {"t": i.t, "source": i.source, **i.spec.model_dump(mode="json")}
                for i in self.injections
            ],
            "active_disruptions": self.active_disruptions(),
            "next_beat": next((b.name for b in self._beats if b.at > self.now), None),
        }

    def recorded_scenario(self) -> ScenarioBundle:
        """The scenario with this session's injections as scheduled disruptions."""
        extra = [
            {
                "id": i.spec.id,
                "type": i.spec.type,
                "at": i.t,
                "duration": i.spec.duration,
                "target": i.spec.target,
                "params": dict(i.spec.params),
            }
            for i in self.injections
        ]
        current = [d.model_dump(mode="json") for d in self.bundle.scenario.disruptions]
        return self.bundle.with_overrides({"disruptions": current + extra, "beats": []})

    def result(self) -> Any:
        """Final :class:`RunResult` once the session reached the end of the scenario."""
        if not self.finished:
            raise RuntimeError(f"the session is at t={self.now}, not at the end")
        if self._result is None:
            self._result = self.engine.collect()
        return self._result

    def close(self) -> None:
        if self._engine is not None:
            self._engine.teardown()
            self._engine = None

    # ------------------------------------------------------------------ internals

    def _next_beat(self, until: float) -> BeatSpec | None:
        for b in self._beats:
            if self.now <= b.at <= until and not any(c.beat == b.name for c in self.captions):
                return b
        return None

    def _run_to(self, t: float) -> None:
        if t > self.now or self.now == 0:
            self.engine.run(until=t)
            self._hud_cache = None

    def _fire(self, beat: BeatSpec) -> None:
        self.captions.append(Caption(self.now, beat.name, beat.narration))
        if beat.speed is not None:
            self.speed = beat.speed
        for spec in beat.inject:
            self.inject(spec, source=f"beat:{beat.name}")

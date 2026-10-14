"""Simulation context and model base classes.

:class:`SimContext` bundles the per-replication services (clock, scheduler, bus,
registry, RNG, event log, queue, backend). :class:`SimModel` is what a domain
plug-in builds. :class:`DispatchingModel` implements the generic loop shared by
most domains: demand -> task queue -> policy-driven assignment (pull or push) ->
domain-specific task execution -> completion, plus core-generic disruptions.
"""

from __future__ import annotations

import operator
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from simcore.core.backend import WorldBackend
from simcore.core.bus import EventBus
from simcore.core.clock import SimClock
from simcore.core.config import ScenarioBundle
from simcore.core.demand import ARRIVAL, Arrival, DemandSource, make_source
from simcore.core.dispatch.queue import TaskQueue
from simcore.core.dispatch.task import Task
from simcore.core.obs.eventlog import EventLog, EventType
from simcore.core.policy.base import (
    AgentView,
    DispatchAction,
    DispatchObservation,
    Policy,
    PolicyError,
    PolicyFactory,
    TaskView,
    make_policy,
)
from simcore.core.registry import Entity, EntityRegistry
from simcore.core.rng import RNGManager
from simcore.core.sampling import make_sampler
from simcore.core.scheduler import EventScheduler, Priority, ScheduledEvent
from simcore.specs.behaviors import AgentTypeSpec
from simcore.specs.scenario import DisruptionSpec

__all__ = [
    "AgentRuntime",
    "DispatchingModel",
    "SimContext",
    "SimModel",
    "SimulationError",
]

Position = tuple[float, ...]


class SimulationError(RuntimeError):
    """A run could not continue (deadlock, invalid action, event budget exhausted)."""


@dataclass
class SimContext:
    """Per-replication services handed to the model."""

    bundle: ScenarioBundle
    clock: SimClock
    scheduler: EventScheduler
    bus: EventBus
    registry: EntityRegistry
    rng: RNGManager
    log: EventLog
    queue: TaskQueue
    backend: WorldBackend
    params: BaseModel | None = None

    @property
    def now(self) -> float:
        return self.clock.now

    def emit(
        self,
        type_: str,
        *,
        agent: str | None = None,
        task: str | None = None,
        location: str | None = None,
        **data: Any,
    ) -> None:
        """Append a record to the event log at the current sim-time."""
        self.log.emit(self.clock.now, type_, agent=agent, task=task, location=location, **data)

    def schedule_in(
        self, delay: float, kind: str, payload: object = None, priority: int = Priority.DEFAULT
    ) -> ScheduledEvent:
        if delay < 0:
            raise SimulationError(f"negative delay {delay} for event {kind!r}")
        return self.scheduler.schedule(self.clock.now + delay, kind, payload, priority)


class SimModel(ABC):
    """A domain model for one replication."""

    @abstractmethod
    def setup(self, ctx: SimContext) -> None:
        """Register event kinds and handlers; schedule initial events."""

    def finish(self, ctx: SimContext) -> None:  # noqa: B027 - optional hook
        """Called once after the run loop ends (before KPIs are computed)."""


# ----------------------------------------------------------------------------- agents


@dataclass
class AgentRuntime:
    entity: Entity
    spec: AgentTypeSpec | None
    speed_multiplier: float = 1.0
    state: str = "idle"
    task: Task | None = None
    unavailable: int = 0  # number of active unavailability disruptions
    speed_factor: float = 1.0  # from speed_change disruptions

    @property
    def id(self) -> str:
        return self.entity.id

    @property
    def type(self) -> str:
        return self.entity.tags[0] if self.entity.tags else self.entity.type

    @property
    def free(self) -> bool:
        return self.task is None and self.unavailable == 0

    def speed(self, profile: str = "default") -> float:
        """Effective speed (m/s) for a named profile including multipliers."""
        speeds = self.spec.speeds if self.spec is not None else {}
        if profile not in speeds:
            if not speeds:
                raise SimulationError(f"agent type {self.type!r} declares no speeds")
            profile = next(iter(speeds))
        return speeds[profile] * self.speed_multiplier * self.speed_factor


@dataclass(frozen=True)
class TaskDone:
    agent_id: str
    task_id: str


@dataclass(frozen=True)
class ActivityDone:
    agent_id: str
    name: str
    data: Mapping[str, Any] = field(default_factory=dict)


_OPS: dict[str, Callable[[float, float], bool]] = {
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
    "==": operator.eq,
}


class DispatchingModel(SimModel):
    """Generic demand -> queue -> dispatch -> execute loop (FR-DSP-04, FR-SCN-01..04).

    Subclasses implement :meth:`start_task` and call :meth:`complete_task` when
    the work is done. Agents are created from ``scenario.fleet`` with ids
    ``<agent_type>-<nnn>``; per-agent speed multipliers are drawn from stream
    ``agent_speed`` in creation order.
    """

    #: Agent type -> optional extra entity tags.
    agent_tags: Mapping[str, tuple[str, ...]] = {}

    def __init__(
        self,
        bundle: ScenarioBundle,
        params: BaseModel | None = None,
        policies: Mapping[str, PolicyFactory] | None = None,
    ) -> None:
        self.bundle = bundle
        self.params = params
        scenario = bundle.scenario
        self.policy: Policy = make_policy(scenario.policy.name, scenario.policy.params, policies)
        self.assignment = scenario.policy.assignment
        self.agents: dict[str, AgentRuntime] = {}
        self.sources: list[DemandSource] = [make_source(d) for d in scenario.demand]
        self._dispatch_pending: ScheduledEvent | None = None
        self._released = 0
        self._completed = 0
        self._triggers: list[DisruptionSpec] = [d for d in scenario.disruptions if d.trigger]
        self._handlers: dict[str, Callable[[SimContext, AgentRuntime, ActivityDone], None]] = {}

    # ------------------------------------------------------------------ setup

    def setup(self, ctx: SimContext) -> None:
        self.ctx = ctx
        bus = ctx.bus
        bus.register(ARRIVAL, Arrival)
        bus.register("dispatch", None)
        bus.register("task.done", TaskDone)
        bus.register("activity.done", ActivityDone)
        bus.register("disruption.begin", DisruptionSpec)
        bus.register("disruption.finish", DisruptionSpec)
        bus.subscribe(ARRIVAL, self._on_arrival)
        bus.subscribe("dispatch", self._on_dispatch)
        bus.subscribe("task.done", self._on_task_done)
        bus.subscribe("activity.done", self._on_activity_done)
        bus.subscribe("disruption.begin", self._on_disruption_begin)
        bus.subscribe("disruption.finish", self._on_disruption_finish)
        self.create_agents(ctx)
        for source in self.sources:
            source.start(ctx)
        for d in self.bundle.scenario.disruptions:
            if d.at is not None:
                ctx.scheduler.schedule(d.at, "disruption.begin", d, Priority.DISRUPTION)

    def create_agents(self, ctx: SimContext) -> None:
        behaviors = self.bundle.behaviors
        speed_rng = ctx.rng.stream("agent_speed")
        for entry in self.bundle.scenario.fleet:
            spec = behaviors.agent_type(entry.agent_type) if behaviors else None
            mult_sampler = (
                make_sampler(spec.speed_multiplier) if spec and spec.speed_multiplier else None
            )
            for i in range(entry.count):
                agent_id = f"{entry.agent_type}-{i:03d}"
                attrs = {**(spec.attributes if spec else {}), **entry.attributes}
                position = self.initial_position(ctx, entry.agent_type, i)
                entity = ctx.registry.add(
                    Entity(
                        agent_id,
                        "agent",
                        (entry.agent_type, *self.agent_tags.get(entry.agent_type, ())),
                        attrs,
                        position,
                    )
                )
                mult = mult_sampler(speed_rng) if mult_sampler else 1.0
                agent = AgentRuntime(entity, spec, speed_multiplier=mult)
                self.agents[agent_id] = agent
                ctx.backend.spawn_agent(agent_id, entry.agent_type, position)
                ctx.emit(
                    EventType.AGENT_CREATED,
                    agent=agent_id,
                    agent_type=entry.agent_type,
                    speed_multiplier=mult,
                )
                ctx.emit(EventType.AGENT_STATE, agent=agent_id, state="idle")

    def initial_position(self, ctx: SimContext, agent_type: str, index: int) -> Position | None:
        """Override to place agents; default: no position."""
        return None

    # ------------------------------------------------------------------ hooks for subclasses

    @abstractmethod
    def start_task(self, ctx: SimContext, agent: AgentRuntime, task: Task) -> None:
        """Begin executing ``task`` with ``agent``; eventually call :meth:`complete_task`."""

    def task_position(self, ctx: SimContext, task: Task) -> Position | None:
        """Position of a task's origin for distance-aware policies; default none."""
        return None

    def on_disruption(self, ctx: SimContext, spec: DisruptionSpec, active: bool) -> None:
        """Domain handling for disruptions (called for every type after core handling)."""

    # ------------------------------------------------------------------ agent helpers

    def set_state(self, agent: AgentRuntime, state: str, **data: Any) -> None:
        if state != agent.state:
            agent.state = state
            self.ctx.emit(
                EventType.AGENT_STATE,
                agent=agent.id,
                task=agent.task.id if agent.task else None,
                state=state,
                **data,
            )

    def activity(
        self,
        agent: AgentRuntime,
        state: str,
        duration: float,
        name: str,
        on_done: Callable[[SimContext, AgentRuntime, ActivityDone], None],
        location: str | None = None,
        **data: Any,
    ) -> None:
        """Put ``agent`` in ``state`` for ``duration`` then call ``on_done``."""
        self.set_state(agent, state, **({"location": location} if location else {}))
        self._handlers[f"{agent.id}:{name}"] = on_done
        self.ctx.schedule_in(
            duration, "activity.done", ActivityDone(agent.id, name, data), Priority.COMPLETION
        )

    def _on_activity_done(self, ev: ScheduledEvent) -> None:
        assert isinstance(ev.payload, ActivityDone)
        handler = self._handlers.pop(f"{ev.payload.agent_id}:{ev.payload.name}")
        handler(self.ctx, self.agents[ev.payload.agent_id], ev.payload)

    def complete_task(self, ctx: SimContext, agent: AgentRuntime) -> None:
        task = agent.task
        if task is None:
            raise SimulationError(f"agent {agent.id} completed a task but holds none")
        ctx.queue.ack(task.id)
        self._completed += 1
        ctx.emit(
            EventType.TASK_COMPLETED,
            agent=agent.id,
            task=task.id,
            location=task.destination,
            cycle_time=ctx.now - task.release_time,
        )
        agent.task = None
        self.set_state(agent, "unavailable" if agent.unavailable else "idle")
        self._check_triggers(ctx)
        self.request_dispatch()

    # ------------------------------------------------------------------ demand

    def release(self, ctx: SimContext, task: Task) -> None:
        ctx.queue.put(task)
        self._released += 1
        ctx.emit(
            EventType.TASK_RELEASED,
            task=task.id,
            location=task.origin,
            task_type=task.type,
            priority=task.priority,
            due_time=task.due_time,
            quantity=task.quantity,
            destination=task.destination,
        )
        self._check_triggers(ctx)
        self.request_dispatch()

    def _on_arrival(self, ev: ScheduledEvent) -> None:
        assert isinstance(ev.payload, Arrival)
        self.release(self.ctx, ev.payload.task)
        for source in self.sources:
            if source.name == ev.payload.source:
                source.on_arrival(self.ctx, ev.payload)

    # ------------------------------------------------------------------ dispatch

    def request_dispatch(self) -> None:
        """Coalesce dispatch requests into one decision point at the current sim-time.

        Pull mode presents free agents choosing among tasks; push mode presents
        tasks choosing among free agents (FR-DSP-04).
        """
        if self._dispatch_pending is None:
            self._dispatch_pending = self.ctx.scheduler.schedule(
                self.ctx.now, "dispatch", None, Priority.DISPATCH
            )

    def _on_dispatch(self, ev: ScheduledEvent) -> None:
        self._dispatch_pending = None
        self.dispatch(self.ctx, self.assignment)

    def observation(self, ctx: SimContext, trigger: Any) -> DispatchObservation:
        agents = tuple(
            AgentView(
                a.id,
                a.type,
                ctx.backend.position(a.id, ctx.now) or a.entity.position,
                a.entity.attributes,
            )
            for a in self.agents.values()
            if a.free
        )
        tasks = tuple(
            TaskView(
                t.id,
                t.type,
                t.priority,
                t.release_time,
                t.due_time,
                t.origin,
                t.destination,
                self.task_position(ctx, t),
                t.attributes,
            )
            for t in ctx.queue.pending()
        )
        return DispatchObservation(ctx.now, trigger, agents, tasks)

    def dispatch(self, ctx: SimContext, trigger: Any) -> None:
        if len(ctx.queue) == 0:
            return
        obs = self.observation(ctx, trigger)
        if not obs.agents:
            return
        action = self.policy.decide(obs)
        self._apply(ctx, obs, action)

    def _apply(self, ctx: SimContext, obs: DispatchObservation, action: DispatchAction) -> None:
        free_agents = {a.id for a in obs.agents}
        pending = {t.id for t in obs.tasks}
        for a in action.assignments:
            if a.agent_id not in free_agents or a.task_id not in pending:
                raise PolicyError(
                    f"policy {self.policy.name!r} returned invalid assignment "
                    f"{a.agent_id}->{a.task_id} (agent not free or task not pending)"
                )
            free_agents.discard(a.agent_id)
            pending.discard(a.task_id)
            task = ctx.queue.claim(a.agent_id, a.task_id)
            assert task is not None
            agent = self.agents[a.agent_id]
            agent.task = task
            ctx.emit(
                EventType.POLICY_DECISION,
                agent=agent.id,
                task=task.id,
                policy=self.policy.name,
                trigger=obs.trigger,
            )
            ctx.emit(
                EventType.TASK_ASSIGNED,
                agent=agent.id,
                task=task.id,
                wait=ctx.now - task.release_time,
            )
            self.start_task(ctx, agent, task)

    def _on_task_done(self, ev: ScheduledEvent) -> None:
        assert isinstance(ev.payload, TaskDone)
        self.complete_task(self.ctx, self.agents[ev.payload.agent_id])

    # ------------------------------------------------------------------ disruptions

    def _check_triggers(self, ctx: SimContext) -> None:
        if not self._triggers:
            return
        signals = {
            "queue_length": float(len(ctx.queue)),
            "completed_tasks": float(self._completed),
            "released_tasks": float(self._released),
        }
        fired = []
        for d in self._triggers:
            assert d.trigger is not None
            if _OPS[d.trigger.op](signals[d.trigger.signal], d.trigger.threshold):
                fired.append(d)
        for d in fired:
            self._triggers.remove(d)
            ctx.scheduler.schedule(ctx.now, "disruption.begin", d, Priority.DISRUPTION)

    def _targets(self, ctx: SimContext, spec: DisruptionSpec) -> list[AgentRuntime]:
        if spec.target is None:
            agents = list(self.agents.values())
        elif spec.target.startswith("type:"):
            agents = [a for a in self.agents.values() if a.type == spec.target[5:]]
        elif spec.target.startswith("tag:"):
            agents = [a for a in self.agents.values() if a.entity.has_tag(spec.target[4:])]
        else:
            agents = [self.agents[spec.target]] if spec.target in self.agents else []
        count = spec.params.get("count")
        return agents[: int(count)] if count is not None else agents

    def _on_disruption_begin(self, ev: ScheduledEvent) -> None:
        spec = ev.payload
        assert isinstance(spec, DisruptionSpec)
        ctx = self.ctx
        ctx.emit(
            EventType.DISRUPTION_START,
            disruption=spec.id,
            type=spec.type,
            target=spec.target,
            params=spec.params,
        )
        self._apply_disruption(ctx, spec, active=True)
        if spec.duration is not None:
            ctx.schedule_in(spec.duration, "disruption.finish", spec, Priority.DISRUPTION)

    def _on_disruption_finish(self, ev: ScheduledEvent) -> None:
        spec = ev.payload
        assert isinstance(spec, DisruptionSpec)
        self.ctx.emit(EventType.DISRUPTION_END, disruption=spec.id, type=spec.type)
        self._apply_disruption(self.ctx, spec, active=False)

    def _apply_disruption(self, ctx: SimContext, spec: DisruptionSpec, active: bool) -> None:
        if spec.type == "resource_unavailable":
            for agent in self._targets(ctx, spec):
                agent.unavailable += 1 if active else -1
                if agent.task is None:
                    self.set_state(agent, "unavailable" if agent.unavailable else "idle")
            if not active:
                self.request_dispatch()
        elif spec.type == "demand_surge":
            factor = float(spec.params.get("factor", 1.0))
            if factor <= 0:
                raise SimulationError(f"disruption {spec.id}: demand_surge factor must be > 0")
            for source in self.sources:
                if spec.target in (None, source.name):
                    source.rate_factor = (
                        source.rate_factor * factor if active else (source.rate_factor / factor)
                    )
        elif spec.type == "speed_change":
            factor = float(spec.params.get("factor", 1.0))
            for agent in self._targets(ctx, spec):
                agent.speed_factor = (
                    agent.speed_factor * factor if active else (agent.speed_factor / factor)
                )
        self.on_disruption(ctx, spec, active)

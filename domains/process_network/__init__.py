"""Generic config-only (Tier 0) domain: a network of stations (FR-DOM-01, BRD 6.16).

A Tier-0 domain is just a ``domain.yaml`` with ``tier: 0`` and a ``process`` block
(stations + routings). ``plugin_for(definition)`` turns it into a plug-in; simcore
finds Tier-0 domains through the ``simcore.tier0`` entry point and the domain
search path, so onboarding one needs no Python.

Semantics:
* Work items (tasks) arrive from the scenario's demand; ``task.type`` selects the
  routing (ordered stations).
* A station has ``capacity`` parallel servers. A server starts when ``batch_size``
  items are waiting, the station is up, and (if ``resource_type``) an operator of
  that type is free. Duration = changeover (if the item type changed) + one draw
  of ``service_time`` per batch (stream ``service_time``).
* After service, items move to the next station's buffer. If that buffer is full
  (``buffer_capacity``), the server stays blocked holding them (blocking after
  service) until space frees. External arrivals always enter the first buffer.
* Breakdowns (``failures``): a station fails after an ``mtbf`` draw and is repaired
  after an ``mttr`` draw (stream ``failures``); work in progress finishes, no new
  work starts while down.
* Operators are released when service ends (they never wait on blocked servers).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from simcore.core.config import ConfigError, ConfigIssue, ScenarioBundle, validate_spec
from simcore.core.demand import ARRIVAL, Arrival, DemandSource, make_source
from simcore.core.dispatch.task import Task
from simcore.core.model import SimContext, SimModel, SimulationError
from simcore.core.obs.eventlog import EventLog, EventType
from simcore.core.obs.kpi import KpiCalculator, KpiContext
from simcore.core.plugins import DomainPlugin
from simcore.core.registry import Entity
from simcore.core.sampling import Sampler, make_sampler
from simcore.core.scheduler import Priority, ScheduledEvent
from simcore.specs.distributions import DistributionSpec
from simcore.specs.domain import DomainDefinition, StationDef
from simcore.specs.scenario import DisruptionSpec

__all__ = ["ProcessNetworkParams", "ProcessNetworkPlugin", "load_definition", "plugin_for"]

QUEUE = "process_network.queue"
BREAKDOWN = "process_network.breakdown"
REPAIR = "process_network.repair"
BLOCKED = "process_network.blocked"
UNBLOCKED = "process_network.unblocked"


class StationOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capacity: int | None = Field(default=None, ge=1)
    service_time: DistributionSpec | None = None
    batch_size: int | None = Field(default=None, ge=1)
    buffer_capacity: int | None = Field(default=None, ge=0)
    changeover: float | None = Field(default=None, ge=0)
    service_time_factor: float = Field(default=1.0, gt=0, description="Speed-up < 1 < slow-down.")


class ProcessNetworkParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stations: dict[str, StationOverride] = Field(default_factory=dict)


@dataclass
class _Server:
    index: int
    busy: bool = False
    blocked: list[Task] = field(default_factory=list)
    blocked_since: float | None = None


@dataclass
class _Station:
    spec: StationDef
    capacity: int
    service: Sampler | None
    factor: float
    batch_size: int
    buffer: int | None
    changeover: float
    servers: list[_Server]
    queue: deque[Task] = field(default_factory=deque)
    down: int = 0
    last_type: str | None = None

    @property
    def id(self) -> str:
        return self.spec.id

    def has_space(self) -> bool:
        return self.buffer is None or len(self.queue) < self.buffer


@dataclass(frozen=True)
class _Done:
    station: str
    server: int
    items: tuple[Task, ...]
    operator: str | None


class ProcessNetworkModel(SimModel):
    def __init__(
        self, definition: DomainDefinition, bundle: ScenarioBundle, params: ProcessNetworkParams
    ) -> None:
        if definition.process is None:
            raise SimulationError(f"domain {definition.id!r} has no 'process' block")
        self.definition, self.bundle, self.params = definition, bundle, params
        self.routes = {r.work_item_type: list(r.steps) for r in definition.process.routings}
        self.stations: dict[str, _Station] = {}
        for st in definition.process.stations:
            ov = params.stations.get(st.id, StationOverride())
            dist = ov.service_time or st.service_time
            capacity = ov.capacity or st.capacity
            self.stations[st.id] = _Station(
                spec=st,
                capacity=capacity,
                service=make_sampler(dist) if dist is not None else None,
                factor=ov.service_time_factor,
                batch_size=ov.batch_size or st.batch_size,
                buffer=ov.buffer_capacity if ov.buffer_capacity is not None else st.buffer_capacity,
                changeover=ov.changeover if ov.changeover is not None else (st.changeover or 0.0),
                servers=[_Server(i) for i in range(capacity)],
            )
        self.position: dict[str, int] = {}  # task id -> index in its route
        self.pools: dict[str, list[str]] = {}
        self.unavailable: dict[str, int] = {}
        self.sources: list[DemandSource] = [make_source(d) for d in bundle.scenario.demand]

    # ------------------------------------------------------------------ setup

    def setup(self, ctx: SimContext) -> None:
        self.ctx = ctx
        bus = ctx.bus
        bus.register(ARRIVAL, Arrival)
        bus.register("process_network.done", _Done)
        bus.register(BREAKDOWN, str)
        bus.register(REPAIR, str)
        bus.register("disruption.begin", DisruptionSpec)
        bus.register("disruption.finish", DisruptionSpec)
        bus.subscribe(ARRIVAL, self._on_arrival)
        bus.subscribe("process_network.done", self._on_done)
        bus.subscribe(BREAKDOWN, self._on_breakdown)
        bus.subscribe(REPAIR, self._on_repair)
        bus.subscribe("disruption.begin", lambda ev: self._disruption(ev, True))
        bus.subscribe("disruption.finish", lambda ev: self._disruption(ev, False))
        for entry in self.bundle.scenario.fleet:
            pool = self.pools.setdefault(entry.agent_type, [])
            for i in range(entry.count):
                agent_id = f"{entry.agent_type}-{i:03d}"
                ctx.registry.add(
                    Entity(agent_id, "agent", (entry.agent_type,), dict(entry.attributes))
                )
                ctx.backend.spawn_agent(agent_id, entry.agent_type, None)
                ctx.emit(EventType.AGENT_CREATED, agent=agent_id, agent_type=entry.agent_type)
                ctx.emit(EventType.AGENT_STATE, agent=agent_id, state="idle")
                pool.append(agent_id)
        for st in self.stations.values():
            ctx.emit(QUEUE, location=st.id, length=0)
            if st.spec.failures is not None:
                self._schedule_failure(st)
        for source in self.sources:
            source.start(ctx)
        for d in self.bundle.scenario.disruptions:
            if d.at is not None:
                ctx.scheduler.schedule(d.at, "disruption.begin", d, Priority.DISRUPTION)

    # ------------------------------------------------------------------ flow

    def _on_arrival(self, ev: ScheduledEvent) -> None:
        assert isinstance(ev.payload, Arrival)
        task = ev.payload.task
        if task.type not in self.routes:
            raise SimulationError(
                f"work item type {task.type!r} has no routing; known: {sorted(self.routes)}"
            )
        ctx = self.ctx
        ctx.emit(
            EventType.TASK_RELEASED,
            task=task.id,
            task_type=task.type,
            priority=task.priority,
            due_time=task.due_time,
            quantity=task.quantity,
            destination=task.destination,
        )
        self.position[task.id] = 0
        self._enqueue(self.stations[self.routes[task.type][0]], task)
        for source in self.sources:
            if source.name == ev.payload.source:
                source.on_arrival(ctx, ev.payload)

    def _enqueue(self, st: _Station, task: Task) -> None:
        st.queue.append(task)
        self.ctx.emit(QUEUE, location=st.id, length=len(st.queue))
        self._try_start(st)

    def _operator(self, st: _Station) -> str | None:
        rt = st.spec.resource_type
        if rt is None:
            return None
        for agent_id in self.pools.get(rt, []):
            if not self.unavailable.get(agent_id):
                return agent_id
        return ""

    def _try_start(self, st: _Station) -> None:
        ctx = self.ctx
        while not st.down and len(st.queue) >= st.batch_size:
            server = next(
                (s for s in st.servers[: st.capacity] if not s.busy and not s.blocked), None
            )
            if server is None:
                return
            op = self._operator(st)
            if op == "":
                return  # no operator free
            if st.service is None:
                raise SimulationError(f"station {st.id!r} has no service_time")
            items = tuple(st.queue.popleft() for _ in range(st.batch_size))
            ctx.emit(QUEUE, location=st.id, length=len(st.queue))
            if op is not None:
                self.pools[st.spec.resource_type or ""].remove(op)
                ctx.emit(EventType.AGENT_STATE, agent=op, state="service", location=st.id)
            change = (
                st.changeover
                if (st.last_type is not None and items[0].type != st.last_type)
                else 0.0
            )
            st.last_type = items[-1].type
            duration = change + st.service(ctx.rng.stream("service_time")) * st.factor
            server.busy = True
            for item in items:
                if self.position[item.id] == 0:
                    ctx.emit(
                        EventType.TASK_ASSIGNED,
                        task=item.id,
                        agent=op,
                        location=st.id,
                        wait=ctx.now - item.release_time,
                    )
                    ctx.emit(EventType.TASK_STARTED, task=item.id, agent=op, location=st.id)
            ctx.emit(
                EventType.SERVICE_START,
                agent=op,
                location=st.id,
                location_kind="station",
                group=st.id,
                server=server.index,
                items=[i.id for i in items],
                changeover=change,
                duration=duration,
                colocated=0,
            )
            ctx.schedule_in(
                duration,
                "process_network.done",
                _Done(st.id, server.index, items, op),
                Priority.COMPLETION,
            )
            self._release_upstream(st)

    def _on_done(self, ev: ScheduledEvent) -> None:
        assert isinstance(ev.payload, _Done)
        d = ev.payload
        ctx = self.ctx
        st = self.stations[d.station]
        server = st.servers[d.server]
        ctx.emit(
            EventType.SERVICE_END,
            agent=d.operator,
            location=st.id,
            location_kind="station",
            group=st.id,
            server=d.server,
        )
        if d.operator is not None:
            self.pools.setdefault(st.spec.resource_type or "", []).append(d.operator)
            self.pools[st.spec.resource_type or ""].sort()
            ctx.emit(
                EventType.AGENT_STATE,
                agent=d.operator,
                state="unavailable" if self.unavailable.get(d.operator) else "idle",
            )
        server.busy = False
        server.blocked = list(d.items)
        self._push_downstream(st, server)
        self._start_all()

    def _push_downstream(self, st: _Station, server: _Server) -> None:
        ctx = self.ctx
        while server.blocked:
            item = server.blocked[0]
            route = self.routes[item.type]
            idx = self.position[item.id]
            if idx + 1 >= len(route):
                server.blocked.pop(0)
                del self.position[item.id]
                ctx.emit(
                    EventType.TASK_COMPLETED,
                    task=item.id,
                    location=st.id,
                    cycle_time=ctx.now - item.release_time,
                )
                continue
            nxt = self.stations[route[idx + 1]]
            if not nxt.has_space():
                if server.blocked_since is None:
                    server.blocked_since = ctx.now
                    ctx.emit(BLOCKED, location=st.id, server=server.index, target=nxt.id)
                return
            server.blocked.pop(0)
            self.position[item.id] = idx + 1
            self._enqueue(nxt, item)
        if server.blocked_since is not None:
            ctx.emit(UNBLOCKED, location=st.id, server=server.index)
            ctx.emit(
                EventType.BLOCKING,
                location=st.id,
                kind="buffer_full",
                duration=ctx.now - server.blocked_since,
                since=server.blocked_since,
                blockers=[],
            )
            server.blocked_since = None

    def _release_upstream(self, st: _Station) -> None:
        """Space opened in ``st``'s buffer: let blocked servers upstream push into it."""
        for other in self.stations.values():
            for server in other.servers:
                if server.blocked and st.has_space():
                    item = server.blocked[0]
                    route = self.routes[item.type]
                    idx = self.position[item.id]
                    if idx + 1 < len(route) and route[idx + 1] == st.id:
                        self._push_downstream(other, server)
                        if not server.blocked:
                            self._try_start(other)

    def _start_all(self) -> None:
        for st in self.stations.values():
            self._try_start(st)

    # ------------------------------------------------------------------ breakdowns

    def _schedule_failure(self, st: _Station) -> None:
        assert st.spec.failures is not None
        sampler = make_sampler(st.spec.failures.mtbf)
        self.ctx.schedule_in(
            sampler(self.ctx.rng.stream("failures")), BREAKDOWN, st.id, Priority.DISRUPTION
        )

    def _on_breakdown(self, ev: ScheduledEvent) -> None:
        st = self.stations[str(ev.payload)]
        assert st.spec.failures is not None
        st.down += 1
        self.ctx.emit(
            EventType.DISRUPTION_START,
            location=st.id,
            disruption="breakdown",
            type="breakdown",
            target=st.id,
        )
        repair = make_sampler(st.spec.failures.mttr)(self.ctx.rng.stream("failures"))
        self.ctx.schedule_in(repair, REPAIR, st.id, Priority.DISRUPTION)

    def _on_repair(self, ev: ScheduledEvent) -> None:
        st = self.stations[str(ev.payload)]
        st.down -= 1
        self.ctx.emit(
            EventType.DISRUPTION_END, location=st.id, disruption="breakdown", type="breakdown"
        )
        self._schedule_failure(st)
        self._try_start(st)

    # ------------------------------------------------------------------ scenario disruptions

    def _disruption(self, ev: ScheduledEvent, active: bool) -> None:
        spec = ev.payload
        assert isinstance(spec, DisruptionSpec)
        ctx = self.ctx
        ctx.emit(
            EventType.DISRUPTION_START if active else EventType.DISRUPTION_END,
            disruption=spec.id,
            type=spec.type,
            target=spec.target,
        )
        if active and spec.duration is not None:
            ctx.schedule_in(spec.duration, "disruption.finish", spec, Priority.DISRUPTION)
        target = spec.target or ""
        if spec.type == "location_blocked" and target in self.stations:
            self.stations[target].down += 1 if active else -1
        elif spec.type == "capacity_change" and target in self.stations:
            st = self.stations[target]
            cap = (
                int(spec.params.get("capacity", st.spec.capacity))
                if active
                else (
                    self.params.stations.get(target, StationOverride()).capacity or st.spec.capacity
                )
            )
            while len(st.servers) < cap:
                st.servers.append(_Server(len(st.servers)))
            st.capacity = cap
        elif spec.type == "resource_unavailable":
            kind = target[5:] if target.startswith("type:") else None
            agents = sorted(
                e.id
                for e in ctx.registry.by_type("agent")
                if kind is None or e.has_tag(kind) or e.id == target
            )
            count = int(spec.params.get("count", len(agents)))
            idle = {a for pool in self.pools.values() for a in pool}
            for agent_id in agents[:count]:
                self.unavailable[agent_id] = self.unavailable.get(agent_id, 0) + (
                    1 if active else -1
                )
                if agent_id in idle:  # busy operators change state when they finish
                    state = "unavailable" if self.unavailable[agent_id] else "idle"
                    ctx.emit(EventType.AGENT_STATE, agent=agent_id, state=state)
        self._start_all()


class ProcessKpis:
    """Per-station utilization, blocking, downtime and queue; line WIP (time-weighted)."""

    name = "process_network.stations"

    def __init__(self, definition: DomainDefinition) -> None:
        assert definition.process is not None
        self.capacity = {s.id: s.capacity for s in definition.process.stations}

    def compute(self, log: EventLog, ctx: KpiContext) -> dict[str, float]:
        out: dict[str, float] = {}
        w = ctx.window

        def clip(a: float, b: float) -> float:
            return max(0.0, min(b, ctx.end) - max(a, ctx.warmup))

        busy: dict[str, float] = {}
        start: dict[tuple[str, int], float] = {}
        cap: dict[str, int] = dict(self.capacity)
        for r in log.of_type(EventType.SERVICE_START, EventType.SERVICE_END):
            key = (str(r.location), int(r.data.get("server", 0)))
            if r.type == EventType.SERVICE_START:
                start[key] = r.time
                cap[key[0]] = max(cap.get(key[0], 1), key[1] + 1)
            elif key in start:
                busy[key[0]] = busy.get(key[0], 0.0) + clip(start.pop(key), r.time)
        for key, t0 in start.items():
            busy[key[0]] = busy.get(key[0], 0.0) + clip(t0, ctx.end)
        blocked: dict[str, float] = {}
        bstart: dict[tuple[str, int], float] = {}
        for r in log.of_type(BLOCKED, UNBLOCKED):
            key = (str(r.location), int(r.data.get("server", 0)))
            if r.type == BLOCKED:
                bstart[key] = r.time
            elif key in bstart:
                blocked[key[0]] = blocked.get(key[0], 0.0) + clip(bstart.pop(key), r.time)
        for key, t0 in bstart.items():
            blocked[key[0]] = blocked.get(key[0], 0.0) + clip(t0, ctx.end)
        down: dict[str, float] = {}
        dstart: dict[str, float] = {}
        for r in log.of_type(EventType.DISRUPTION_START, EventType.DISRUPTION_END):
            if r.data.get("type") != "breakdown" or r.location is None:
                continue
            if r.type == EventType.DISRUPTION_START:
                dstart[r.location] = r.time
            elif r.location in dstart:
                down[r.location] = down.get(r.location, 0.0) + clip(dstart.pop(r.location), r.time)
        for st, t0 in dstart.items():
            down[st] = down.get(st, 0.0) + clip(t0, ctx.end)
        qarea: dict[str, float] = {}
        qlast: dict[str, tuple[float, int]] = {}
        for r in log.of_type(QUEUE):
            st = str(r.location)
            if st in qlast:
                t0, n = qlast[st]
                qarea[st] = qarea.get(st, 0.0) + n * clip(t0, r.time)
            qlast[st] = (r.time, int(r.data.get("length", 0)))
        for st, (t0, n) in qlast.items():
            qarea[st] = qarea.get(st, 0.0) + n * clip(t0, ctx.end)
        for st in sorted(self.capacity):
            c = cap.get(st, 1)
            out[f"station_{st}_utilization"] = busy.get(st, 0.0) / (c * w) if w > 0 else 0.0
            out[f"station_{st}_blocked_share"] = blocked.get(st, 0.0) / (c * w) if w > 0 else 0.0
            out[f"station_{st}_down_share"] = down.get(st, 0.0) / w if w > 0 else 0.0
            out[f"station_{st}_queue_mean"] = qarea.get(st, 0.0) / w if w > 0 else 0.0
        level, prev, area = 0, 0.0, 0.0
        for r in log.of_type(EventType.TASK_RELEASED, EventType.TASK_COMPLETED):
            area += level * clip(prev, r.time)
            prev = r.time
            level += 1 if r.type == EventType.TASK_RELEASED else -1
        area += level * clip(prev, ctx.end)
        out["wip_mean"] = area / w if w > 0 else 0.0
        if busy:
            bottleneck = max(sorted(busy), key=lambda s: busy[s] / cap.get(s, 1))
            out["bottleneck_utilization"] = busy[bottleneck] / (cap.get(bottleneck, 1) * w)
        return out


class ProcessNetworkPlugin(DomainPlugin):
    """Plug-in for one Tier-0 domain definition (instances are made by ``plugin_for``)."""

    name = "process_network"
    version = "1.0.0"
    description = "Generic station network (Tier 0); instantiate with plugin_for(definition)."
    definition: DomainDefinition | None = None
    source_path: str | None = None

    def domain_definition(self) -> DomainDefinition | None:
        return self.definition

    def __reduce__(self) -> tuple[Any, tuple[Any, ...]]:
        # Instances use per-definition classes made at runtime; rebuild them from the
        # definition when pickled into worker processes (FR-IFC-04 spawn pool).
        return (plugin_for, (self.definition, self.source_path))

    def entity_types(self) -> Mapping[str, str]:
        d = self.definition
        if d is None:
            return {}
        return {
            e.id: e.description for e in (*d.resource_types, *d.work_item_types, *d.entity_types)
        }

    def kpi_calculators(self) -> Sequence[KpiCalculator]:
        return (ProcessKpis(self.definition),) if self.definition is not None else ()

    def params_model(self) -> type[BaseModel]:
        return ProcessNetworkParams

    def validate_scenario(self, bundle: ScenarioBundle) -> BaseModel | None:
        params = super().validate_scenario(bundle)
        d = self.definition
        if d is None or d.process is None:
            raise ConfigError(
                [
                    ConfigIssue(
                        bundle.source,
                        "domain",
                        "process_network needs a Tier-0 domain definition",
                        "use a domain whose domain.yaml has a process block",
                    )
                ]
            )
        issues = []
        routes = {r.work_item_type for r in d.process.routings}
        stations = {s.id: s for s in d.process.stations}
        for n, src in enumerate(bundle.scenario.demand):
            types = {getattr(src, "task_type", None)} if hasattr(src, "task_type") else set()
            types |= {t.type for t in getattr(src, "tasks", [])}
            for t in sorted(x for x in types if x):
                if t not in routes:
                    issues.append(
                        ConfigIssue(
                            bundle.source,
                            f"demand.{n}",
                            f"work item type {t!r} has no routing in domain {d.id!r}",
                            f"use one of {sorted(routes)}",
                        )
                    )
        assert isinstance(params, ProcessNetworkParams)
        for sid in params.stations:
            if sid not in stations:
                issues.append(
                    ConfigIssue(
                        bundle.source,
                        f"params.stations.{sid}",
                        f"unknown station {sid!r}",
                        f"use {sorted(stations)}",
                    )
                )
        fleet = {f.agent_type for f in bundle.scenario.fleet if f.count > 0}
        for st in stations.values():
            if st.resource_type and st.resource_type not in fleet:
                issues.append(
                    ConfigIssue(
                        bundle.source,
                        "fleet",
                        f"station {st.id!r} needs operators of type "
                        f"{st.resource_type!r} but the fleet has none",
                        f"add a fleet entry for {st.resource_type!r}",
                    )
                )
            dist = params.stations.get(st.id, StationOverride()).service_time or st.service_time
            if dist is None:
                issues.append(
                    ConfigIssue(
                        bundle.source,
                        f"params.stations.{st.id}",
                        f"station {st.id!r} has no service_time",
                        "set it in domain.yaml or the scenario",
                    )
                )
        if issues:
            raise ConfigError(issues)
        return params

    def create_model(self, bundle: ScenarioBundle, params: BaseModel | None) -> SimModel:
        assert self.definition is not None and isinstance(params, ProcessNetworkParams)
        return ProcessNetworkModel(self.definition, bundle, params)


_CLASSES: dict[tuple[str, str], type[ProcessNetworkPlugin]] = {}


def load_definition(path: Path | str) -> DomainDefinition:
    path = Path(path)
    if not path.exists():
        raise ConfigError(
            [ConfigIssue(str(path), "", "domain.yaml not found", "check the domain path")]
        )
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return validate_spec(DomainDefinition, data, str(path))


def plugin_for(
    definition: DomainDefinition | Path | str, source: str | None = None
) -> ProcessNetworkPlugin:
    """Plug-in for a Tier-0 definition (one stable class per (id, version))."""
    if not isinstance(definition, DomainDefinition):
        source = str(definition)
        definition = load_definition(definition)
    if definition.tier != 0:
        raise ConfigError(
            [
                ConfigIssue(
                    source or definition.id,
                    "tier",
                    f"process_network runs Tier-0 domains; "
                    f"{definition.id!r} is tier {definition.tier}",
                    "set tier: 0 or provide a Python plug-in",
                )
            ]
        )
    key = (definition.id, definition.version)
    cls = _CLASSES.get(key)
    if cls is None:
        cls = type(
            f"ProcessNetwork_{definition.id}",
            (ProcessNetworkPlugin,),
            {
                "name": definition.id,
                "version": definition.version,
                "description": definition.description or definition.name,
            },
        )
        _CLASSES[key] = cls
    plugin = cls()
    plugin.definition = definition
    plugin.source_path = source
    return plugin

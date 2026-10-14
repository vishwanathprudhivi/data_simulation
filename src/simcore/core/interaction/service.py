"""Work at locations: capacity, queueing for space and co-location penalties (FR-INT-04/06).

* A location serves at most ``capacity`` agents at once; others queue FIFO and
  their wait is logged as ``space`` blocking (time-in-queue-for-space).
* Locations sharing a co-location group (location attribute named by
  ``ColocationSpec.group_attribute``) slow each other down: service time is
  multiplied by ``1 + penalty_per_agent x others working in the group`` at start.
* A location with ``attributes.occupies_edge`` makes the working agent a
  stationary occupant of that edge, so passing traffic must fit around it.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from simcore.core.facility.graph import NavGraph
from simcore.core.obs.eventlog import EventType
from simcore.core.scheduler import Priority, ScheduledEvent
from simcore.specs.scenario import ColocationSpec

if TYPE_CHECKING:
    from simcore.core.interaction.movement import MovementSystem
    from simcore.core.model import AgentRuntime, SimContext

__all__ = ["ServiceSystem"]

DONE = "service.done"


@dataclass
class _Request:
    agent: AgentRuntime
    location: str
    base: float
    on_done: Callable[[], None]
    state: str
    since: float
    blockers: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Done:
    agent_id: str
    location: str


class ServiceSystem:
    def __init__(
        self,
        ctx: SimContext,
        nav: NavGraph,
        spec: ColocationSpec,
        movement: MovementSystem | None = None,
    ) -> None:
        self.ctx, self.nav, self.spec, self.movement = ctx, nav, spec, movement
        self.active: dict[str, list[str]] = {}
        self.group_active: dict[str, int] = {}
        self.queue: dict[str, deque[_Request]] = {}
        self.capacity_override: dict[str, int] = {}
        self._pending: dict[str, _Request] = {}
        ctx.bus.register(DONE, _Done)
        ctx.bus.subscribe(DONE, self._on_done)

    def group_of(self, location: str) -> str:
        loc = self.nav.locations.get(location)
        if loc is None:
            return location
        return str(loc.attributes.get(self.spec.group_attribute, location))

    def kind_of(self, location: str) -> str | None:
        loc = self.nav.locations.get(location)
        return loc.kind if loc is not None else None

    def capacity(self, location: str) -> int:
        if location in self.capacity_override:
            return self.capacity_override[location]
        loc = self.nav.locations.get(location)
        return loc.capacity if loc is not None else 1

    def set_capacity(self, location: str, capacity: int | None) -> None:
        if capacity is None:
            self.capacity_override.pop(location, None)
        else:
            self.capacity_override[location] = max(0, capacity)
        self._start_queued(location)

    def serve(
        self,
        agent: AgentRuntime,
        location: str,
        base_duration: float,
        on_done: Callable[[], None],
        state: str = "service",
    ) -> None:
        """Work at ``location`` for ``base_duration`` (before penalties), then ``on_done``."""
        req = _Request(agent, location, base_duration, on_done, state, self.ctx.now)
        if len(self.active.get(location, ())) >= self.capacity(location):
            req.blockers = tuple(sorted(self.active.get(location, ())))
            self.queue.setdefault(location, deque()).append(req)
            agent.state = "waiting"
            self.ctx.emit(
                EventType.AGENT_STATE,
                agent=agent.id,
                task=agent.task.id if agent.task else None,
                state="waiting",
                location=location,
            )
            return
        self._start(req)

    def _start(self, req: _Request) -> None:
        ctx, agent, loc = self.ctx, req.agent, req.location
        if ctx.now > req.since:
            ctx.emit(
                EventType.BLOCKING,
                agent=agent.id,
                location=loc,
                kind="space",
                blockers=list(req.blockers),
                duration=ctx.now - req.since,
                since=req.since,
            )
        group = self.group_of(loc)
        others = self.group_active.get(group, 0)
        duration = req.base * (1.0 + self.spec.penalty_per_agent * others)
        self.active.setdefault(loc, []).append(agent.id)
        self.group_active[group] = others + 1
        edge = self._edge(loc)
        if edge is not None and self.movement is not None:
            self.movement.occupy(agent, edge)
        if agent.state != req.state:
            agent.state = req.state
            ctx.emit(
                EventType.AGENT_STATE,
                agent=agent.id,
                task=agent.task.id if agent.task else None,
                state=req.state,
                location=loc,
            )
        ctx.emit(
            EventType.SERVICE_START,
            agent=agent.id,
            task=agent.task.id if agent.task else None,
            location=loc,
            group=group,
            location_kind=self.kind_of(loc),
            colocated=others,
            base_duration=req.base,
            duration=duration,
        )
        if others:
            ctx.emit(
                EventType.COLOCATION,
                agent=agent.id,
                location=loc,
                group=group,
                others=others,
                penalty=duration - req.base,
            )
        self._pending[agent.id] = req
        ctx.schedule_in(duration, DONE, _Done(agent.id, loc), Priority.COMPLETION)

    def _edge(self, location: str) -> str | None:
        loc = self.nav.locations.get(location)
        if loc is None:
            return None
        edge = loc.attributes.get("occupies_edge")
        return str(edge) if edge is not None else None

    def _on_done(self, ev: ScheduledEvent) -> None:
        assert isinstance(ev.payload, _Done)
        req = self._pending.pop(ev.payload.agent_id)
        loc = ev.payload.location
        self.active[loc].remove(req.agent.id)
        group = self.group_of(loc)
        self.group_active[group] -= 1
        edge = self._edge(loc)
        if edge is not None and self.movement is not None:
            self.movement.vacate(req.agent, edge)
        self.ctx.emit(
            EventType.SERVICE_END,
            agent=req.agent.id,
            task=req.agent.task.id if req.agent.task else None,
            location=loc,
            group=group,
            location_kind=self.kind_of(loc),
        )
        self._start_queued(loc)
        req.on_done()

    def _start_queued(self, location: str) -> None:
        q = self.queue.get(location)
        while q and len(self.active.get(location, ())) < self.capacity(location):
            self._start(q.popleft())

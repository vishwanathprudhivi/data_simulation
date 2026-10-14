"""Movement over the navigation graph with occupancy, blocking and deadlocks
(FR-INT-01/02/03/07, ADR-007).

Agents traverse routes edge by edge. On entering an edge the interaction model
fixes the agent's speed from the edge geometry and its current occupants
(link-entry approximation). Rules:

* **Opposing / stationary agents.** An agent may enter only if it can pass every
  opposing or stationary occupant (``InteractionModel.can_pass``); otherwise it
  waits at the node (``opposing`` blocking).
* **Single lane, same direction.** No overtaking: exit order equals entry order
  and an agent leaves no earlier than ``leader exit + headway`` (``following``).
* **Holding.** An agent that reached the end of an edge but cannot enter the next
  one keeps occupying its edge. This is what makes deadlocks possible.
* **Deadlocks.** Each new wait triggers a cycle search in the wait-for graph.
  Resolution per ``InteractionSpec.deadlock``: ``priority_yield`` (lowest
  priority, then highest id, backs out of its edge and re-plans), ``reroute``
  (backs out and avoids the contested edge if an alternative exists),
  ``timeout`` (yield after a delay if still deadlocked) or ``none`` (agents in the
  cycle are failed and a failure record is logged, never a hang).
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from simcore.core.facility.graph import Edge, NavGraph, NoRouteError, Route, Step
from simcore.core.interaction.base import EdgeOccupant, InteractionModel
from simcore.core.obs.eventlog import EventType
from simcore.core.scheduler import Priority, ScheduledEvent
from simcore.specs.scenario import InteractionSpec

if TYPE_CHECKING:
    from simcore.core.model import AgentRuntime, SimContext

__all__ = ["MovementHooks", "MovementSystem"]

ARRIVE = "move.arrive"
RETRY = "move.retry"
DEADLOCK_CHECK = "move.deadlock_check"


class MovementHooks(Protocol):
    def speed_modifier(self, agent: AgentRuntime, edge: Edge, time: float) -> float: ...

    def on_agent_failed(self, agent: AgentRuntime, reason: str) -> None: ...


@dataclass
class Occupant:
    agent_id: str
    direction: int  # +1 / -1 moving, 0 stationary
    width: float
    length: float
    entry: float
    order: int
    planned_exit: float | None
    source: str | None = None
    target: str | None = None
    free_speed: float = 0.0
    follow_delay: float = 0.0
    leader: str | None = None


@dataclass
class Wait:
    kind: str  # opposing | following | no_route
    edge: str | None
    since: float
    blockers: set[str] = field(default_factory=set)  # everyone who blocked during the wait
    current: set[str] = field(default_factory=set)  # who blocks right now (wait-for graph)
    soft: set[str] = field(default_factory=set)  # fairness deferrals (subset of current)
    kinds: set[str] = field(default_factory=set)
    deadlock_pending: bool = False


@dataclass
class Trip:
    agent: AgentRuntime
    route: Route
    destination: str
    profile: str
    on_arrival: Callable[[], None]
    index: int = 0
    phase: str = "entering"  # entering | moving | at_end
    holding: str | None = None
    wait: Wait | None = None
    token: int = 0
    avoid: frozenset[str] = frozenset()
    backing_out: bool = False
    chain_plan: dict[str, int] = field(default_factory=dict)  # corridor edges -> direction
    fairness_waived: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class Arrive:
    agent_id: str
    token: int


@dataclass(frozen=True)
class DeadlockCheck:
    agents: tuple[str, ...]


class MovementSystem:
    def __init__(
        self,
        ctx: SimContext,
        nav: NavGraph,
        spec: InteractionSpec,
        model: InteractionModel,
        hooks: MovementHooks,
    ) -> None:
        self.ctx = ctx
        self.nav = nav
        self.spec = spec
        self.model = model
        self.hooks = hooks
        self.node_of: dict[str, str | None] = {}
        self.occupants: dict[str, dict[str, Occupant]] = {e: {} for e in nav.edges}
        self.waiters: dict[str, list[str]] = {e: [] for e in nav.edges}
        self.no_route: list[str] = []
        self.trips: dict[str, Trip] = {}
        self._order = itertools.count()
        bus = ctx.bus
        bus.register(ARRIVE, Arrive)
        bus.register(RETRY, str)
        bus.register(DEADLOCK_CHECK, DeadlockCheck)
        bus.subscribe(ARRIVE, self._on_arrive)
        bus.subscribe(RETRY, self._on_retry)
        bus.subscribe(DEADLOCK_CHECK, self._on_deadlock_check)

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _width(agent: AgentRuntime) -> float:
        return agent.spec.footprint.width if agent.spec is not None else 1.0

    @staticmethod
    def _length(agent: AgentRuntime) -> float:
        return agent.spec.footprint.length if agent.spec is not None else 1.0

    def place(self, agent: AgentRuntime, place: str) -> None:
        self.node_of[agent.id] = self.nav.node_of(place)

    def is_moving(self, agent_id: str) -> bool:
        return agent_id in self.trips

    def _set_state(self, agent: AgentRuntime, state: str, **data: Any) -> None:
        if state != agent.state:
            agent.state = state
            self.ctx.emit(
                EventType.AGENT_STATE,
                agent=agent.id,
                task=agent.task.id if agent.task else None,
                state=state,
                **data,
            )

    def _free_speed(self, trip: Trip, edge: Edge) -> float:
        speed = trip.agent.speed(trip.profile)
        speed *= self.hooks.speed_modifier(trip.agent, edge, self.ctx.now)
        speed *= self.nav.speed_factor.get(edge.id, 1.0)
        if edge.max_speed is not None:
            speed = min(speed, edge.max_speed)
        if speed <= 0:
            raise ValueError(f"agent {trip.agent.id}: non-positive speed on edge {edge.id}")
        return speed

    # ------------------------------------------------------------------ public API

    def travel(
        self,
        agent: AgentRuntime,
        destination: str,
        profile: str,
        on_arrival: Callable[[], None],
    ) -> None:
        """Move ``agent`` to ``destination`` (location or node); call ``on_arrival`` there."""
        if agent.id in self.trips:
            raise RuntimeError(f"agent {agent.id} is already travelling")
        node = self.node_of.get(agent.id)
        if node is None:
            raise RuntimeError(f"agent {agent.id} has no known position; call place() first")
        trip = Trip(agent, Route(node, node), destination, profile, on_arrival)
        self.trips[agent.id] = trip
        self._plan(trip, node)

    def occupy(self, agent: AgentRuntime, edge_id: str) -> None:
        """Stationary occupancy, e.g. while working at a location beside an edge."""
        self.occupants[edge_id][agent.id] = Occupant(
            agent.id,
            0,
            self._width(agent),
            self._length(agent),
            self.ctx.now,
            next(self._order),
            None,
        )

    def vacate(self, agent: AgentRuntime, edge_id: str) -> None:
        self.occupants[edge_id].pop(agent.id, None)
        self._wake(edge_id)

    def graph_changed(self) -> None:
        """Re-try everyone waiting (call after edges are blocked/unblocked or degraded)."""
        for agent_id in sorted({*self.no_route, *itertools.chain(*self.waiters.values())}):
            self.ctx.scheduler.schedule(self.ctx.now, RETRY, agent_id, Priority.DISPATCH - 1)

    # ------------------------------------------------------------------ planning

    def _plan(self, trip: Trip, node: str) -> None:
        try:
            try:
                route = self.nav.route(node, trip.destination, avoid=trip.avoid)
            except NoRouteError:
                if not trip.avoid:
                    raise
                route = self.nav.route(node, trip.destination)
        except NoRouteError:
            self._wait(trip, "no_route", None, set())
            return
        trip.route, trip.index, trip.phase, trip.avoid = route, 0, "entering", frozenset()
        if not route.steps:
            self._finish(trip)
            return
        self._attempt(trip)

    def _finish(self, trip: Trip) -> None:
        if trip.wait is not None:
            self._end_wait(trip)
        if trip.holding is not None:
            self._release(trip.holding, trip)
        self.node_of[trip.agent.id] = trip.route.destination
        del self.trips[trip.agent.id]
        trip.on_arrival()

    # ------------------------------------------------------------------ entering edges

    def _conflicts(self, trip: Trip, step: Step) -> set[str]:
        me = trip.agent
        d = step.direction
        blockers = set()
        for occ in self.occupants[step.edge.id].values():
            if occ.agent_id == me.id:
                continue
            if (occ.direction == 0 or occ.direction != d) and not self.model.can_pass(
                step.edge.width, self._width(me), occ.width
            ):
                blockers.add(occ.agent_id)
        return blockers

    def _entry_blockers(self, trip: Trip, step: Step) -> tuple[set[str], set[str]]:
        """Conflicts on the next edge; when entering a corridor, on the whole stretch ahead.

        Corridor reservation: an agent does not enter a run of edges joined by
        pass-through nodes while an agent it cannot pass is coming the other way
        anywhere along the part it will travel (ADR-007).
        """
        blockers = self._conflicts(trip, step)
        chain = self.nav.chain_id[step.edge.id]
        prev = trip.route.steps[trip.index - 1] if trip.index > 0 else None
        if (
            prev is not None
            and self.nav.chain_id[prev.edge.id] == chain
            and (trip.holding == prev.edge.id)
        ):
            return blockers, set()  # already inside this corridor
        plan = {step.edge.id: step.direction}
        for ahead in trip.route.steps[trip.index + 1 :]:
            if self.nav.chain_id[ahead.edge.id] != chain:
                break
            plan[ahead.edge.id] = ahead.direction
            blockers |= {
                a
                for a in self._conflicts(trip, ahead)
                if self.occupants[ahead.edge.id][a].direction != 0
            }
        trip.chain_plan = plan
        # Fairness: do not cut in front of an agent that has waited longer to come
        # through this corridor the other way (prevents starvation at corridor ends).
        # This is a preference: a deadlock through it is broken by waiving it.
        soft: set[str] = set()
        mine = trip.wait.since if trip.wait is not None else self.ctx.now
        for other in self.trips.values():
            w = other.wait
            if other is trip or w is None or w.kind != "opposing" or w.since >= mine:
                continue
            if other.agent.id in trip.fairness_waived:
                continue
            if any(other.chain_plan.get(e) == -d for e, d in plan.items()):
                soft.add(other.agent.id)
        return blockers, soft

    def _attempt(self, trip: Trip) -> None:
        step = trip.route.steps[trip.index]
        if step.edge.id in self.nav.blocked:
            self._replan_from(trip, step.start)
            return
        hard, soft = self._entry_blockers(trip, step)
        if hard or soft:
            self._wait(trip, "opposing", step.edge.id, hard, soft)
            return
        self._enter(trip, step)

    def _replan_from(self, trip: Trip, node: str) -> None:
        try:
            route = self.nav.route(node, trip.destination)
        except NoRouteError:
            self._wait(trip, "no_route", None, set())
            return
        trip.route, trip.index, trip.phase = route, 0, "entering"
        if not route.steps:
            self._finish(trip)
            return
        self._attempt(trip)

    def _enter(self, trip: Trip, step: Step) -> None:
        ctx, edge, agent = self.ctx, step.edge, trip.agent
        now = ctx.now
        if trip.wait is not None:
            self._end_wait(trip)
        if trip.holding is not None:
            self._release(trip.holding, trip)
        trip.chain_plan = {}
        trip.fairness_waived = set()
        d = step.direction
        others = [
            EdgeOccupant(o.agent_id, o.width, o.direction) for o in self.occupants[edge.id].values()
        ]
        free = self._free_speed(trip, edge)
        width = self._width(agent)
        speed = self.model.effective_speed(
            free, edge.width, edge.directed, EdgeOccupant(agent.id, width, d), others
        )
        planned = now + edge.length / speed
        occ = Occupant(
            agent.id,
            d,
            width,
            self._length(agent),
            now,
            next(self._order),
            planned,
            source=step.start,
            target=step.end,
            free_speed=free,
        )
        if self.model.lanes(edge.width, width) == 1:
            leaders = [
                o
                for o in self.occupants[edge.id].values()
                if o.direction == d and o.planned_exit is not None
            ]
            if leaders:
                lead = max(leaders, key=lambda o: o.order)
                # default headway: time to clear one's own length (+ clearance) at free speed
                headway = (
                    self.spec.headway
                    if self.spec.headway is not None
                    else (occ.length + self.spec.clearance) / free
                )
                earliest = lead.planned_exit + headway  # type: ignore[operator]
                if earliest > planned:
                    occ.follow_delay = earliest - planned
                    occ.leader = lead.agent_id
                    planned = earliest
                occ.planned_exit = planned
        self.occupants[edge.id][agent.id] = occ
        trip.holding, trip.phase = edge.id, "moving"
        trip.token += 1
        self.node_of[agent.id] = None
        self._set_state(agent, "travel", edge=edge.id)
        ctx.backend.move(
            agent.id, [self.nav.positions[step.start], self.nav.positions[step.end]], [now, planned]
        )
        ctx.scheduler.schedule(planned, ARRIVE, Arrive(agent.id, trip.token), Priority.COMPLETION)

    def _release(self, edge_id: str, trip: Trip) -> None:
        occ = self.occupants[edge_id].pop(trip.agent.id)
        now = self.ctx.now
        edge = self.nav.edges[edge_id]
        self.ctx.emit(
            EventType.AGENT_MOVE,
            agent=trip.agent.id,
            task=trip.agent.task.id if trip.agent.task else None,
            location=edge_id,
            source=occ.source,
            target=occ.target,
            entry=occ.entry,
            arrive=occ.planned_exit,
            length=edge.length,
            free_speed=occ.free_speed,
            speed=edge.length / (now - occ.entry) if now > occ.entry else None,
        )
        if occ.follow_delay > 0 and occ.leader is not None:
            self.ctx.emit(
                EventType.BLOCKING,
                agent=trip.agent.id,
                location=edge_id,
                kind="following",
                blockers=[occ.leader],
                duration=occ.follow_delay,
            )
        trip.holding = None
        self._wake(edge_id)

    def _wake(self, edge_id: str) -> None:
        for agent_id in self.waiters[edge_id]:
            self.ctx.scheduler.schedule(self.ctx.now, RETRY, agent_id, Priority.DISPATCH - 1)

    # ------------------------------------------------------------------ arrivals

    def _on_arrive(self, ev: ScheduledEvent) -> None:
        assert isinstance(ev.payload, Arrive)
        trip = self.trips.get(ev.payload.agent_id)
        if trip is None or trip.token != ev.payload.token:
            return  # stale (agent backed out or failed)
        trip.phase = "at_end"
        self._leave_edge_end(trip)

    def _leaders_on_edge(self, trip: Trip) -> set[str]:
        """Same-direction agents ahead in a single-lane edge that have not left yet."""
        if trip.holding is None:
            return set()
        edge = self.nav.edges[trip.holding]
        mine = self.occupants[edge.id][trip.agent.id]
        if self.model.lanes(edge.width, mine.width) > 1:
            return set()
        return {
            o.agent_id
            for o in self.occupants[edge.id].values()
            if o.direction == mine.direction and o.order < mine.order
        }

    def _leave_edge_end(self, trip: Trip) -> None:
        leaders = self._leaders_on_edge(trip)
        if leaders:
            self._wait(trip, "following", trip.holding, leaders)
            return
        if trip.wait is not None:
            self._end_wait(trip)
        if trip.index + 1 >= len(trip.route.steps):
            if trip.backing_out:
                node = trip.route.destination
                if trip.holding is not None:
                    self._release(trip.holding, trip)
                self.node_of[trip.agent.id] = node
                trip.backing_out = False
                self._plan(trip, node)
                return
            self._finish(trip)
            return
        trip.index += 1
        trip.phase = "entering"
        self._attempt(trip)

    # ------------------------------------------------------------------ waiting

    def _wait(
        self,
        trip: Trip,
        kind: str,
        edge: str | None,
        blockers: set[str],
        soft: set[str] | None = None,
    ) -> None:
        now = self.ctx.now
        if trip.wait is None:
            trip.wait = Wait(kind, edge, now)
        w = trip.wait
        self._unregister(trip)
        w.kind, w.edge = kind, edge
        soft = soft or set()
        w.blockers |= blockers | soft
        w.kinds.add(kind)
        w.current = blockers | soft
        w.soft = set(soft)
        if edge is None:
            self.no_route.append(trip.agent.id)
        else:
            # wake on any release along the reserved corridor stretch, not just the next edge
            for e in sorted({edge, *trip.chain_plan}) if kind == "opposing" else [edge]:
                self.waiters[e].append(trip.agent.id)
        self._set_state(trip.agent, "blocked", reason=kind, edge=edge)
        cycle = self._find_cycle(trip.agent.id)
        if cycle and not self._waive_fairness(cycle):
            self._on_deadlock(cycle)

    def _waive_fairness(self, cycle: list[str]) -> bool:
        """Break a wait cycle that runs through a fairness deferral by waiving it."""
        for a, b in zip(cycle, [*cycle[1:], cycle[0]]):
            w = self.trips[a].wait
            if w is not None and b in w.soft:
                self.trips[a].fairness_waived.add(b)
                w.soft.discard(b)
                w.current.discard(b)
                self.ctx.scheduler.schedule(self.ctx.now, RETRY, a, Priority.DISPATCH - 1)
                return True
        return False

    def _unregister(self, trip: Trip) -> None:
        aid = trip.agent.id
        if aid in self.no_route:
            self.no_route.remove(aid)
        for lst in self.waiters.values():
            if aid in lst:
                lst.remove(aid)

    def _end_wait(self, trip: Trip) -> None:
        w = trip.wait
        assert w is not None
        self._unregister(trip)
        duration = self.ctx.now - w.since
        if duration > 0:
            self.ctx.emit(
                EventType.BLOCKING,
                agent=trip.agent.id,
                location=w.edge,
                kind=sorted(w.kinds)[0] if len(w.kinds) == 1 else "+".join(sorted(w.kinds)),
                blockers=sorted(w.blockers),
                duration=duration,
                since=w.since,
            )
        trip.wait = None
        # agents deferring to this one (fairness) may now go
        for other in self.trips.values():
            if other.wait is not None and trip.agent.id in other.wait.soft:
                self.ctx.scheduler.schedule(
                    self.ctx.now, RETRY, other.agent.id, Priority.DISPATCH - 1
                )

    def _on_retry(self, ev: ScheduledEvent) -> None:
        trip = self.trips.get(str(ev.payload))
        if trip is None or trip.wait is None:
            return
        if trip.wait.kind == "no_route":
            node = self.node_of.get(trip.agent.id)
            if node is None:  # holding an edge: replan from its far end
                step = trip.route.steps[trip.index]
                node = step.start
            self._unregister(trip)
            self._replan_from(trip, node)
        elif trip.phase == "at_end":
            self._leave_edge_end(trip)
        else:
            self._attempt(trip)

    # ------------------------------------------------------------------ deadlocks

    def _blockers_now(self, agent_id: str) -> list[str]:
        trip = self.trips.get(agent_id)
        if trip is None or trip.wait is None:
            return []
        return sorted(trip.wait.current)

    def _find_cycle(self, start: str) -> list[str] | None:
        """Cycle through ``start`` in the wait-for graph (deterministic DFS)."""
        path: list[str] = []
        on_path: set[str] = set()

        def dfs(node: str) -> list[str] | None:
            path.append(node)
            on_path.add(node)
            for nxt in self._blockers_now(node):
                if nxt == start:
                    return list(path)
                if nxt not in on_path and self.trips.get(nxt) and self.trips[nxt].wait:
                    found = dfs(nxt)
                    if found:
                        return found
            path.pop()
            on_path.discard(node)
            return None

        return dfs(start)

    def _on_deadlock(self, cycle: list[str]) -> None:
        strategy = self.spec.deadlock.strategy
        edges = sorted(t.holding for a in cycle if (t := self.trips[a]).holding)
        if strategy == "timeout":
            first = self.trips[cycle[0]].wait
            if first is not None and not first.deadlock_pending:
                for a in cycle:
                    w = self.trips[a].wait
                    if w is not None:
                        w.deadlock_pending = True
                self.ctx.emit(
                    EventType.DEADLOCK,
                    agents=sorted(cycle),
                    edges=edges,
                    strategy=strategy,
                    resolved=None,
                    status="detected",
                )
                self.ctx.schedule_in(
                    self.spec.deadlock.timeout,
                    DEADLOCK_CHECK,
                    DeadlockCheck(tuple(sorted(cycle))),
                    Priority.DISRUPTION,
                )
            return
        if strategy == "none":
            self.ctx.emit(
                EventType.DEADLOCK,
                agents=sorted(cycle),
                edges=edges,
                strategy=strategy,
                resolved=False,
                status="unresolved",
            )
            self.ctx.emit(EventType.WARNING, message=f"unresolved deadlock among {sorted(cycle)}")
            for a in sorted(cycle):
                self._fail(a, "deadlock")
            return
        self._yield(cycle, reroute=strategy == "reroute", strategy=strategy, edges=edges)

    def _on_deadlock_check(self, ev: ScheduledEvent) -> None:
        assert isinstance(ev.payload, DeadlockCheck)
        for a in ev.payload.agents:
            w = self.trips[a].wait if a in self.trips else None
            if w is not None:
                w.deadlock_pending = False
        for a in ev.payload.agents:
            if a in self.trips and self.trips[a].wait is not None:
                cycle = self._find_cycle(a)
                if cycle:
                    edges = sorted(t.holding for x in cycle if (t := self.trips[x]).holding)
                    self._yield(cycle, reroute=False, strategy="timeout", edges=edges)
                return

    def _yield(self, cycle: list[str], reroute: bool, strategy: str, edges: list[str]) -> None:
        def priority(a: str) -> float:
            return float(self.trips[a].agent.entity.attributes.get("priority", 0))

        holders = [a for a in cycle if self.trips[a].holding is not None]
        yielder = max(holders, key=lambda a: (-priority(a), a))
        trip = self.trips[yielder]
        assert trip.holding is not None and trip.wait is not None
        contested = trip.wait.edge
        held = trip.holding
        self.ctx.emit(
            EventType.DEADLOCK,
            agents=sorted(cycle),
            edges=edges,
            strategy=strategy,
            resolved=True,
            status="resolved",
            yielder=yielder,
            contested_edge=contested,
        )
        self._end_wait(trip)
        # reverse along the held edge back to where it was entered
        occ = self.occupants[held][yielder]
        edge = self.nav.edges[held]
        self.ctx.emit(
            EventType.AGENT_MOVE,
            agent=yielder,
            task=trip.agent.task.id if trip.agent.task else None,
            location=held,
            source=occ.source,
            target=occ.target,
            entry=occ.entry,
            arrive=occ.planned_exit,
            length=edge.length,
            free_speed=occ.free_speed,
            speed=None,
            yielded=True,
        )
        assert occ.source is not None and occ.target is not None
        back = Step(edge, occ.target, occ.source)
        free = self._free_speed(trip, edge)
        occ.direction, occ.entry, occ.order = -occ.direction, self.ctx.now, next(self._order)
        occ.source, occ.target = occ.target, occ.source
        occ.planned_exit = self.ctx.now + edge.length / free
        occ.free_speed, occ.follow_delay, occ.leader = free, 0.0, None
        trip.route = Route(back.start, back.end, (back,))
        trip.index, trip.phase, trip.backing_out = 0, "moving", True
        trip.avoid = frozenset({contested}) if reroute and contested else frozenset()
        trip.token += 1
        self._set_state(trip.agent, "travel", edge=held, yielding=True)
        pos = self.nav.positions
        self.ctx.backend.move(
            yielder, [pos[back.start], pos[back.end]], [self.ctx.now, occ.planned_exit]
        )
        self.ctx.scheduler.schedule(
            occ.planned_exit, ARRIVE, Arrive(yielder, trip.token), Priority.COMPLETION
        )
        self._wake(held)
        if contested:
            self._wake(contested)

    def _fail(self, agent_id: str, reason: str) -> None:
        trip = self.trips.pop(agent_id)
        self._unregister(trip)
        if trip.holding is not None:
            self.occupants[trip.holding].pop(agent_id, None)
            self._wake(trip.holding)
        self.node_of[agent_id] = None
        self._set_state(trip.agent, "failed", reason=reason)
        self.hooks.on_agent_failed(trip.agent, reason)

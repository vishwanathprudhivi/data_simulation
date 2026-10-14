"""Navigation graph derived from the facility manifest (FR-FAC-02/03).

The manifest is immutable during a run. Scenario events may only change the
*overlay*: blocked edges and per-edge speed factors (FR-FAC-03). Routing is
Dijkstra by travel distance with deterministic tie-breaking on node ids.
"""

from __future__ import annotations

import heapq
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from simcore.specs.facility import FacilityManifest

__all__ = ["Edge", "NavGraph", "NoRouteError", "Route"]

Position = tuple[float, ...]


class NoRouteError(RuntimeError):
    pass


@dataclass(frozen=True)
class Edge:
    id: str
    source: str
    target: str
    length: float
    width: float
    directed: bool
    max_speed: float | None
    zone: str | None

    def other(self, node: str) -> str:
        return self.target if node == self.source else self.source

    def direction_from(self, node: str) -> int:
        """+1 when traversed source->target, -1 when target->source."""
        return 1 if node == self.source else -1


@dataclass(frozen=True)
class Step:
    edge: Edge
    start: str
    end: str

    @property
    def direction(self) -> int:
        return self.edge.direction_from(self.start)


@dataclass(frozen=True)
class Route:
    origin: str
    destination: str
    steps: tuple[Step, ...] = field(default_factory=tuple)

    @property
    def length(self) -> float:
        return sum(s.edge.length for s in self.steps)

    @property
    def nodes(self) -> tuple[str, ...]:
        return (self.origin, *(s.end for s in self.steps))


class NavGraph:
    def __init__(self, manifest: FacilityManifest) -> None:
        self.manifest = manifest
        self.positions: dict[str, Position] = {n.id: tuple(n.position) for n in manifest.nodes}
        self.node_zone: dict[str, str | None] = {n.id: n.zone for n in manifest.nodes}
        self.edges: dict[str, Edge] = {}
        self._adj: dict[str, list[tuple[str, Edge]]] = {n: [] for n in self.positions}
        for e in manifest.edges:
            if e.source not in self.positions or e.target not in self.positions:
                continue  # reported by the validator
            length = (
                e.length
                if e.length is not None
                else math.dist(self.positions[e.source][:2], self.positions[e.target][:2])
            )
            edge = Edge(e.id, e.source, e.target, length, e.width, e.directed, e.max_speed, e.zone)
            self.edges[e.id] = edge
            self._adj[e.source].append((e.target, edge))
            if not e.directed:
                self._adj[e.target].append((e.source, edge))
        for node in self._adj:
            self._adj[node].sort(key=lambda p: (p[0], p[1].id))
        self.location_node: dict[str, str] = {loc.id: loc.node for loc in manifest.locations}
        self.locations = {loc.id: loc for loc in manifest.locations}
        self.chain_id: dict[str, int] = self._chains()
        # --- overlay changed only by scenario events (FR-FAC-03) ---
        self.blocked: set[str] = set()
        self.speed_factor: dict[str, float] = {}

    def _chains(self) -> dict[str, int]:
        """Group edges into corridors: maximal runs joined by pass-through (degree-2) nodes."""
        incident: dict[str, list[str]] = {n: [] for n in self.positions}
        for e in self.edges.values():
            incident[e.source].append(e.id)
            incident[e.target].append(e.id)
        chain: dict[str, int] = {}
        next_id = 0
        for start in sorted(self.edges):
            if start in chain:
                continue
            stack = [start]
            while stack:
                eid = stack.pop()
                if eid in chain:
                    continue
                chain[eid] = next_id
                e = self.edges[eid]
                for node in (e.source, e.target):
                    if len(incident[node]) == 2:
                        stack.extend(x for x in incident[node] if x not in chain)
            next_id += 1
        return chain

    # ------------------------------------------------------------------ lookups

    def node_of(self, place: str) -> str:
        """Node for a location id or a node id."""
        if place in self.location_node:
            return self.location_node[place]
        if place in self.positions:
            return place
        raise KeyError(f"unknown location or node {place!r}")

    def position(self, place: str) -> Position:
        return self.positions[self.node_of(place)]

    def neighbors(self, node: str) -> list[tuple[str, Edge]]:
        return self._adj[node]

    # ------------------------------------------------------------------ overlay

    def edges_for(self, target: str) -> list[str]:
        """Resolve a disruption target (edge id, location id, node id, or 'zone:<id>')."""
        if target in self.edges:
            return [target]
        if target.startswith("zone:"):
            zone = target[5:]
            return [e.id for e in self.edges.values() if e.zone == zone]
        node = self.node_of(target)
        return [e.id for e in self.edges.values() if node in (e.source, e.target)]

    def block(self, edges: Iterable[str], blocked: bool) -> None:
        for e in edges:
            if blocked:
                self.blocked.add(e)
            else:
                self.blocked.discard(e)

    def degrade(self, edges: Iterable[str], factor: float, active: bool) -> None:
        for e in edges:
            current = self.speed_factor.get(e, 1.0)
            new = current * factor if active else current / factor
            if abs(new - 1.0) < 1e-12:
                self.speed_factor.pop(e, None)
            else:
                self.speed_factor[e] = new

    # ------------------------------------------------------------------ routing

    def route(self, origin: str, destination: str, avoid: Iterable[str] = ()) -> Route:
        """Shortest route by length avoiding blocked and ``avoid`` edges."""
        start, goal = self.node_of(origin), self.node_of(destination)
        if start == goal:
            return Route(start, goal)
        excluded = self.blocked | set(avoid)
        dist: dict[str, float] = {start: 0.0}
        prev: dict[str, tuple[str, Edge]] = {}
        heap: list[tuple[float, str]] = [(0.0, start)]
        done: set[str] = set()
        while heap:
            d, node = heapq.heappop(heap)
            if node in done:
                continue
            done.add(node)
            if node == goal:
                break
            for nxt, edge in self._adj[node]:
                if edge.id in excluded:
                    continue
                nd = d + edge.length
                if nd < dist.get(nxt, math.inf) - 1e-12:
                    dist[nxt] = nd
                    prev[nxt] = (node, edge)
                    heapq.heappush(heap, (nd, nxt))
        if goal not in done:
            raise NoRouteError(
                f"no route from {origin!r} to {destination!r}"
                + (f" avoiding {sorted(excluded)}" if excluded else "")
            )
        steps: list[Step] = []
        node = goal
        while node != start:
            p, edge = prev[node]
            steps.append(Step(edge, p, node))
            node = p
        return Route(start, goal, tuple(reversed(steps)))

    def reachable(self, origin: str, edges_ok: Mapping[str, bool] | None = None) -> set[str]:
        seen = {origin}
        stack = [origin]
        while stack:
            node = stack.pop()
            for nxt, edge in self._adj[node]:
                if edges_ok is not None and not edges_ok.get(edge.id, True):
                    continue
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen

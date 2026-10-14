"""Logical backend: positions are piecewise-linear interpolations of declared moves."""

from __future__ import annotations

import bisect
from collections.abc import Sequence
from typing import Any

from simcore.core.backend import MoveSegment, WorldBackend
from simcore.specs.facility import FacilityManifest

Position = tuple[float, ...]


class LogicalBackend(WorldBackend):
    mode = "logical"

    def __init__(self) -> None:
        self.facility: FacilityManifest | None = None
        self._spawn: dict[str, Position | None] = {}
        self._segments: dict[str, list[MoveSegment]] = {}
        self._ends: dict[str, list[float]] = {}

    def build(self, facility: FacilityManifest | None) -> None:
        self.facility = facility

    def spawn_agent(self, agent_id: str, agent_type: str, position: Position | None) -> None:
        if agent_id in self._spawn:
            raise ValueError(f"agent {agent_id!r} already spawned")
        self._spawn[agent_id] = position
        self._segments[agent_id] = []
        self._ends[agent_id] = []

    def move(self, agent_id: str, waypoints: Sequence[Position], times: Sequence[float]) -> None:
        if len(waypoints) != len(times) or len(waypoints) < 2:
            raise ValueError("move needs >= 2 waypoints with matching times")
        segs = self._segments[agent_id]
        ends = self._ends[agent_id]
        for (a, ta), (b, tb) in zip(zip(waypoints, times), zip(waypoints[1:], times[1:])):
            if tb < ta:
                raise ValueError("move times must be non-decreasing")
            if ends and ta < ends[-1]:
                raise ValueError(f"agent {agent_id}: overlapping moves at t={ta}")
            segs.append(MoveSegment(agent_id, ta, tb, tuple(a), tuple(b)))
            ends.append(tb)

    def position(self, agent_id: str, time: float) -> Position | None:
        segs = self._segments.get(agent_id)
        if not segs:
            return self._spawn.get(agent_id)
        i = bisect.bisect_left(self._ends[agent_id], time)
        if i >= len(segs):
            return segs[-1].end
        seg = segs[i]
        if time < seg.start_time:
            return segs[i - 1].end if i > 0 else seg.start
        return seg.at(time)

    def describe(self) -> dict[str, Any]:
        return {"mode": self.mode, "gpu": None, "physics": False}

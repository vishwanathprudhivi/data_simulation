"""``WorldBackend``: the execution substrate abstraction (FR-ENG-09, BRD 5.1/7.4).

The core and domains only talk to the world through this interface, so the same
scenario runs in ``logical`` (pure Python), ``kinematic`` and ``physics``
(Isaac Sim) modes. Implementations live in ``simcore.adapters``.
"""

from __future__ import annotations

import bisect
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

from simcore.specs.facility import FacilityManifest

__all__ = ["MoveSegment", "TrajectoryStore", "WorldBackend"]

Position = tuple[float, ...]


@dataclass(frozen=True)
class MoveSegment:
    """Straight-line motion of one agent between two positions over a sim-time interval."""

    agent_id: str
    start_time: float
    end_time: float
    start: Position
    end: Position

    def at(self, time: float) -> Position:
        if self.end_time <= self.start_time:
            return self.end
        f = min(max((time - self.start_time) / (self.end_time - self.start_time), 0.0), 1.0)
        return tuple(a + (b - a) * f for a, b in zip(self.start, self.end))


class TrajectoryStore:
    """Piecewise-linear agent trajectories in sim-time (shared by backends and replay)."""

    def __init__(self) -> None:
        self.spawn: dict[str, Position | None] = {}
        self.segments: dict[str, list[MoveSegment]] = {}
        self._ends: dict[str, list[float]] = {}

    def add_agent(self, agent_id: str, position: Position | None) -> None:
        if agent_id in self.spawn:
            raise ValueError(f"agent {agent_id!r} already spawned")
        self.spawn[agent_id] = position
        self.segments[agent_id] = []
        self._ends[agent_id] = []

    def add_move(
        self, agent_id: str, waypoints: Sequence[Position], times: Sequence[float]
    ) -> None:
        if len(waypoints) != len(times) or len(waypoints) < 2:
            raise ValueError("move needs >= 2 waypoints with matching times")
        segs, ends = self.segments[agent_id], self._ends[agent_id]
        for (a, ta), (b, tb) in zip(zip(waypoints, times), zip(waypoints[1:], times[1:])):
            if tb < ta:
                raise ValueError("move times must be non-decreasing")
            if ends and ta < ends[-1]:
                raise ValueError(f"agent {agent_id}: overlapping moves at t={ta}")
            segs.append(MoveSegment(agent_id, ta, tb, tuple(a), tuple(b)))
            ends.append(tb)

    def position(self, agent_id: str, time: float) -> Position | None:
        segs = self.segments.get(agent_id)
        if not segs:
            return self.spawn.get(agent_id)
        i = bisect.bisect_left(self._ends[agent_id], time)
        if i >= len(segs):
            return segs[-1].end
        seg = segs[i]
        if time < seg.start_time:
            return segs[i - 1].end if i > 0 else seg.start
        return seg.at(time)

    def heading(self, agent_id: str, time: float) -> float:
        """Direction of travel (radians, x-y plane) of the segment active at ``time``."""
        import math

        segs = self.segments.get(agent_id) or []
        i = bisect.bisect_left(self._ends.get(agent_id, []), time)
        seg = segs[min(i, len(segs) - 1)] if segs else None
        if seg is None or seg.start[:2] == seg.end[:2]:
            return 0.0
        return math.atan2(seg.end[1] - seg.start[1], seg.end[0] - seg.start[0])


class WorldBackend(ABC):
    """Abstract world. All methods take sim-time; none may read wall-clock."""

    mode: ClassVar[str]

    @abstractmethod
    def build(
        self, facility: FacilityManifest | None, assets: Mapping[str, str] | None = None
    ) -> None:
        """Load the facility (stage + manifest); ``assets`` maps agent types to assets."""

    @abstractmethod
    def spawn_agent(self, agent_id: str, agent_type: str, position: Position | None) -> None:
        """Create the agent's representation (asset binding happens in the adapter)."""

    @abstractmethod
    def move(self, agent_id: str, waypoints: Sequence[Position], times: Sequence[float]) -> None:
        """Move ``agent_id`` through ``waypoints`` reaching each at the matching sim-time."""

    @abstractmethod
    def position(self, agent_id: str, time: float) -> Position | None:
        """Agent position at sim-time ``time`` (``None`` if unknown)."""

    def step(self, time: float, dt: float) -> None:  # noqa: B027 - optional hook
        """Advance backend state to ``time`` (fixed-step mode)."""

    def teardown(self) -> None:  # noqa: B027 - optional hook
        """Release resources."""

    def describe(self) -> dict[str, Any]:
        """Backend provenance for run metadata (FR-OBS-06)."""
        return {"mode": self.mode}

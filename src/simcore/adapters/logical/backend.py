"""Logical backend: positions are piecewise-linear interpolations of declared moves."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from simcore.core.backend import TrajectoryStore, WorldBackend
from simcore.specs.facility import FacilityManifest

Position = tuple[float, ...]


class LogicalBackend(WorldBackend):
    mode = "logical"

    def __init__(self) -> None:
        self.facility: FacilityManifest | None = None
        self.trajectories = TrajectoryStore()

    def build(
        self, facility: FacilityManifest | None, assets: Mapping[str, str] | None = None
    ) -> None:
        self.facility = facility

    def spawn_agent(self, agent_id: str, agent_type: str, position: Position | None) -> None:
        self.trajectories.add_agent(agent_id, position)

    def move(self, agent_id: str, waypoints: Sequence[Position], times: Sequence[float]) -> None:
        self.trajectories.add_move(agent_id, waypoints, times)

    def position(self, agent_id: str, time: float) -> Position | None:
        return self.trajectories.position(agent_id, time)

    def describe(self) -> dict[str, Any]:
        return {"mode": self.mode, "gpu": None, "physics": False}

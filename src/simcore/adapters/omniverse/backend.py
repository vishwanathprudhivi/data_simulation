"""``kinematic`` (and experimental ``physics``) backend on Isaac Sim (BRD 5.1, 7.4, FR-ENG-09).

Agents follow the same graph paths and timing as in ``logical`` mode (the
movement and interaction logic is shared and deterministic, in Python); this
backend writes their transforms to the USD stage at every fixed step so the run
can be watched or rendered. ``physics`` additionally enables a PhysX scene; its
calibration use (FR-INT-05) is an M14 item.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from simcore.adapters.omniverse.compat import ISAAC_SIM_VERSION, StageAPI
from simcore.core.backend import TrajectoryStore, WorldBackend
from simcore.core.facility.graph import NavGraph
from simcore.specs.facility import FacilityManifest

__all__ = ["KinematicBackend"]

Position = tuple[float, ...]


class KinematicBackend(WorldBackend):
    mode = "kinematic"

    def __init__(
        self,
        stage_factory: Callable[[], StageAPI],
        mode: str = "kinematic",
        export_usd: str | None = None,
    ) -> None:
        self.mode = mode  # type: ignore[misc]
        self._factory = stage_factory
        self.stage: StageAPI | None = None
        self.trajectories = TrajectoryStore()
        self.assets: Mapping[str, str] = {}
        self._types: dict[str, str] = {}
        self.steps = 0
        self.export_usd = export_usd

    def build(
        self, facility: FacilityManifest | None, assets: Mapping[str, str] | None = None
    ) -> None:
        self.stage = self._factory()
        self.assets = dict(assets or {})
        self.stage.open(facility.usd_stage if facility is not None else None)
        if facility is not None and facility.usd_stage is None:
            # FR-DOM-07: minimal scene from the manifest so no 3D asset is needed to start
            nav = NavGraph(facility)
            self.stage.build_floor(
                [(z.id, z.polygon) for z in facility.zones if len(z.polygon) >= 3],
                [
                    (e.id, nav.positions[e.source], nav.positions[e.target], e.width)
                    for e in nav.edges.values()
                ],
            )

    def spawn_agent(self, agent_id: str, agent_type: str, position: Position | None) -> None:
        assert self.stage is not None, "build() first"
        self.trajectories.add_agent(agent_id, position)
        self._types[agent_id] = agent_type
        self.stage.add_agent(agent_id, self.assets.get(agent_type), (1.0, 0.8))
        if position is not None:
            self.stage.set_pose(agent_id, position, 0.0)

    def move(self, agent_id: str, waypoints: Sequence[Position], times: Sequence[float]) -> None:
        self.trajectories.add_move(agent_id, waypoints, times)

    def position(self, agent_id: str, time: float) -> Position | None:
        return self.trajectories.position(agent_id, time)

    def step(self, time: float, dt: float) -> None:
        """Write every agent's interpolated pose at ``time`` and advance the app."""
        assert self.stage is not None
        for agent_id in self.trajectories.spawn:
            pos = self.trajectories.position(agent_id, time)
            if pos is not None:
                self.stage.set_pose(agent_id, pos, self.trajectories.heading(agent_id, time))
        self.stage.update(time)
        self.steps += 1

    def teardown(self) -> None:
        if self.stage is not None:
            if self.export_usd:
                self.stage.save(self.export_usd)
            self.stage.close()
            self.stage = None

    def describe(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "isaac_sim": ISAAC_SIM_VERSION,
            "steps": self.steps,
            "physics": self.mode == "physics",
        }

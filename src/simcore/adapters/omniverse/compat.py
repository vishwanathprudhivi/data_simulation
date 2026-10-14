"""Isaac Sim / USD compatibility shim (ADR-001). The ONLY module that imports
``isaacsim``, ``omni`` or ``pxr``; every import happens inside a function so the
rest of simcore imports cleanly without Isaac Sim.

:class:`StageAPI` is the narrow surface the kinematic backend needs. The real
implementation is :class:`IsaacStage`; tests use a fake with the same methods.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Sequence
from typing import Any, Protocol

__all__ = ["ISAAC_SIM_VERSION", "IsaacStage", "StageAPI", "isaac_available", "require_isaac"]

#: Pinned target version; see docs/decisions/ADR-001.md.
ISAAC_SIM_VERSION = "5.0.0"

Position = tuple[float, ...]


def isaac_available() -> bool:
    return importlib.util.find_spec("isaacsim") is not None


def require_isaac() -> None:
    if not isaac_available():
        raise ImportError(
            f"Isaac Sim {ISAAC_SIM_VERSION} is not installed in this Python environment; "
            "kinematic/physics modes need it (logical mode does not)"
        )


class StageAPI(Protocol):
    def open(self, usd_path: str | None) -> None: ...

    def build_floor(
        self,
        polygons: Sequence[tuple[str, Sequence[tuple[float, float]]]],
        edges: Sequence[tuple[str, Position, Position, float]],
    ) -> None: ...

    def add_agent(self, agent_id: str, asset: str | None, size: tuple[float, float]) -> None: ...

    def set_pose(self, agent_id: str, position: Position, heading: float) -> None: ...

    def update(self, sim_time: float) -> None: ...

    def save(self, path: str) -> None: ...

    def close(self) -> None: ...


class IsaacStage:
    """Headless Isaac Sim stage (standalone workflow). Requires a GPU and Isaac Sim."""

    def __init__(
        self, headless: bool = True, physics: bool = False, renderer: str = "RayTracedLighting"
    ) -> None:
        require_isaac()
        from isaacsim import SimulationApp  # version-specific (Isaac Sim 4.5+ / 5.x)

        self._app = SimulationApp({"headless": headless, "renderer": renderer})
        self.physics = physics
        self._ops: dict[str, tuple[Any, Any]] = {}

    # imports below must follow SimulationApp creation
    def _usd(self) -> tuple[Any, Any, Any, Any]:
        import omni.usd
        from pxr import Gf, UsdGeom, UsdPhysics

        return omni.usd, Gf, UsdGeom, UsdPhysics

    def open(self, usd_path: str | None) -> None:
        omni_usd, _, UsdGeom, UsdPhysics = self._usd()
        ctx = omni_usd.get_context()
        if usd_path:
            ctx.open_stage(usd_path)
        else:
            ctx.new_stage()
        self._stage = ctx.get_stage()
        UsdGeom.SetStageMetersPerUnit(self._stage, 1.0)
        UsdGeom.SetStageUpAxis(self._stage, UsdGeom.Tokens.z)
        UsdGeom.Xform.Define(self._stage, "/World/Agents")
        if self.physics:
            UsdPhysics.Scene.Define(self._stage, "/World/PhysicsScene")

    def build_floor(
        self,
        polygons: Sequence[tuple[str, Sequence[tuple[float, float]]]],
        edges: Sequence[tuple[str, Position, Position, float]],
    ) -> None:
        _, Gf, UsdGeom, _ = self._usd()
        UsdGeom.Xform.Define(self._stage, "/World/Facility")
        for zid, poly in polygons:
            mesh = UsdGeom.Mesh.Define(self._stage, f"/World/Facility/zone_{zid}")
            mesh.CreatePointsAttr([Gf.Vec3f(x, y, 0.0) for x, y in poly])
            mesh.CreateFaceVertexCountsAttr([len(poly)])
            mesh.CreateFaceVertexIndicesAttr(list(range(len(poly))))
        for eid, a, b, width in edges:
            cube = UsdGeom.Cube.Define(self._stage, f"/World/Facility/edge_{eid}")
            cube.CreateSizeAttr(1.0)
            xf = UsdGeom.Xformable(cube)
            mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, 0.005)
            length = ((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5
            import math

            xf.AddTranslateOp().Set(Gf.Vec3d(*mid))
            xf.AddRotateZOp().Set(math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])))
            xf.AddScaleOp().Set(Gf.Vec3f(length, width, 0.01))

    def add_agent(self, agent_id: str, asset: str | None, size: tuple[float, float]) -> None:
        _, _, UsdGeom, _ = self._usd()
        path = f"/World/Agents/{agent_id.replace('-', '_')}"
        xform = UsdGeom.Xform.Define(self._stage, path)
        if asset and not asset.startswith("placeholder://"):
            xform.GetPrim().GetReferences().AddReference(asset)
        else:  # BRD Q4: placeholder primitives until assets are supplied
            cap = UsdGeom.Capsule.Define(self._stage, f"{path}/body")
            cap.CreateRadiusAttr(max(size[1], 0.2) / 2)
            cap.CreateHeightAttr(max(size[0] - size[1], 0.1))
            cap.CreateAxisAttr("X")
        xf = UsdGeom.Xformable(xform)
        self._ops[agent_id] = (xf.AddTranslateOp(), xf.AddRotateZOp())

    def set_pose(self, agent_id: str, position: Position, heading: float) -> None:
        import math

        _, Gf, _, _ = self._usd()
        translate, rotate = self._ops[agent_id]
        z = position[2] if len(position) > 2 else 0.0
        translate.Set(Gf.Vec3d(position[0], position[1], z + 0.5))
        rotate.Set(math.degrees(heading))

    def update(self, sim_time: float) -> None:
        self._app.update()

    def save(self, path: str) -> None:
        self._stage.GetRootLayer().Export(path)

    def close(self) -> None:
        self._app.close()

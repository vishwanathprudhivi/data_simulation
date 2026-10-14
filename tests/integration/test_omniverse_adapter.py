"""Omniverse adapter (M4). CPU tests drive the kinematic backend with a fake stage;
``isaac``-marked tests need Isaac Sim + GPU (AC-X-01, AC-X-02) and are skipped here."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from simcore.adapters.omniverse.backend import KinematicBackend
from simcore.adapters.omniverse.replay import replay_to_stage
from simcore.api import default_plugins, run_scenario
from simcore.core.engine import Engine
from simcore.core.facility.graph import NavGraph
from simcore.core.obs.eventlog import EventLog
from simcore.core.obs.replay import trajectories_from_log
from tests.conftest import WAREHOUSE

Position = tuple[float, ...]


class FakeStage:
    """Records what the kinematic backend asks of the stage (same surface as IsaacStage)."""

    def __init__(self) -> None:
        self.opened: list[str | None] = []
        self.floor: tuple[int, int] | None = None
        self.agents: dict[str, str | None] = {}
        self.poses: dict[str, tuple[Position, float]] = {}
        self.pose_writes = 0
        self.updates: list[float] = []
        self.closed = False
        self.saved: str | None = None

    def open(self, usd_path: str | None) -> None:
        self.opened.append(usd_path)

    def build_floor(self, polygons: Sequence[Any], edges: Sequence[Any]) -> None:
        self.floor = (len(polygons), len(edges))

    def add_agent(self, agent_id: str, asset: str | None, size: tuple[float, float]) -> None:
        self.agents[agent_id] = asset

    def set_pose(self, agent_id: str, position: Position, heading: float) -> None:
        self.poses[agent_id] = (tuple(position), heading)
        self.pose_writes += 1

    def update(self, sim_time: float) -> None:
        self.updates.append(sim_time)

    def save(self, path: str) -> None:
        self.saved = path

    def close(self) -> None:
        self.closed = True


def _engine(stages: list[FakeStage], **kw: Any) -> Engine:
    def factory(mode: str) -> KinematicBackend:
        def make() -> FakeStage:
            stages.append(FakeStage())
            return stages[-1]

        return KinematicBackend(make, mode=mode, **kw)

    return Engine(default_plugins(), factory, mode="kinematic")


def test_FR_ENG_09_kinematic_backend_writes_stage_each_step() -> None:
    stages: list[FakeStage] = []
    engine = _engine(stages, export_usd="/tmp/out.usd")
    engine.load_config(WAREHOUSE).build_world()
    result = engine.run_replication(3, 0, until=600.0, step=1.0)
    stage = stages[-1]
    assert stage.opened == [None] and stage.floor == (7, 92)  # 6 aisles + staging, 92 edges
    assert set(stage.agents.values()) == {"placeholder://capsule"}  # FR-AGT-06 asset binding
    assert len(stage.updates) == 600 and stage.updates[-1] == pytest.approx(600.0)
    assert stage.pose_writes >= 600 * 12
    assert result.meta["backend"]["steps"] == 600
    engine.teardown()
    assert stage.closed and stage.saved == "/tmp/out.usd"


def test_AC_X_02_kinematic_logic_matches_logical_mode() -> None:
    """With the shared movement model the kinematic event log equals the logical one except
    for the ``sim.start`` record naming the mode, so KPIs agree exactly (ADR-008). The GPU
    test below re-checks this on a real Isaac stage."""
    from simcore.adapters import get_backend

    stages: list[FakeStage] = []
    kin_engine = _engine(stages)
    kin_engine.load_config(WAREHOUSE).build_world()
    kin = kin_engine.run_replication(3, 0, until=1800.0, step=0.5)
    log_engine = Engine(default_plugins(), get_backend)
    log_engine.load_config(WAREHOUSE).build_world()
    lr = log_engine.run_replication(3, 0, until=1800.0)
    assert kin.events.to_table().slice(1).equals(lr.events.to_table().slice(1))
    assert kin.events.record(0).data["mode"] == "kinematic"
    assert kin.kpis == lr.kpis


def test_FR_OBS_04_replay_drives_stage_from_event_log(tmp_path: Path) -> None:
    result = run_scenario(WAREHOUSE, seed=1, out=tmp_path)
    log = EventLog.from_parquet(tmp_path / "events.parquet")
    from simcore.core.config import load_scenario

    nav = NavGraph(load_scenario(WAREHOUSE).facility)  # type: ignore[arg-type]
    stage = FakeStage()
    frames = replay_to_stage(log, nav, stage, start=0.0, end=120.0, dt=2.0)
    assert frames == 61 and len(stage.agents) == 12
    store = trajectories_from_log(log, nav)
    for agent, (pos, _) in stage.poses.items():
        assert pos == pytest.approx(store.position(agent, 120.0))
    assert result.meta["mode"] == "logical"


def test_adapter_imports_without_isaac_and_refuses_cleanly() -> None:
    from simcore.adapters import BackendUnavailableError, get_backend
    from simcore.adapters.omniverse.compat import isaac_available

    if isaac_available():
        pytest.skip("Isaac Sim is installed")
    with pytest.raises((BackendUnavailableError, ImportError), match="Isaac Sim"):
        get_backend("kinematic")


@pytest.mark.isaac
def test_AC_X_01_reference_runs_headless_in_isaac_kinematic(tmp_path: Path) -> None:
    result = run_scenario(WAREHOUSE, seed=1, mode="kinematic", out=tmp_path, step=0.5)
    assert result.meta["backend"]["mode"] == "kinematic"
    log = EventLog.from_parquet(tmp_path / "events.parquet")
    assert len(log) > 0  # replayable event log


@pytest.mark.isaac
def test_AC_X_02_headline_kpis_agree_within_10pct_on_isaac() -> None:
    kin = run_scenario(WAREHOUSE, seed=2, mode="kinematic", step=0.5).kpis
    log = run_scenario(WAREHOUSE, seed=2).kpis
    for kpi in ("throughput", "cycle_time_mean", "utilization", "dock_on_time_rate"):
        assert abs(kin[kpi] - log[kpi]) <= 0.10 * abs(log[kpi]), kpi

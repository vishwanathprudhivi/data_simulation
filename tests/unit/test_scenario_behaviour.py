"""Scenario-layer behaviour: demand sources, disruptions, dispatch modes (FR-SCN-*, FR-DSP-04)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from simcore.api import run_scenario
from simcore.core.obs.eventlog import EventType
from tests.conftest import MMC

BEHAVIORS = (MMC.parent / "behaviors.yaml").read_text()


def _scenario(tmp: Path, **overrides: Any) -> Path:
    (tmp / "behaviors.yaml").write_text(BEHAVIORS)
    doc: dict[str, Any] = {
        "id": "t",
        "domain": "toy_queue",
        "behaviors": "behaviors.yaml",
        "duration": 500,
        "seed": 3,
        "fleet": [{"agent_type": "server", "count": 2}],
        "demand": [{"kind": "stochastic", "interarrival": {"kind": "exponential", "mean": 1.0}}],
    }
    doc.update(overrides)
    path = tmp / "s.yaml"
    path.write_text(yaml.safe_dump(doc))
    return path


def test_FR_SCN_02_list_demand(tmp_path: Path) -> None:
    tasks = [{"id": f"job{i}", "release_time": float(i)} for i in range(5)]
    path = _scenario(tmp_path, demand=[{"kind": "list", "tasks": tasks}], duration=100)
    result = run_scenario(path)
    released = [r.task for r in result.events.of_type(EventType.TASK_RELEASED)]
    assert released == [f"job{i}" for i in range(5)]
    assert result.kpis["tasks_completed"] == 5


def test_FR_SCN_02_replay_demand_csv(tmp_path: Path) -> None:
    csv = tmp_path / "hist.csv"
    csv.write_text("order,t,due,lane\nA1,1.5,30,L1\nA2,0.5,,L2\nA3,4.0,10,L1\n")
    path = _scenario(
        tmp_path,
        demand=[
            {
                "kind": "replay",
                "path": "hist.csv",
                "columns": {"id": "order", "release_time": "t", "due_time": "due"},
            }
        ],
        duration=60,
    )
    result = run_scenario(path)
    released = list(result.events.of_type(EventType.TASK_RELEASED))
    assert [r.task for r in released] == ["A2", "A1", "A3"]
    assert released[1].data["due_time"] == 30.0
    assert result.kpis["tasks_completed"] == 3


def test_FR_SCN_03_resource_unavailable_reduces_capacity(tmp_path: Path) -> None:
    base = run_scenario(_scenario(tmp_path))
    outage = [
        {
            "id": "out",
            "type": "resource_unavailable",
            "at": 100,
            "duration": 200,
            "target": "type:server",
            "params": {"count": 1},
        }
    ]
    hit = run_scenario(_scenario(tmp_path, disruptions=outage))
    assert hit.kpis["state_share_unavailable"] > 0
    assert hit.kpis["wait_time_mean"] > base.kpis["wait_time_mean"]
    states = [
        r.data["state"]
        for r in hit.events.of_type(EventType.AGENT_STATE)
        if r.agent == "server-000"
    ]
    assert "unavailable" in states


def test_FR_SCN_03_demand_surge_raises_arrivals(tmp_path: Path) -> None:
    base = run_scenario(_scenario(tmp_path))
    surge = [{"id": "s", "type": "demand_surge", "at": 0, "params": {"factor": 1.5}}]
    hit = run_scenario(_scenario(tmp_path, disruptions=surge))
    assert hit.kpis["tasks_released"] > 1.3 * base.kpis["tasks_released"]


def test_FR_SCN_04_condition_triggered_disruption(tmp_path: Path) -> None:
    trig = [
        {
            "id": "q",
            "type": "resource_unavailable",
            "duration": 10,
            "trigger": {"signal": "released_tasks", "op": ">=", "threshold": 50},
        }
    ]
    result = run_scenario(_scenario(tmp_path, disruptions=trig))
    starts = list(result.events.of_type(EventType.DISRUPTION_START))
    released_before = sum(
        1 for r in result.events.of_type(EventType.TASK_RELEASED) if r.time <= starts[0].time
    )
    assert len(starts) == 1 and released_before == 50


def test_FR_DSP_04_push_and_pull_modes_both_run(tmp_path: Path) -> None:
    pull = run_scenario(_scenario(tmp_path, policy={"name": "fifo", "assignment": "pull"}))
    push = run_scenario(_scenario(tmp_path, policy={"name": "fifo", "assignment": "push"}))
    triggers = {r.data["trigger"] for r in push.events.of_type(EventType.POLICY_DECISION)}
    assert triggers == {"push"}
    assert pull.kpis["tasks_completed"] > 0 and push.kpis["tasks_completed"] > 0


def test_FR_AGT_05_heterogeneous_fleet(tmp_path: Path) -> None:
    (tmp_path / "behaviors.yaml").write_text(
        "agent_types:\n"
        "  - {id: fast, service_times: {default: {kind: constant, value: 0.5}}}\n"
        "  - {id: slow, service_times: {default: {kind: constant, value: 2.0}}}\n"
    )
    doc = {
        "id": "h",
        "domain": "toy_queue",
        "behaviors": "behaviors.yaml",
        "duration": 100,
        "fleet": [{"agent_type": "fast", "count": 1}, {"agent_type": "slow", "count": 1}],
        "demand": [{"kind": "stochastic", "interarrival": {"kind": "exponential", "mean": 1.0}}],
    }
    (tmp_path / "h.yaml").write_text(yaml.safe_dump(doc))
    result = run_scenario(tmp_path / "h.yaml")
    services = {
        r.agent: r.data["service_time"] for r in result.events.of_type(EventType.TASK_STARTED)
    }
    assert services["fast-000"] == 0.5 and services["slow-000"] == 2.0

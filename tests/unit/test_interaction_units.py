"""Location work, co-location, facility disruptions, congestion KPIs (FR-INT-04/06, FR-SCN-03)."""

from __future__ import annotations

from typing import Any

import pytest

from simcore.core.interaction import GeometricInteractionModel
from simcore.core.interaction.base import EdgeOccupant
from simcore.core.obs.eventlog import EventType
from simcore.specs.scenario import InteractionSpec
from tests.support.courier import bundle, corridor
from tests.validation.test_interaction import completed, run


def test_FR_INT_02_geometric_model() -> None:
    m = GeometricInteractionModel(InteractionSpec())
    assert m.lanes(2.4, 0.8) == 2 and m.lanes(1.6, 0.8) == 1 and m.lanes(0.5, 0.8) == 1
    assert m.can_pass(1.8, 0.8, 0.8) and not m.can_pass(1.79, 0.8, 0.8)
    me = EdgeOccupant("a", 0.8, 1)
    assert m.effective_speed(1.0, 2.0, False, me, []) == 1.0
    assert m.effective_speed(1.0, 2.0, False, me, [EdgeOccupant("b", 0.8, 1)]) == pytest.approx(0.8)
    assert m.effective_speed(1.0, 2.0, False, me, [EdgeOccupant("b", 0.8, -1)]) == pytest.approx(
        0.48
    )
    crowd = [EdgeOccupant(str(i), 0.8, 1) for i in range(10)]
    assert m.effective_speed(1.0, 2.0, False, me, crowd) == pytest.approx(0.2)  # floor


SPOT = corridor({"W": (0, 0), "E": (10, 0)}, [("aisle", "W", "E", 3.0)])


def _work(n_agents: int, penalty: float, capacity: int = 2) -> Any:
    fac = SPOT.model_copy(
        update={
            "locations": [loc.model_copy(update={"capacity": capacity}) for loc in SPOT.locations]
        }
    )
    return run(
        bundle(
            fac,
            [{"id": "w", "speed": 1.0, "count": n_agents, "start": "at_W"}],
            [
                {"id": f"t{i}", "origin": "at_E", "destination": "at_W", "release_time": 0}
                for i in range(n_agents)
            ],
            interaction={"colocation": {"penalty_per_agent": penalty}},
            params={"work_time": 10.0},
        )
    )


def test_FR_INT_04_colocation_penalty_and_kpi() -> None:
    r = _work(2, penalty=0.5)
    starts = [s.data for s in r.events.of_type(EventType.SERVICE_START)]
    assert [s["colocated"] for s in starts] == [0, 1]
    assert [s["duration"] for s in starts] == pytest.approx([10.0, 15.0])
    assert r.kpis["colocation_time"] > 0
    assert list(r.events.of_type(EventType.COLOCATION))


def test_FR_INT_06_capacity_queueing_logged_as_space_blocking() -> None:
    r = _work(2, penalty=0.0, capacity=1)
    space = [b for b in r.events.of_type(EventType.BLOCKING) if b.data["kind"] == "space"]
    # first agent: arrives 10 s, works until 20 s. Second follows in the other lane at
    # 1 x (1 - 0.5 x 0.8/3) m/s, arrives 11.538 s and waits for the spot until 20 s.
    arrival = 10 / (1 - 0.5 * 0.8 / 3)
    assert len(space) == 1 and space[0].data["duration"] == pytest.approx(20 - arrival)
    assert space[0].data["blockers"] == ["w-000"]
    assert r.kpis["blocked_time_space"] == pytest.approx(space[0].data["duration"])


def test_FR_SCN_03_location_blocked_edge_forces_reroute() -> None:
    fac = corridor(
        {"W": (0, 0), "E": (10, 0), "N": (5, 8)},
        [("direct", "W", "E", 3.0), ("wn", "W", "N", 3.0), ("ne", "N", "E", 3.0)],
    )
    tasks = [{"id": "t", "origin": "at_W", "destination": "at_E", "release_time": 1}]
    agents = [{"id": "w", "speed": 1.0, "start": "at_W"}]
    base = run(bundle(fac, agents, tasks))
    b = bundle(fac, agents, tasks)
    b = b.with_overrides(
        {"disruptions": [{"id": "x", "type": "location_blocked", "at": 0, "target": "direct"}]}
    )
    hit = run(b)
    edges = [m.location for m in hit.events.of_type(EventType.AGENT_MOVE)]
    assert edges == ["wn", "ne"]
    assert completed(hit)["t"] > completed(base)["t"]


def test_FR_SCN_03_degraded_edge_slows_and_capacity_change() -> None:
    tasks = [{"id": "t", "origin": "at_W", "destination": "at_E", "release_time": 0}]
    agents = [{"id": "w", "speed": 1.0, "start": "at_W"}]
    b = bundle(SPOT, agents, tasks).with_overrides(
        {
            "disruptions": [
                {
                    "id": "slow",
                    "type": "location_degraded",
                    "at": 0,
                    "target": "aisle",
                    "params": {"factor": 0.5},
                }
            ]
        }
    )
    assert completed(run(b))["t"] == pytest.approx(20.0)
    closed = bundle(SPOT, agents, tasks, params={"work_time": 1.0}).with_overrides(
        {
            "disruptions": [
                {
                    "id": "shut",
                    "type": "location_blocked",
                    "at": 0,
                    "duration": 50,
                    "target": "at_W",
                }
            ]
        }
    )
    # the task starts at W: work there waits until the location reopens at t=50
    assert completed(run(closed))["t"] == pytest.approx(50 + 1 + 10)


def test_no_route_waits_until_reopened() -> None:
    tasks = [{"id": "t", "origin": "at_W", "destination": "at_E", "release_time": 0}]
    agents = [{"id": "w", "speed": 1.0, "start": "at_W"}]
    b = bundle(SPOT, agents, tasks).with_overrides(
        {
            "disruptions": [
                {"id": "x", "type": "location_blocked", "at": 0, "duration": 30, "target": "aisle"}
            ]
        }
    )
    r = run(b)
    assert completed(r)["t"] == pytest.approx(40.0)
    assert r.kpis["blocked_time_no_route"] == pytest.approx(30.0)


def test_movement_is_deterministic_and_positions_interpolate() -> None:
    b = bundle(
        SPOT,
        [{"id": "w", "speed": 2.0, "count": 3, "start": "at_W"}],
        [
            {"id": f"t{i}", "origin": "at_W", "destination": "at_E", "release_time": i}
            for i in range(6)
        ],
    )
    r1, r2 = run(b), run(b)
    assert r1.events.to_table().equals(r2.events.to_table())
    assert r1.kpis["travel_distance_total"] == pytest.approx(6 * 10 + 3 * 10)

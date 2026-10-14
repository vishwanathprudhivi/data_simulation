"""AC-INT-01 / AC-INT-02: interaction outcomes match hand calculations; deadlocks resolve.

Default interaction parameters: clearance 0.2 m, passing factor 0.6, density
alpha 0.5, min factor 0.2. Agents are 0.8 m wide and 1.0 m long.
"""

from __future__ import annotations

from typing import Any

import pytest

from simcore.adapters import get_backend
from simcore.core.config import ScenarioBundle
from simcore.core.engine import Engine, RunResult
from simcore.core.obs.eventlog import EventType
from simcore.core.plugins import PluginRegistry
from tests.support.courier import CourierPlugin, bundle, corridor

PLUGINS = PluginRegistry([CourierPlugin()])


def run(b: ScenarioBundle) -> RunResult:
    engine = Engine(PLUGINS, get_backend)
    engine.load_config(b).build_world()
    return engine.run_replication(0, 0)


def completed(r: RunResult) -> dict[str, float]:
    return {str(x.task): x.time for x in r.events.of_type(EventType.TASK_COMPLETED)}


def moves(r: RunResult, agent: str) -> list[dict[str, Any]]:
    return [
        {"edge": m.location, "entry": m.data["entry"], "exit": m.time, "speed": m.data["speed"]}
        for m in r.events.of_type(EventType.AGENT_MOVE)
        if m.agent == agent
    ]


def blocking(r: RunResult) -> list[dict[str, Any]]:
    return [{"agent": b.agent, **b.data} for b in r.events.of_type(EventType.BLOCKING)]


NARROW = corridor({"W": (0, 0), "E": (30, 0)}, [("aisle", "W", "E", 1.6)])
WIDE = corridor({"W": (0, 0), "E": (30, 0)}, [("aisle", "W", "E", 2.0)])


def test_AC_INT_01_head_on_narrow_aisle_waits_for_clearance() -> None:
    # 1.6 m < 0.8 + 0.8 + 0.2 => no passing. A: 0 -> 20 s. B waits 5 -> 20, then 20 -> 40.
    b = bundle(
        NARROW,
        [{"id": "a", "speed": 1.5, "start": "at_W"}, {"id": "b", "speed": 1.5, "start": "at_E"}],
        [
            {"id": "t1", "origin": "at_W", "destination": "at_E", "release_time": 0},
            {"id": "t2", "origin": "at_E", "destination": "at_W", "release_time": 5},
        ],
    )
    r = run(b)
    assert completed(r) == pytest.approx({"t1": 20.0, "t2": 40.0})
    (blk,) = blocking(r)
    assert blk["agent"] == "b-000" and blk["kind"] == "opposing"
    assert blk["blockers"] == ["a-000"] and blk["duration"] == pytest.approx(15.0)
    assert moves(r, "b-000")[0]["speed"] == pytest.approx(1.5)


def test_AC_INT_01_wide_aisle_passing_slows_both_ways() -> None:
    # 2.0 m >= 1.8 => pass. B's speed = 1.5 x (1 - 0.5 x 0.8/2.0) x 0.6 = 0.72 m/s.
    b = bundle(
        WIDE,
        [{"id": "a", "speed": 1.5, "start": "at_W"}, {"id": "b", "speed": 1.5, "start": "at_E"}],
        [
            {"id": "t1", "origin": "at_W", "destination": "at_E", "release_time": 0},
            {"id": "t2", "origin": "at_E", "destination": "at_W", "release_time": 5},
        ],
    )
    r = run(b)
    assert completed(r)["t2"] == pytest.approx(5 + 30 / 0.72)
    assert moves(r, "b-000")[0]["speed"] == pytest.approx(0.72)
    assert blocking(r) == []


def test_AC_INT_01_single_lane_following_no_overtaking() -> None:
    # slow leader 1.0 m/s exits at 30 s. Fast follower enters at 2 s:
    # speed 1.5 x (1 - 0.5 x 0.8/1.6) = 1.125 -> unconstrained exit 28.667 s;
    # headway = (length 1.0 + clearance 0.2) / free speed 1.5 = 0.8 s -> exit 30.8 s.
    b = bundle(
        NARROW,
        [
            {"id": "slow", "speed": 1.0, "start": "at_W"},
            {"id": "fast", "speed": 1.5, "start": "at_W"},
        ],
        [
            {
                "id": "t1",
                "origin": "at_W",
                "destination": "at_E",
                "release_time": 0,
                "attributes": {"requires": "slow"},
            },
            {
                "id": "t2",
                "origin": "at_W",
                "destination": "at_E",
                "release_time": 2,
                "attributes": {"requires": "fast"},
            },
        ],
    )
    # capability routing: slow agent takes t1, fast takes t2
    b = b.__class__(
        b.scenario,
        b.behaviors.model_copy(
            update={
                "agent_types": [  # type: ignore[union-attr]
                    a.model_copy(update={"capabilities": [a.id]})
                    for a in b.behaviors.agent_types  # type: ignore[union-attr]
                ]
            }
        ),
        b.facility,
        b.source,
        b.hashes,
    )
    r = run(b)
    assert completed(r) == pytest.approx({"t1": 30.0, "t2": 30.8})
    (blk,) = blocking(r)
    assert blk["kind"] == "following" and blk["blockers"] == ["slow-000"]
    assert blk["duration"] == pytest.approx(30.8 - (2 + 30 / 1.125))


# A side spur at M splits W-M-E into two corridors, so corridor reservation cannot
# see the oncoming agent and the two meet head-on at M: a genuine deadlock.
THREE = corridor(
    {"W": (0, 0), "M": (10, 0), "E": (20, 0), "S": (10, -5)},
    [("west", "W", "M", 1.6), ("east", "M", "E", 1.6), ("spur", "M", "S", 1.6)],
)


def _head_on(strategy: str, facility: Any = THREE, **extra: Any) -> RunResult:
    return run(
        bundle(
            facility,
            [{"id": "m", "speed": 1.0, "count": 2, "start": ["at_W", "at_E"]}],
            [
                {"id": "t1", "origin": "at_W", "destination": "at_E", "release_time": 0},
                {"id": "t2", "origin": "at_E", "destination": "at_W", "release_time": 0},
            ],
            interaction={"deadlock": {"strategy": strategy, **extra}},
        )
    )


def test_AC_INT_02_deadlock_detected_and_resolved_by_priority_yield() -> None:
    r = _head_on("priority_yield")
    (dl,) = list(r.events.of_type(EventType.DEADLOCK))
    assert dl.time == pytest.approx(10.0)
    assert dl.data["resolved"] is True and dl.data["yielder"] == "m-001"
    assert dl.data["agents"] == ["m-000", "m-001"]
    done = completed(r)
    # m-001 backs out to E (10 -> 20). m-000 follows it into 'east': speed 0.75 m/s with
    # density, exit 10 + 13.333 = 23.333. m-001 then waits for 'east' to clear and goes
    # 23.333 -> 33.333 -> 43.333.
    assert done["t1"] == pytest.approx(10 + 10 / 0.75)
    assert done["t2"] == pytest.approx(10 + 10 / 0.75 + 20)
    assert r.kpis["deadlocks_detected"] == 1 and r.kpis["deadlocks_unresolved"] == 0


def test_AC_INT_02_timeout_strategy_waits_then_yields() -> None:
    r = _head_on("timeout", timeout=30)
    statuses = [(d.time, d.data["status"]) for d in r.events.of_type(EventType.DEADLOCK)]
    assert statuses == [(pytest.approx(10.0), "detected"), (pytest.approx(40.0), "resolved")]
    assert set(completed(r)) == {"t1", "t2"}
    assert r.kpis["deadlocks_detected"] == 1


def test_AC_INT_02_reroute_uses_bypass() -> None:
    bypass = corridor(
        {
            "W": (0, 0),
            "M": (10, 0),
            "E": (20, 0),
            "N": (10, 10),
            "S": (10, -5),
            "WS": (-5, 0),
            "ES": (25, 0),
        },
        [
            ("west", "W", "M", 1.6),
            ("east", "M", "E", 1.6),
            ("spur", "M", "S", 1.6),
            ("n_w", "W", "N", 3.0),
            ("n_e", "N", "E", 3.0),
            ("w_spur", "W", "WS", 3.0),
            ("e_spur", "E", "ES", 3.0),
        ],
    )
    # make the corridor the shortest path for both so they meet head-on
    r = _head_on("reroute", facility=bypass)
    (dl,) = list(r.events.of_type(EventType.DEADLOCK))
    assert dl.data["resolved"] is True
    yielder_edges = [m["edge"] for m in moves(r, dl.data["yielder"])]
    assert "n_e" in yielder_edges or "n_w" in yielder_edges
    assert set(completed(r)) == {"t1", "t2"}


def test_AC_INT_02_unresolved_deadlock_is_a_failure_record_not_a_hang() -> None:
    r = _head_on("none")
    (dl,) = list(r.events.of_type(EventType.DEADLOCK))
    assert dl.data["resolved"] is False and dl.data["status"] == "unresolved"
    states = {s.agent: s.data["state"] for s in r.events.of_type(EventType.AGENT_STATE)}
    assert states == {"m-000": "failed", "m-001": "failed"}
    assert r.kpis["deadlocks_unresolved"] == 1
    assert completed(r) == {}
    assert r.meta["sim_time"]["end"] == 200.0  # ran to the end
    requeued = {x.task for x in r.events.of_type(EventType.TASK_REQUEUED)}
    assert requeued == {"t1", "t2"}


def test_corridor_reservation_prevents_head_on_deadlock() -> None:
    """Without a spur the corridor W-M-E is one chain: B waits at its entrance instead."""
    straight = corridor(
        {"W": (0, 0), "M": (10, 0), "E": (20, 0)},
        [("west", "W", "M", 1.6), ("east", "M", "E", 1.6)],
    )
    r = _head_on("none", facility=straight)
    assert list(r.events.of_type(EventType.DEADLOCK)) == []
    assert completed(r) == pytest.approx({"t1": 20.0, "t2": 40.0})


def test_corridor_waiter_woken_when_blocker_leaves_by_far_edge() -> None:
    """Regression: B waits at W for A, who is on the far edge of the corridor and stops at M.

    A never releases B's first edge, so B must be woken by the release of 'east'.
    """
    straight = corridor(
        {"W": (0, 0), "M": (10, 0), "E": (20, 0)},
        [("west", "W", "M", 1.6), ("east", "M", "E", 1.6)],
    )
    r = run(
        bundle(
            straight,
            [{"id": "m", "speed": 1.0, "count": 2, "start": ["at_E", "at_W"]}],
            [
                {"id": "a", "origin": "at_E", "destination": "at_M", "release_time": 0},
                {"id": "b", "origin": "at_W", "destination": "at_E", "release_time": 1},
            ],
        )
    )
    assert completed(r) == pytest.approx({"a": 10.0, "b": 30.0})

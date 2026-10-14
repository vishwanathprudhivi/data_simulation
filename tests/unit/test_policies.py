"""Policy contract and built-ins (FR-AGT-02, FR-POL-01/05)."""

from __future__ import annotations

import pytest

from simcore.core.policy import (
    AgentView,
    DispatchObservation,
    FifoPolicy,
    NearestTaskPolicy,
    PolicyError,
    TaskView,
    ZoneAffinityPolicy,
    make_policy,
)


def _task(id_: str, pos: tuple[float, float] | None = None, zone: str | None = None) -> TaskView:
    return TaskView(
        id_, "t", 0, 0.0, origin_position=pos, attributes={"zone": zone} if zone else {}
    )


def _pairs(action: object) -> list[tuple[str, str]]:
    return [(a.agent_id, a.task_id) for a in action.assignments]  # type: ignore[attr-defined]


def test_FR_AGT_02_fifo_pairs_in_order() -> None:
    obs = DispatchObservation(
        0.0,
        "pull",
        (AgentView("a1", "x"), AgentView("a2", "x")),
        (_task("t1"), _task("t2"), _task("t3")),
    )
    assert _pairs(FifoPolicy().decide(obs)) == [("a1", "t1"), ("a2", "t2")]


def test_FR_AGT_02_nearest_task_pull_and_push() -> None:
    agents = (AgentView("a1", "x", (0.0, 0.0)), AgentView("a2", "x", (10.0, 0.0)))
    tasks = (_task("far", (9.0, 0.0)), _task("near", (1.0, 0.0)))
    pull = DispatchObservation(0.0, "pull", agents, tasks)
    assert _pairs(NearestTaskPolicy().decide(pull)) == [("a1", "near"), ("a2", "far")]
    push = DispatchObservation(0.0, "push", agents, tasks)
    assert _pairs(NearestTaskPolicy().decide(push)) == [("a2", "far"), ("a1", "near")]


def test_FR_AGT_02_zone_affinity_with_and_without_fallback() -> None:
    agents = (AgentView("a1", "x", attributes={"zones": ["z2"]}),)
    tasks = (_task("t1", zone="z1"), _task("t2", zone="z2"))
    obs = DispatchObservation(0.0, "pull", agents, tasks)
    assert _pairs(ZoneAffinityPolicy().decide(obs)) == [("a1", "t2")]
    only_z1 = DispatchObservation(0.0, "pull", agents, (_task("t1", zone="z1"),))
    assert _pairs(ZoneAffinityPolicy().decide(only_z1)) == [("a1", "t1")]
    assert _pairs(ZoneAffinityPolicy(fallback=False).decide(only_z1)) == []


def test_FR_POL_01_policies_declare_observation_and_action_schemas() -> None:
    for policy in (FifoPolicy, NearestTaskPolicy, ZoneAffinityPolicy):
        obs = policy.observation_schema()
        act = policy.action_schema()
        assert "agents" in obs["properties"] and "tasks" in obs["properties"]
        assert "assignments" in act["properties"]
    assert FifoPolicy.observation_schema() == ZoneAffinityPolicy.observation_schema()  # FR-POL-05


def test_make_policy_unknown_lists_available() -> None:
    with pytest.raises(PolicyError, match="available"):
        make_policy("magic", {})

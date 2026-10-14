"""Plans, solver callbacks and Redis-backed runs (FR-DSP-05, FR-POL-02, AC-DSP-01/02)."""

from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from simcore.api import run_scenario
from simcore.core.config import ConfigError, load_scenario
from simcore.core.obs.eventlog import EventType
from simcore.core.plugins import PluginRegistry
from simcore.core.policy import (
    AgentView,
    DispatchObservation,
    PolicyError,
    TaskView,
    assignment_min_distance,
    load_plan,
)
from simcore.experiment.evaluator import Evaluator
from simcore.specs import PlanSpec
from tests.conftest import EXAMPLES, MMC, WAREHOUSE


def _first_orders(n: int) -> list[dict[str, str]]:
    with (EXAMPLES / "data" / "warehouse_orders.csv").open() as fh:
        return [row for _, row in zip(range(n), csv.DictReader(fh))]


def _plan(n_tasks: int = 24, agents: int = 12, **extra: Any) -> dict[str, Any]:
    """A trivial 'external optimizer': round-robin the first orders over the fleet."""
    rows = _first_orders(n_tasks)
    seqs: dict[str, list[dict[str, Any]]] = {
        f"pallet_jack_operator-{i:03d}": [] for i in range(agents)
    }
    for i, row in enumerate(rows):
        seqs[f"pallet_jack_operator-{i % agents:03d}"].append(
            {"task": row["order_id"], "planned_start": float(row["release_s"])}
        )
    return {
        "id": "rr_plan",
        "source": "test-optimizer",
        "agents": [{"agent": a, "sequence": s} for a, s in seqs.items()],
        "expected_kpis": {"tasks_completed": 606.0, "cycle_time_mean": 400.0},
        **extra,
    }


def test_AC_DSP_02_external_plan_executed_and_compared(tmp_path: Path) -> None:
    plan_file = tmp_path / "plan.yaml"
    plan_file.write_text(yaml.safe_dump(_plan()))
    doc = yaml.safe_load(WAREHOUSE.read_text())
    doc["id"] = "planned"
    doc["policy"] = {"name": "plan_follower", "params": {"plan": "plan.yaml"}}
    for key in ("facility", "behaviors"):
        doc[key] = str((WAREHOUSE.parent / doc[key]).resolve())
    doc["demand"][0]["path"] = str((WAREHOUSE.parent / doc["demand"][0]["path"]).resolve())
    (tmp_path / "s.yaml").write_text(yaml.safe_dump(doc))
    result = run_scenario(tmp_path / "s.yaml")
    k = result.kpis
    assert k["plan_tasks_planned"] == 24 and k["plan_tasks_executed"] == 24
    assert k["plan_on_agent_rate"] == 1.0 and k["plan_sequence_adherence"] == 1.0
    assert k["plan_tasks_unplanned"] == 606 - 24  # the rest went through the fifo fallback
    assert k["plan_start_deviation_mean"] >= 0
    report = result.meta["policy_report"]
    assert report["plan_id"] == "rr_plan"
    row = report["kpis"]["tasks_completed"]
    assert row["expected"] == 606.0 and row["realized"] == k["tasks_completed"]
    assert row["delta"] == pytest.approx(k["tasks_completed"] - 606.0)
    owner = {t.task: t.agent for t in result.events.of_type(EventType.TASK_ASSIGNED)}
    assert owner["ORD00001"] == "pallet_jack_operator-000"
    assert owner["ORD00013"] == "pallet_jack_operator-000"  # second in its sequence


def test_plan_without_fallback_leaves_unplanned_work() -> None:
    bundle = load_scenario(WAREHOUSE).with_overrides(
        {
            "policy.name": "plan_follower",
            "policy.params": {"plan": _plan(n_tasks=6, agents=2, unplanned="none")},
        }
    )
    r = run_scenario(bundle)
    assert r.kpis["tasks_completed"] == 6 and r.kpis["plan_tasks_unplanned"] == 0


def test_plan_validation_errors() -> None:
    with pytest.raises(Exception, match="planned more than once"):
        PlanSpec.model_validate(
            {
                "id": "p",
                "agents": [
                    {"agent": "a", "sequence": [{"task": "t"}]},
                    {"agent": "b", "sequence": [{"task": "t"}]},
                ],
            }
        )
    with pytest.raises(PolicyError, match="not found"):
        load_plan("/nope/plan.yaml")
    bundle = load_scenario(WAREHOUSE).with_overrides({"policy.name": "plan_follower"})
    with pytest.raises(PolicyError, match=r"needs params\.plan"):
        run_scenario(bundle)


def test_FR_POL_02_assignment_solver_minimizes_total_distance() -> None:
    agents = (AgentView("a", "x", (0.0, 0.0)), AgentView("b", "x", (10.0, 0.0)))
    tasks = (
        TaskView("t_far_from_a", "t", 0, 0.0, origin_position=(9.0, 0.0)),
        TaskView("t_near_a", "t", 0, 0.0, origin_position=(2.0, 0.0)),
    )
    pairs = assignment_min_distance(DispatchObservation(0.0, "pull", agents, tasks), {})
    assert sorted(pairs) == [("a", "t_near_a"), ("b", "t_far_from_a")]


def test_FR_POL_02_solver_callback_policy_in_a_run() -> None:
    bundle = load_scenario(WAREHOUSE).with_overrides({"policy.name": "solver_callback"})
    r = run_scenario(bundle)
    assert r.kpis["tasks_completed"] == 606
    pol = {e.data["policy"] for e in r.events.of_type(EventType.POLICY_DECISION)}
    assert pol == {"solver_callback"}
    bad = load_scenario(WAREHOUSE).with_overrides(
        {"policy.name": "solver_callback", "policy.params": {"callback": "nope:fn"}}
    )
    with pytest.raises(PolicyError, match="cannot load solver callback"):
        run_scenario(bad)


def test_FR_POL_05_policies_compared_with_crn(plugins: PluginRegistry) -> None:
    ev = Evaluator(WAREHOUSE, plugins)
    a = ev.evaluate({"policy.name": "fifo"}, replications=2, seed=1)
    b = ev.evaluate({"policy.name": "solver_callback"}, replications=2, seed=1)
    assert a.kpis["cycle_time_mean"].mean != b.kpis["cycle_time_mean"].mean


def test_FR_IFC_03_evaluator_accepts_plans_and_caches_them(plugins: PluginRegistry) -> None:
    ev = Evaluator(WAREHOUSE, plugins)
    first = ev.evaluate(plan=_plan(n_tasks=12), replications=2, seed=3)
    again = ev.evaluate(plan=PlanSpec.model_validate(_plan(n_tasks=12)), replications=2, seed=3)
    assert not first.cached and again.cached
    assert first.kpis["plan_on_agent_rate"].mean == 1.0
    other = ev.evaluate(plan=_plan(n_tasks=6), replications=2, seed=3)
    assert not other.cached


@pytest.mark.redis
def test_AC_DSP_01_redis_queue_run_matches_in_memory() -> None:
    url = os.environ["SIMCORE_REDIS_URL"]
    mem = run_scenario(MMC, seed=2)
    red = run_scenario(MMC, seed=2, queue=url)
    assert mem.events.to_table().equals(red.events.to_table())


def test_unknown_queue_url_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown queue"):
        run_scenario(MMC, queue="kafka://x")


def test_scenario_config_error_for_bad_policy_param_type() -> None:
    with pytest.raises(ConfigError):
        load_scenario(MMC).with_overrides({"policy.assignment": "sideways"})

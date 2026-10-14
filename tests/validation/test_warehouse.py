"""Warehouse reference domain in logical mode (BRD 9, M3; AC-ENG-01/02 on the reference)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from simcore.api import run_scenario
from simcore.cli.main import cli
from simcore.core.config import ConfigError, load_scenario
from simcore.core.obs.eventlog import EventType
from tests.conftest import EXAMPLES, WAREHOUSE

WH = EXAMPLES / "scenarios" / "warehouse"


@pytest.fixture(scope="module")
def baseline():  # type: ignore[no-untyped-def]
    return run_scenario(WAREHOUSE)


def test_AC_ENG_01_reference_warehouse_run_via_cli(tmp_path: Path) -> None:
    res = CliRunner().invoke(cli, ["run", str(WAREHOUSE), "--out", str(tmp_path)])
    assert res.exit_code == 0, res.output
    rep = tmp_path / "adhoc" / "wh_baseline" / "rep_000"
    meta = json.loads((rep / "meta.json").read_text())
    assert meta["facility"] == {"id": "dc_outbound", "version": "0.1.0"}
    assert set(meta["config_hashes"]) == {"scenario", "behaviors", "facility"}
    assert (rep / "events.parquet").stat().st_size > 0


def test_AC_ENG_02_reference_warehouse_byte_identical(tmp_path: Path) -> None:
    run_scenario(WAREHOUSE, seed=4, out=tmp_path / "a")
    run_scenario(WAREHOUSE, seed=4, out=tmp_path / "b")
    a, b = (tmp_path / "a" / "events.parquet"), (tmp_path / "b" / "events.parquet")
    assert a.read_bytes() == b.read_bytes()


def test_NFR_02_logical_mode_faster_than_500x(baseline) -> None:  # type: ignore[no-untyped-def]
    assert baseline.meta["real_time_factor"] > 500


def test_baseline_completes_the_shift_with_domain_kpis(baseline) -> None:  # type: ignore[no-untyped-def]
    k = baseline.kpis
    assert k["tasks_completed"] == k["tasks_released"] == 606
    for key in (
        "pallets_per_labor_hour",
        "travel_distance_per_pallet",
        "bay_colocation_time",
        "dock_on_time_rate",
        "blocked_time_per_agent",
        "edge_utilization_max",
    ):
        assert key in k, key
    assert 0 < k["dock_on_time_rate"] <= 1 and k["pallets_moved"] > 606
    # triple pallet jack: profiles differ loaded vs empty
    speeds = {
        m.data["free_speed"]
        for m in baseline.events.of_type(EventType.AGENT_MOVE)
        if m.agent == "pallet_jack_operator-000"
    }
    assert len(speeds) >= 2


def test_narrow_aisles_create_geometric_congestion(baseline) -> None:  # type: ignore[no-untyped-def]
    narrow = run_scenario(WH / "narrow_aisles.yaml")
    assert narrow.kpis["blocked_time_opposing"] > 0 and baseline.kpis["blocked_time_opposing"] == 0
    assert narrow.kpis["cycle_time_mean"] > baseline.kpis["cycle_time_mean"]
    assert narrow.kpis["deadlocks_unresolved"] == 0


def test_BRD9_disruptions_have_measurable_effects(baseline) -> None:  # type: ignore[no-untyped-def]
    r = run_scenario(WH / "disruptions.yaml")
    k = r.kpis
    assert k["state_share_unavailable"] > 0
    starts = {d.data["disruption"] for d in r.events.of_type(EventType.DISRUPTION_START)}
    assert starts == {"aisle_02_blocked", "labor_shortfall", "door_1_closed"}
    assert (
        k["blocked_time_no_route"] + k["blocked_time_space"] > baseline.kpis["blocked_time_space"]
    )
    assert k["cycle_time_mean"] > baseline.kpis["cycle_time_mean"]


def test_BRD9_speed_retiming_slows_targeted_aisles_only() -> None:
    r = run_scenario(WH / "speed_retiming.yaml")
    base = run_scenario(WAREHOUSE)

    def speeds(run, zone_prefix: str, before: float):  # type: ignore[no-untyped-def]
        return [
            m.data["free_speed"]
            for m in run.events.of_type(EventType.AGENT_MOVE)
            if (m.location or "").startswith(zone_prefix) and m.data["entry"] < before
        ]

    assert max(speeds(r, "e_c01", 3600)) < max(speeds(base, "e_c01", 3600))
    assert max(speeds(r, "e_c04", 3600)) == pytest.approx(max(speeds(base, "e_c04", 3600)))


def test_stochastic_demand_with_surge() -> None:
    r = run_scenario(WH / "stochastic.yaml")
    times = [e.time for e in r.events.of_type(EventType.TASK_RELEASED)]
    in_surge = sum(1 for t in times if 7200 <= t < 9000)
    before = sum(1 for t in times if 5400 <= t < 7200)
    assert in_surge > before


def test_triple_pallet_capacity_and_selector_capabilities(tmp_path: Path) -> None:
    csv = tmp_path / "orders.csv"
    csv.write_text(
        "id,release_time,origin,destination,quantity,requires\n"
        "big,0,pick_face_c01_01,staging_lane_01,4,pallet_move\n"
    )
    doc = (WAREHOUSE).read_text().replace("../../data/warehouse_orders.csv", str(csv))
    doc = doc.replace(
        "      id: order_id\n      release_time: release_s\n      due_time: due_s\n"
        "      origin: pick_face\n      destination: staging_lane\n"
        "      quantity: pallets\n",
        "      {}\n",
    )
    for name in ("behaviors.yaml",):
        (tmp_path / name).write_text((WH / name).read_text())
    doc = doc.replace("../../facilities/", str(EXAMPLES / "facilities") + "/")
    (tmp_path / "s.yaml").write_text(doc)
    with pytest.raises(Exception, match="carries 3"):
        run_scenario(tmp_path / "s.yaml")


def test_warehouse_needs_a_facility(tmp_path: Path) -> None:
    (tmp_path / "behaviors.yaml").write_text((WH / "behaviors.yaml").read_text())
    (tmp_path / "s.yaml").write_text(
        "id: s\ndomain: warehouse\nbehaviors: behaviors.yaml\nduration: 60\n"
        "fleet: [{agent_type: selector, count: 1}]\n"
    )
    with pytest.raises(Exception, match="needs a facility"):
        run_scenario(tmp_path / "s.yaml")


def test_unknown_place_in_demand_is_a_config_error(tmp_path: Path) -> None:
    from simcore.api import default_plugins

    doc = (WH / "stochastic.yaml").read_text().replace("pick_face_c06_11", "pick_face_c99_99")
    (tmp_path / "bad.yaml").write_text(
        doc.replace("extends: baseline.yaml", f"extends: {WAREHOUSE}")
    )
    bundle = load_scenario(tmp_path / "bad.yaml")
    with pytest.raises(ConfigError, match="pick_face_c99_99"):
        default_plugins().get("warehouse").validate_scenario(bundle)

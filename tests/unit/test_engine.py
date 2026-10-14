"""Engine lifecycle, determinism, outputs (FR-ENG-01/02, FR-OBS-01/06, AC-ENG-01/02)."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from click.testing import CliRunner

from simcore.api import default_engine, run_scenario
from simcore.cli.main import cli
from simcore.core.engine import EngineStateError
from simcore.core.obs.eventlog import EventLog, EventType
from tests.conftest import MMC, WAREHOUSE_STUB


def test_FR_ENG_01_lifecycle_order_enforced() -> None:
    engine = default_engine()
    with pytest.raises(EngineStateError, match="lifecycle"):
        engine.run()
    engine.load_config(MMC)
    with pytest.raises(EngineStateError):
        engine.reset(1)
    engine.build_world().reset(1).run(until=50.0)
    engine.run()  # continue to the scenario duration
    result = engine.collect()
    assert result.meta["sim_time"]["end"] == 2200.0
    engine.teardown()
    with pytest.raises(EngineStateError):
        engine.collect()


def test_AC_ENG_01_cli_run_emits_event_log_kpis_and_metadata(tmp_path: Path) -> None:
    res = CliRunner().invoke(cli, ["run", str(MMC), "--out", str(tmp_path), "--jsonl"])
    assert res.exit_code == 0, res.output
    rep = tmp_path / "adhoc" / "mmc_baseline" / "rep_000"
    assert {p.name for p in rep.iterdir()} == {
        "events.parquet",
        "events.jsonl",
        "kpis.json",
        "meta.json",
    }
    kpis = json.loads((rep / "kpis.json").read_text())
    assert kpis["tasks_completed"] > 4000 and 0.7 < kpis["utilization"] < 0.9
    table = pq.read_table(rep / "events.parquet")
    assert table.column_names == ["seq", "time", "type", "agent", "task", "location", "data"]
    assert table.num_rows == len((rep / "events.jsonl").read_text().splitlines())


def test_FR_OBS_06_metadata_manifest() -> None:
    meta = run_scenario(MMC).meta
    for key in (
        "engine_version",
        "git_sha",
        "config_hashes",
        "config_hash",
        "seeds",
        "mode",
        "backend",
        "versions",
        "host",
        "wall_time_s",
        "real_time_factor",
        "domain",
    ):
        assert key in meta, key
    assert meta["seeds"]["master_seed"] == 42 and meta["mode"] == "logical"
    assert set(meta["config_hashes"]) == {"scenario", "behaviors"}


def test_AC_ENG_02_same_seed_byte_identical_event_logs(tmp_path: Path) -> None:
    a = run_scenario(MMC, seed=5, out=tmp_path / "a", jsonl=True)
    b = run_scenario(MMC, seed=5, out=tmp_path / "b", jsonl=True)
    for name in ("events.parquet", "events.jsonl", "kpis.json"):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes(), name
    assert a.meta["event_log_sha256"] == b.meta["event_log_sha256"]
    c = run_scenario(MMC, seed=6, out=tmp_path / "c")
    assert c.meta["event_log_sha256"] != a.meta["event_log_sha256"]


def test_AC_ENG_02_reused_engine_matches_fresh_engine(tmp_path: Path) -> None:
    engine = default_engine()
    engine.load_config(MMC).build_world()
    engine.run_replication(seed=1, replication_id=0)
    reused = engine.run_replication(seed=3, replication_id=2)
    fresh = run_scenario(MMC, seed=3, replication=2)
    assert reused.events.to_table().equals(fresh.events.to_table())


def test_FR_ENG_02_fixed_step_matches_event_driven() -> None:
    engine = default_engine()
    engine.load_config(MMC).build_world()
    stepped = engine.reset(9).run(step=7.5).collect()
    driven = engine.reset(9).run().collect()
    assert stepped.events.to_table().equals(driven.events.to_table())
    assert stepped.meta["real_time_factor"] > 1


def test_FR_OBS_04_event_log_round_trips_through_parquet(tmp_path: Path) -> None:
    result = run_scenario(MMC, out=tmp_path)
    reloaded = EventLog.from_parquet(tmp_path / "events.parquet")
    assert reloaded.to_table().equals(result.events.to_table())


def test_FR_OBS_01_event_log_contains_lifecycle_types() -> None:
    types = {r.type for r in run_scenario(WAREHOUSE_STUB).events}
    for t in (
        EventType.SIM_START,
        EventType.SIM_END,
        EventType.TASK_RELEASED,
        EventType.TASK_ASSIGNED,
        EventType.TASK_STARTED,
        EventType.TASK_COMPLETED,
        EventType.AGENT_STATE,
        EventType.POLICY_DECISION,
        EventType.DISRUPTION_START,
        EventType.DISRUPTION_END,
    ):
        assert t in types, t


def test_cli_validate_reports_errors(tmp_path: Path) -> None:
    bad = tmp_path / "b.yaml"
    bad.write_text("id: b\ndomain: toy_queue\nduration: -5\n")
    res = CliRunner().invoke(cli, ["validate", str(bad)])
    assert res.exit_code == 1 and "duration" in res.output
    ok = CliRunner().invoke(cli, ["validate", str(MMC)])
    assert ok.exit_code == 0 and "valid scenario" in ok.output


def test_cli_replay_summarizes(tmp_path: Path) -> None:
    run_scenario(MMC, out=tmp_path)
    res = CliRunner().invoke(cli, ["replay", str(tmp_path / "events.parquet"), "--at", "100"])
    assert res.exit_code == 0 and "task.completed" in res.output and "server-000" in res.output

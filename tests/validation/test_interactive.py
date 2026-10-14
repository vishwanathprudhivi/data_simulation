"""Interactive / demo mode (M13): AC-IFC-02, FR-IFC-05, FR-SCN-06."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from simcore.api import default_plugins, run_scenario
from simcore.cli.main import cli
from simcore.core.config import load_scenario
from simcore.core.obs.eventlog import EventType
from simcore.interactive import InteractiveSession
from tests.conftest import EXAMPLES, MMC

DEMO = EXAMPLES / "scenarios" / "warehouse" / "demo.yaml"


@pytest.fixture(scope="module")
def plugins() -> Any:
    return default_plugins()


def test_AC_IFC_02_scripted_demo_with_live_injection_and_hud(plugins: Any) -> None:
    bundle = load_scenario(DEMO)
    assert bundle.facility is not None and bundle.facility.id == "dc_outbound"  # the reference DC
    sess = InteractiveSession(bundle, plugins, seed=3)
    sess.play()
    huds = []
    while sess.playing:
        sess.tick(10.0)  # 10 wall-seconds at the beat's speed
        huds.append(dict(sess.hud()))
        if 2400 < sess.now < 2400 + 1500:
            assert "aisle_02_drop" in sess.active_disruptions()
    assert sess.captions[-1].beat == "wrap_up" and not sess.finished  # a beat paused it
    names = [c.beat for c in sess.captions]
    assert names == ["intro", "steady_state", "blockage", "shortage", "wrap_up"]
    assert [i.spec.id for i in sess.injections] == ["aisle_02_drop", "pulled_to_receiving"]
    assert sess.speed == 120  # set by the intro beat
    log = sess.engine.ctx.log  # type: ignore[union-attr]
    started = [r.data["disruption"] for r in log.of_type(EventType.DISRUPTION_START)]
    assert "aisle_02_drop" in started and "pulled_to_receiving" in started
    # the HUD shows domain metrics first and moves with the run
    assert list(huds[-1])[:2] == ["pallets_per_labor_hour", "travel_distance_per_pallet"]
    assert huds[-1]["tasks_completed"] > huds[1]["tasks_completed"] > 0
    assert len(sess.positions()) == 12
    sess.play()
    sess.tick(1000)
    assert sess.finished and not sess.playing


def test_FR_IFC_05_live_injection_is_reproducible_in_batch(plugins: Any) -> None:
    bundle = load_scenario(MMC, {"duration": 1800, "warmup": 0})
    sess = InteractiveSession(bundle, plugins, seed=11)
    sess.step(400)
    sess.inject({"id": "server_down", "type": "resource_unavailable", "target": "type:server",
                 "duration": 300, "params": {"count": 1}})  # fmt: skip
    sess.set_speed(50)
    sess.play()
    while not sess.finished:
        sess.tick(1.0)
    live = sess.result().kpis
    batch = run_scenario(sess.recorded_scenario(), seed=11, plugins=plugins).kpis
    assert live == batch  # wall-clock pacing never changes the simulation
    plain = run_scenario(bundle, seed=11, plugins=plugins).kpis
    assert plain["wait_time_mean"] != live["wait_time_mean"]
    sess.close()


def test_FR_IFC_05_controls(plugins: Any) -> None:
    sess = InteractiveSession(load_scenario(MMC), plugins, seed=1, speed=10)
    assert sess.tick(5) == 0  # paused: wall time does not move the clock
    assert sess.step(30) == 30
    sess.play()
    assert sess.tick(2) == 50
    sess.pause()
    assert sess.tick(100) == 50
    with pytest.raises(ValueError):
        sess.set_speed(0)
    sess.inject({"id": "x", "type": "demand_surge", "duration": 10, "params": {"factor": 2}})
    with pytest.raises(ValueError, match="already exists"):
        sess.inject({"id": "x", "type": "demand_surge", "duration": 10})
    with pytest.raises(RuntimeError):
        sess.result()
    state = sess.state()
    assert state["t"] == 50 and state["injections"][0]["source"] == "operator"


def test_cli_interactive_headless_hud_and_record(tmp_path: Path) -> None:
    rec = tmp_path / "recorded.yaml"
    res = CliRunner().invoke(
        cli,
        ["interactive", str(DEMO), "--seed", "2", "--hud-every", "3600", "--inject",
         "at=900,id=op_out,type=resource_unavailable,target=type:pallet_jack_operator,"
         "duration=600,count=2", "--record", str(rec)],
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    out = res.output
    assert "intro: Twelve pallet-jack operators" in out and "INJECT op_out" in out
    assert "INJECT aisle_02_drop (location_blocked)" in out and "HUD " in out
    doc = yaml.safe_load(rec.read_text())
    assert [d["id"] for d in doc["disruptions"]] == [
        "op_out",
        "aisle_02_drop",
        "pulled_to_receiving",
    ]
    assert doc["beats"] == []

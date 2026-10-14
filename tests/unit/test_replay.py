"""FR-OBS-04 replay: trajectories rebuilt from the event log match the live run."""

from __future__ import annotations

import pytest

from simcore.api import default_engine
from simcore.core.facility.graph import NavGraph
from simcore.core.obs.eventlog import EventLog
from simcore.core.obs.replay import snapshot, trajectories_from_log
from tests.conftest import EXAMPLES


@pytest.mark.parametrize("scenario", ["baseline.yaml", "narrow_aisles.yaml"])
def test_FR_OBS_04_replayed_positions_match_live_backend(scenario: str, tmp_path) -> None:  # type: ignore[no-untyped-def]
    engine = default_engine()
    engine.load_config(EXAMPLES / "scenarios" / "warehouse" / scenario).build_world()
    engine.reset(5, 0).run(until=3600.0)
    live = engine.ctx.backend  # type: ignore[union-attr]
    log = engine.ctx.log  # type: ignore[union-attr]
    log.write_parquet(tmp_path / "events.parquet")
    nav = NavGraph(engine.bundle.facility)  # type: ignore[union-attr,arg-type]
    store = trajectories_from_log(EventLog.from_parquet(tmp_path / "events.parquet"), nav)
    agents = sorted(store.spawn)
    for t in [0.0, 17.3, 250.0, 901.7, 1800.0, 2999.9]:
        for a in agents:
            assert store.position(a, t) == pytest.approx(live.position(a, t)), (a, t)
    snap = snapshot(log, nav, 1800.0, store)
    assert set(snap) == set(agents) and all("state" in v for v in snap.values())

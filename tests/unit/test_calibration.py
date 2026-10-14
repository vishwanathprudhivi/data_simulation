"""FR-INT-05: calibration recovers an interaction parameter from target KPIs."""

from __future__ import annotations

import pytest

from simcore.core.plugins import PluginRegistry
from simcore.experiment.calibration import calibrate
from simcore.experiment.evaluator import Evaluator
from tests.support.courier import CourierPlugin, bundle, corridor

FAC = corridor({"W": (0, 0), "E": (40, 0)}, [("aisle", "W", "E", 2.0)])


def _bundle():  # type: ignore[no-untyped-def]
    tasks = [
        {
            "id": f"t{i}",
            "origin": "at_W" if i % 2 else "at_E",
            "destination": "at_E" if i % 2 else "at_W",
            "release_time": 3.0 * i,
        }
        for i in range(12)
    ]
    return bundle(
        FAC, [{"id": "w", "speed": 1.2, "count": 4, "start": ["at_W", "at_E"]}], tasks, duration=400
    )


def test_FR_INT_05_grid_calibration_recovers_parameter() -> None:
    plugins = PluginRegistry([CourierPlugin()])
    ev = Evaluator(_bundle(), plugins)
    truth = ev.evaluate({"interaction.passing_speed_factor": 0.5}, replications=1)
    targets = {"cycle_time_mean": truth.mean("cycle_time_mean")}
    result = calibrate(
        ev, {"interaction.passing_speed_factor": [0.3, 0.5, 0.7, 0.9]}, targets, replications=1
    )
    assert result.best_params == {"interaction.passing_speed_factor": 0.5}
    assert result.best_score == pytest.approx(0.0)
    assert len(result.trace) == 4 and all("errors" in t for t in result.trace)
    with pytest.raises(ValueError, match="empty"):
        calibrate(ev, {}, targets)

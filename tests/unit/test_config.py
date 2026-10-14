"""Config loading: actionable errors, inheritance, overrides (FR-ENG-08, FR-SCN-05)."""

from __future__ import annotations

from pathlib import Path

import pytest

from simcore.core.config import (
    ConfigError,
    apply_overrides,
    deep_merge,
    load_experiment,
    load_scenario,
)
from tests.conftest import EXAMPLES, MMC


def _write(tmp: Path, name: str, text: str) -> Path:
    p = tmp / name
    p.write_text(text)
    return p


def test_FR_ENG_08_errors_say_what_where_and_how(tmp_path: Path) -> None:
    (tmp_path / "behaviors.yaml").write_text((MMC.parent / "behaviors.yaml").read_text())
    bad = _write(
        tmp_path,
        "bad.yaml",
        "id: bad\ndomain: toy_queue\nbehaviors: behaviors.yaml\nduration: 10\n"
        "fleet:\n  - {agent_type: server, count: -1}\n  - {agent_type: robot, count: 1}\n"
        "policy: {name: fifo, assignmnt: pull}\n",
    )
    with pytest.raises(ConfigError) as info:
        load_scenario(bad)
    text = str(info.value)
    assert "bad.yaml at 'fleet.0.count'" in text and "greater than or equal to 0" in text
    assert "policy.assignmnt" in text and "check its spelling" in text


def test_FR_ENG_08_unknown_agent_type_points_to_behaviors(tmp_path: Path) -> None:
    (tmp_path / "behaviors.yaml").write_text((MMC.parent / "behaviors.yaml").read_text())
    bad = _write(
        tmp_path,
        "s.yaml",
        "id: s\ndomain: toy_queue\nbehaviors: behaviors.yaml\nduration: 10\n"
        "fleet: [{agent_type: robot, count: 1}]\n",
    )
    with pytest.raises(ConfigError, match=r"fleet\.0\.agent_type.*'robot' is not defined"):
        load_scenario(bad)


def test_FR_ENG_08_yaml_syntax_and_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="line 2"):
        load_scenario(_write(tmp_path, "x.yaml", "id: x\n  bad: [\n"))
    with pytest.raises(ConfigError, match="file not found"):
        load_scenario(tmp_path / "nope.yaml")


def test_FR_SCN_05_extends_overrides_fields() -> None:
    child = load_scenario(EXAMPLES / "scenarios/toy_queue/mmc_four_servers.yaml")
    base = load_scenario(MMC)
    assert child.scenario.fleet[0].count == 4 and base.scenario.fleet[0].count == 3
    assert child.scenario.duration == base.scenario.duration
    assert child.scenario.demand == base.scenario.demand
    assert child.scenario.extends is not None


def test_FR_SCN_05_extends_cycle_detected(tmp_path: Path) -> None:
    _write(tmp_path, "a.yaml", "extends: b.yaml\nid: a\n")
    _write(tmp_path, "b.yaml", "extends: a.yaml\nid: b\n")
    with pytest.raises(ConfigError, match="inheritance cycle"):
        load_scenario(tmp_path / "a.yaml")


def test_deep_merge_keyed_lists() -> None:
    base = {"fleet": [{"agent_type": "a", "count": 1}, {"agent_type": "b", "count": 2}]}
    out = deep_merge(
        base, {"fleet": [{"agent_type": "b", "count": 5}, {"agent_type": "c", "count": 1}]}
    )
    assert out["fleet"] == [
        {"agent_type": "a", "count": 1},
        {"agent_type": "b", "count": 5},
        {"agent_type": "c", "count": 1},
    ]
    assert deep_merge({"x": [1, 2]}, {"x": [3]}) == {"x": [3]}


def test_apply_overrides_paths() -> None:
    doc = {"fleet": [{"agent_type": "server", "count": 3}], "params": {}}
    out = apply_overrides(doc, {"fleet.server.count": 5, "params.rate": 2, "fleet.0.extra": 1})
    assert out["fleet"][0] == {"agent_type": "server", "count": 5, "extra": 1}
    assert out["params"] == {"rate": 2}
    assert doc["fleet"][0]["count"] == 3  # input untouched
    with pytest.raises(ValueError, match="no list item keyed 'robot'"):
        apply_overrides(doc, {"fleet.robot.count": 1})


def test_bundle_overrides_revalidate() -> None:
    bundle = load_scenario(MMC)
    changed = bundle.with_overrides({"fleet.server.count": 5})
    assert changed.scenario.fleet[0].count == 5
    assert changed.config_hash != bundle.config_hash
    with pytest.raises(ConfigError, match="greater than or equal to 0"):
        bundle.with_overrides({"fleet.server.count": -2})


def test_FR_FAC_07_config_hash_is_location_independent(tmp_path: Path) -> None:
    for name in ("mmc_baseline.yaml", "behaviors.yaml"):
        (tmp_path / name).write_text((MMC.parent / name).read_text())
    assert load_scenario(tmp_path / "mmc_baseline.yaml").hashes == load_scenario(MMC).hashes


def test_load_experiment_resolves_base_relative_to_file() -> None:
    spec, bundle = load_experiment(EXAMPLES / "experiments/mmc_servers.yaml")
    assert spec.factors[0].levels == [3, 4, 5]
    assert bundle.scenario.id == "mmc_baseline"

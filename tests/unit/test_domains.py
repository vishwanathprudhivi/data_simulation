"""Domain definitions, catalogs, levers and the Tier-0 process network (M6).

FR-DOM-01/02/03/10, FR-SEM-01/02, AC-DOM-03 (backend), AC-SEM-01 (catalog part).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from simcore.api import default_plugins, run_scenario
from simcore.catalog import apply_levers, capability_manifest, lever_catalog, metric_catalog
from simcore.cli.main import cli
from simcore.core.config import ConfigError, load_scenario
from simcore.core.obs.eventlog import EventType
from simcore.core.plugins import PluginError, PluginRegistry
from tests.conftest import EXAMPLES, MMC, ROOT, WAREHOUSE

PL = EXAMPLES / "scenarios" / "packaging_line" / "baseline.yaml"
PL_DOMAIN = ROOT / "domains" / "packaging_line" / "domain.yaml"


@pytest.fixture
def domain_dir(tmp_path: Path) -> Path:
    """A private copy of the packaging line domain on its own search path."""
    target = tmp_path / "domains" / "packaging_line"
    target.mkdir(parents=True)
    shutil.copy(PL_DOMAIN, target / "domain.yaml")
    return tmp_path / "domains"


def _registry(search: Path) -> PluginRegistry:
    reg = PluginRegistry()
    reg.discover()
    reg.discover_tier0([search])
    return reg


def _edit(domain_dir: Path, fn: Any) -> None:
    path = domain_dir / "packaging_line" / "domain.yaml"
    doc = yaml.safe_load(path.read_text())
    fn(doc)
    path.write_text(yaml.safe_dump(doc, sort_keys=False))


def test_FR_DOM_01_tier0_domain_discovered_and_runs_without_python() -> None:
    reg = default_plugins()
    assert "packaging_line" in reg.names
    m = capability_manifest(reg.get("packaging_line"))
    assert m.tier == 0 and {"operators", "filler_speed_factor"} <= {lv.id for lv in m.levers}
    r = run_scenario(PL, plugins=reg)
    assert r.kpis["tasks_completed"] > 1500
    assert r.kpis["station_filler_utilization"] == r.kpis["bottleneck_utilization"]
    assert r.kpis["station_depal_blocked_share"] > 0  # finite filler buffer blocks upstream


def test_AC_DOM_03_lever_added_in_yaml_surfaces_without_code(domain_dir: Path) -> None:
    _edit(
        domain_dir,
        lambda d: d["levers"].append(
            {
                "id": "labeler_speed",
                "display_name": "Labeler cycle-time factor",
                "type": "float",
                "path": "params.stations.labeler.service_time_factor",
                "min": 0.5,
                "max": 1.5,
            }
        ),
    )
    reg = _registry(domain_dir)
    plugin = reg.get("packaging_line")
    assert "labeler_speed" in {lv.id for lv in lever_catalog(plugin).levers}
    assert "labeler_speed" in {lv.id for lv in capability_manifest(plugin).levers}
    app = apply_levers(load_scenario(PL), plugin, {"labeler_speed": 0.8})
    assert app.overrides == {"params.stations.labeler.service_time_factor": 0.8}
    assert app.bundle.scenario.params["stations"]["labeler"]["service_time_factor"] == 0.8


def test_AC_SEM_01_metric_added_in_yaml_appears_in_catalog(domain_dir: Path) -> None:
    _edit(
        domain_dir,
        lambda d: d["metrics"].append(
            {
                "id": "labeler_utilization",
                "display_name": "Labeler utilization",
                "unit": "1",
                "formula": "station_labeler_utilization",
                "allowed_dimensions": ["variant"],
            }
        ),
    )
    plugin = _registry(domain_dir).get("packaging_line")
    ids = {m.id for m in metric_catalog(plugin).metrics}
    assert "labeler_utilization" in ids and "throughput" in ids  # domain + core metrics
    res = CliRunner().invoke(cli, ["domains", "show", "warehouse"])
    assert res.exit_code == 0 and '"dock_on_time_rate"' in res.output


def test_FR_DOM_02_warehouse_publishes_domain_yaml() -> None:
    plugin = default_plugins().get("warehouse")
    d = plugin.domain_definition()
    assert d is not None and d.tier == 2 and d.version == plugin.version
    m = capability_manifest(plugin)
    assert "dock_door_closed" in m.disruption_types
    assert {"jacks", "dispatch_policy"} <= {lv.id for lv in m.levers}


def test_levers_enforce_types_bounds_and_envelope() -> None:
    plugin = default_plugins().get("warehouse")
    bundle = load_scenario(WAREHOUSE)
    ok = apply_levers(bundle, plugin, {"jacks": 20, "shift_length": "6 h"})
    assert ok.bundle.scenario.fleet[0].count == 20 and ok.bundle.scenario.duration == 21600
    assert not ok.extrapolated
    out = apply_levers(bundle, plugin, {"jacks": 40})
    assert out.extrapolated and out.outside_envelope["jacks"]["max"] == 30
    with pytest.raises(ConfigError) as info:
        apply_levers(
            bundle,
            plugin,
            {"jacks": 0, "dispatch_policy": "random", "wings": 1, "colocation_penalty": "lots"},
        )
    text = str(info.value)
    assert "below the minimum 1" in text and "must be one of" in text
    assert "'wings' is not a lever" in text and "available levers" in text
    assert "expects a number" in text


def test_FR_DOM_10_domain_version_compatibility(tmp_path: Path) -> None:
    bundle = load_scenario(MMC).with_overrides({"domain_version": "2.0.0"})
    with pytest.raises(ConfigError, match="major version differs"):
        default_plugins().get("toy_queue").validate_scenario(bundle)
    ok = load_scenario(MMC).with_overrides({"domain_version": "0.1.9"})
    default_plugins().get("toy_queue").validate_scenario(ok)


def test_domain_yaml_must_match_plugin_version(tmp_path: Path) -> None:
    from domains.toy_queue import ToyQueuePlugin

    class Drifted(ToyQueuePlugin):
        version = "9.9.9"

    module_dir = Path(__import__("domains.toy_queue", fromlist=["x"]).__file__).parent
    assert (module_dir / "domain.yaml").exists()
    Drifted.__module__ = "domains.toy_queue"
    with pytest.raises(PluginError, match="keep them in sync"):
        Drifted().domain_definition()


def test_broken_tier0_domain_is_reported_not_fatal(domain_dir: Path) -> None:
    _edit(domain_dir, lambda d: d["process"]["routings"][0]["steps"].append("teleporter"))
    reg = _registry(domain_dir)
    assert "packaging_line" not in reg.names and "warehouse" in reg.names
    assert any("teleporter" in e for e in reg.errors.values())


def _pl(**over: Any):  # type: ignore[no-untyped-def]
    return load_scenario(PL).with_overrides(over)


def test_process_network_validation_errors() -> None:
    plugin = default_plugins().get("packaging_line")
    with pytest.raises(ConfigError) as info:
        plugin.validate_scenario(
            _pl(
                **{
                    "demand.demand.task_type": "sku_z",
                    "params.stations.teleporter.capacity": 2,
                    "fleet.operator.count": 0,
                }
            )
        )
    text = str(info.value)
    assert "'sku_z' has no routing" in text and "unknown station 'teleporter'" in text
    assert "needs operators of type 'operator'" in text


def test_process_network_operators_and_disruptions() -> None:
    base = run_scenario(PL)
    fewer = run_scenario(_pl(**{"fleet.operator.count": 1}))
    assert fewer.kpis["tasks_completed"] < base.kpis["tasks_completed"]
    down = run_scenario(
        _pl(
            disruptions=[
                {
                    "id": "jam",
                    "type": "location_blocked",
                    "target": "capper",
                    "at": 3600,
                    "duration": 3600,
                }
            ]
        )
    )
    assert down.kpis["tasks_completed"] < base.kpis["tasks_completed"]
    more = run_scenario(
        _pl(
            disruptions=[
                {
                    "id": "cap",
                    "type": "capacity_change",
                    "target": "filler",
                    "at": 0,
                    "params": {"capacity": 2},
                }
            ]
        )
    )
    assert more.kpis["station_filler_queue_mean"] < base.kpis["station_filler_queue_mean"]
    absent = run_scenario(
        _pl(
            disruptions=[
                {
                    "id": "break",
                    "type": "resource_unavailable",
                    "target": "type:operator",
                    "at": 3600,
                    "duration": 1800,
                    "params": {"count": 3},
                }
            ]
        )
    )
    assert absent.kpis["state_share_unavailable"] > 0


def test_process_network_changeover_and_breakdowns() -> None:
    r = run_scenario(PL)
    changes = [
        s.data["changeover"]
        for s in r.events.of_type(EventType.SERVICE_START)
        if s.location == "filler" and s.data["changeover"] > 0
    ]
    assert changes == [300.0]  # one SKU switch at 4 h
    downs = [
        d for d in r.events.of_type(EventType.DISRUPTION_START) if d.data.get("type") == "breakdown"
    ]
    assert downs and r.kpis["station_filler_down_share"] > 0
    a = run_scenario(PL, seed=9)
    b = run_scenario(PL, seed=9)
    assert a.events.to_table().equals(b.events.to_table())  # deterministic


def test_tier0_scenario_runs_in_experiment(plugins: PluginRegistry) -> None:
    from simcore.experiment.evaluator import Evaluator

    ev = Evaluator(PL, plugins)
    fast = ev.evaluate({"params.stations.filler.service_time_factor": 0.85}, replications=3)
    slow = ev.evaluate({}, replications=3)
    assert fast.mean("station_filler_utilization") < slow.mean("station_filler_utilization")

"""Plug-in discovery and coexistence (FR-ENG-05, AC-ENG-03, BRD 7.2)."""

from __future__ import annotations

from pathlib import Path

import pytest
from domains.toy_queue import ToyQueuePlugin

from simcore.api import run_scenario
from simcore.core.config import ConfigError, load_scenario
from simcore.core.plugins import DomainPlugin, PluginError, PluginRegistry
from tests.conftest import MMC, WAREHOUSE


def test_FR_ENG_05_entry_point_discovery(plugins: PluginRegistry) -> None:
    assert {"toy_queue", "warehouse"} <= set(plugins.names)
    manifest = plugins.get("toy_queue").manifest()
    assert "toy_queue.system" in manifest.kpis
    assert "fifo" in manifest.policies
    assert manifest.params_schema is not None
    assert "dock_door_closed" in plugins.get("warehouse").manifest().scenario_event_types
    assert "dock_door_closed" not in manifest.scenario_event_types


def test_AC_ENG_03_two_domains_load_and_run_in_one_process(plugins: PluginRegistry) -> None:
    toy = run_scenario(MMC, plugins=plugins)
    wh = run_scenario(WAREHOUSE, plugins=plugins)
    toy_again = run_scenario(MMC, plugins=plugins)
    assert "number_in_system_mean" in toy.kpis and "number_in_system_mean" not in wh.kpis
    assert "pallets_per_labor_hour" in wh.kpis and "pallets_per_labor_hour" not in toy.kpis
    assert toy.kpis == toy_again.kpis  # running another domain in between changes nothing


def test_registry_rejects_name_clash_from_different_plugins() -> None:
    class Impostor(DomainPlugin):
        name = "toy_queue"
        version = "9"

        def create_model(self, bundle, params):  # type: ignore[no-untyped-def]
            raise NotImplementedError

    reg = PluginRegistry([ToyQueuePlugin()])
    reg.register(ToyQueuePlugin())  # same plug-in twice is fine
    with pytest.raises(PluginError, match="two different plug-ins"):
        reg.register(Impostor())


def test_unknown_domain_message_lists_installed(plugins: PluginRegistry) -> None:
    with pytest.raises(PluginError, match=r"installed:.*toy_queue"):
        plugins.get("brewery")


def test_load_by_module_path() -> None:
    reg = PluginRegistry()
    reg.load("domains.toy_queue:plugin")
    reg.load("domains.warehouse:WarehousePlugin")
    assert reg.names == ["toy_queue", "warehouse"]
    with pytest.raises(PluginError, match="cannot load"):
        reg.load("domains.nope:plugin")


def test_FR_ENG_08_domain_validation_of_params_policy_and_disruptions(
    tmp_path: Path, plugins: PluginRegistry
) -> None:
    (tmp_path / "behaviors.yaml").write_text((MMC.parent / "behaviors.yaml").read_text())
    p = tmp_path / "s.yaml"
    p.write_text(
        "id: s\ndomain: toy_queue\nbehaviors: behaviors.yaml\nduration: 10\n"
        "policy: {name: magic}\nparams: {colour: blue}\n"
        "disruptions: [{id: d, type: dock_door_closed, at: 1}]\n"
    )
    with pytest.raises(ConfigError) as info:
        plugins.get("toy_queue").validate_scenario(load_scenario(p))
    text = str(info.value)
    assert "unknown policy 'magic'" in text
    assert "'dock_door_closed' is not supported by domain 'toy_queue'" in text
    assert "params.colour" in text

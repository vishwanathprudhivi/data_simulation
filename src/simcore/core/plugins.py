"""Domain plug-in contract, registry and discovery (FR-ENG-05, BRD 7.2, AC-ENG-03).

A plug-in contributes entity types, policies, KPI calculators, scenario event
(disruption) types, a params schema and a model constructor. Everything a plug-in
registers is scoped to that plug-in, so unrelated domains coexist in one process.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import logging
import os
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import yaml
from pydantic import BaseModel

from simcore.core.config import ConfigError, ConfigIssue, ScenarioBundle, validate_spec
from simcore.core.obs.kpi import KpiCalculator
from simcore.core.policy.base import BUILTIN_POLICIES, PolicyFactory
from simcore.specs.domain import DomainDefinition
from simcore.specs.scenario import CORE_DISRUPTION_TYPES

if TYPE_CHECKING:
    from simcore.core.model import SimModel

__all__ = [
    "ENTRY_POINT_GROUP",
    "DomainPlugin",
    "PluginError",
    "PluginManifest",
    "PluginRegistry",
    "facility_issues",
]

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "simcore.domains"
TIER0_GROUP = "simcore.tier0"
DOMAIN_PATH_ENV = "SIMCORE_DOMAIN_PATH"


class PluginError(RuntimeError):
    pass


@dataclass(frozen=True)
class PluginManifest:
    name: str
    version: str
    description: str
    entity_types: tuple[str, ...]
    policies: tuple[str, ...]
    kpis: tuple[str, ...]
    scenario_event_types: tuple[str, ...]
    params_schema: Mapping[str, Any] | None
    has_domain_definition: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "entity_types": list(self.entity_types),
            "policies": list(self.policies),
            "kpis": list(self.kpis),
            "scenario_event_types": list(self.scenario_event_types),
            "params_schema": self.params_schema,
            "has_domain_definition": self.has_domain_definition,
        }


class DomainPlugin(ABC):
    """Base class for domain plug-ins. Override what the domain provides."""

    name: ClassVar[str]
    version: ClassVar[str]
    description: ClassVar[str] = ""

    def entity_types(self) -> Mapping[str, str]:
        """Entity type id -> description."""
        return {}

    def policies(self) -> Mapping[str, PolicyFactory]:
        """Domain policies (may shadow built-ins within this domain only)."""
        return {}

    def kpi_calculators(self) -> Sequence[KpiCalculator]:
        return ()

    def scenario_event_types(self) -> Sequence[str]:
        """Disruption types beyond the core-generic ones."""
        return ()

    def params_model(self) -> type[BaseModel] | None:
        """Pydantic model validating ``scenario.params`` (config schema extension)."""
        return None

    def asset_bindings(self) -> Mapping[str, str]:
        """Entity/agent type -> USD asset path (used by the omniverse adapter only)."""
        return {}

    def domain_definition(self) -> DomainDefinition | None:
        """Published ``domain.yaml`` (FR-DOM-02): by default the file next to the plug-in's
        module. Its ``id``/``version`` must match the plug-in's."""
        cached = self.__dict__.get("_definition_cache", False)
        if cached is not False:
            return cached  # type: ignore[no-any-return]
        definition = None
        path = Path(inspect.getfile(type(self))).parent / "domain.yaml"
        if path.exists():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            definition = validate_spec(DomainDefinition, data, str(path))
            if (definition.id, definition.version) != (self.name, self.version):
                raise PluginError(
                    f"{path}: declares {definition.id} {definition.version} but the plug-in is "
                    f"{self.name} {self.version}; keep them in sync (FR-DOM-10)"
                )
        self.__dict__["_definition_cache"] = definition
        return definition

    @abstractmethod
    def create_model(self, bundle: ScenarioBundle, params: BaseModel | None) -> SimModel:
        """Build the simulation model for one replication."""

    # ------------------------------------------------------------------ helpers

    def all_policies(self) -> dict[str, PolicyFactory]:
        return {**BUILTIN_POLICIES, **self.policies()}

    def disruption_types(self) -> tuple[str, ...]:
        return (*CORE_DISRUPTION_TYPES, *self.scenario_event_types())

    def validate_params(self, bundle: ScenarioBundle) -> BaseModel | None:
        model = self.params_model()
        if model is None:
            if bundle.scenario.params:
                raise ConfigError(
                    [
                        ConfigIssue(
                            bundle.source,
                            "params",
                            f"domain {self.name!r} accepts no params",
                            "remove 'params' from the scenario",
                        )
                    ]
                )
            return None
        try:
            return validate_spec(model, bundle.scenario.params, f"{bundle.source} (params)")
        except ConfigError as exc:
            raise ConfigError(
                [
                    ConfigIssue(i.source, f"params.{i.location}".rstrip("."), i.message, i.hint)
                    for i in exc.issues
                ]
            ) from exc

    def validate_scenario(self, bundle: ScenarioBundle) -> BaseModel | None:
        """Domain-level checks; returns validated params (FR-ENG-08)."""
        issues: list[ConfigIssue] = []
        wanted = bundle.scenario.domain_version
        if wanted is not None and wanted.split(".")[0] != self.version.split(".")[0]:
            issues.append(
                ConfigIssue(
                    bundle.source,
                    "domain_version",
                    f"scenario was written for {self.name} {wanted} but {self.version} is "
                    "installed (major version differs)",
                    "migrate the scenario (see the domain's migration notes) and update "
                    "domain_version",
                )
            )
        allowed = self.disruption_types()
        for n, d in enumerate(bundle.scenario.disruptions):
            if d.type not in allowed:
                issues.append(
                    ConfigIssue(
                        bundle.source,
                        f"disruptions.{n}.type",
                        f"disruption type {d.type!r} is not supported by domain {self.name!r}",
                        f"use one of {list(allowed)}",
                    )
                )
        policies = self.all_policies()
        if bundle.scenario.policy.name not in policies:
            issues.append(
                ConfigIssue(
                    bundle.source,
                    "policy.name",
                    f"unknown policy {bundle.scenario.policy.name!r}",
                    f"use one of {sorted(policies)}",
                )
            )
        issues.extend(facility_issues(bundle))
        try:
            params = self.validate_params(bundle)
        except ConfigError as exc:
            issues.extend(exc.issues)
            params = None
        if issues:
            raise ConfigError(issues)
        return params

    def manifest(self) -> PluginManifest:
        params = self.params_model()
        return PluginManifest(
            name=self.name,
            version=self.version,
            description=self.description,
            entity_types=tuple(self.entity_types()),
            policies=tuple(sorted(self.all_policies())),
            kpis=tuple(c.name for c in self.kpi_calculators()),
            scenario_event_types=self.disruption_types(),
            params_schema=params.model_json_schema() if params is not None else None,
            has_domain_definition=self.domain_definition() is not None,
        )


def facility_issues(bundle: ScenarioBundle) -> list[ConfigIssue]:
    """Facility errors plus scenario references to unknown places (FR-FAC-04, FR-ENG-08)."""
    from simcore.core.facility.validate import validate_facility

    fac = bundle.facility
    if fac is None:
        return []
    source = bundle.scenario.facility or bundle.source
    out = [
        ConfigIssue(source, i.where, f"{i.code}: {i.message}", i.hint)
        for i in validate_facility(fac)
        if i.severity == "error"
    ]
    places = {loc.id for loc in fac.locations} | {n.id for n in fac.nodes}
    for n, src in enumerate(bundle.scenario.demand):
        for key in ("origins", "destinations"):
            for place in getattr(src, key, []) or []:
                if place not in places:
                    out.append(
                        ConfigIssue(
                            bundle.source,
                            f"demand.{n}.{key}",
                            f"{place!r} is not a location or node of facility {fac.id!r}",
                            "use an id from the facility manifest",
                        )
                    )
    for n, entry in enumerate(bundle.scenario.fleet):
        start = entry.attributes.get("start")
        for place in start if isinstance(start, list) else [start] if start else []:
            if place not in places:
                out.append(
                    ConfigIssue(
                        bundle.source,
                        f"fleet.{n}.attributes.start",
                        f"start {place!r} is not in facility {fac.id!r}",
                        "use a location or node id",
                    )
                )
    return out


class PluginRegistry:
    def __init__(self, plugins: Sequence[DomainPlugin] = ()) -> None:
        self._plugins: dict[str, DomainPlugin] = {}
        self.errors: dict[str, str] = {}  # domain sources that failed to load
        for p in plugins:
            self.register(p)

    def register(self, plugin: DomainPlugin) -> None:
        if not isinstance(plugin, DomainPlugin):
            raise PluginError(f"{plugin!r} is not a DomainPlugin")
        existing = self._plugins.get(plugin.name)
        if existing is not None and type(existing) is not type(plugin):
            raise PluginError(
                f"two different plug-ins claim the name {plugin.name!r}: "
                f"{type(existing).__module__} and {type(plugin).__module__}"
            )
        self._plugins[plugin.name] = plugin

    def get(self, name: str) -> DomainPlugin:
        try:
            return self._plugins[name]
        except KeyError:
            raise PluginError(
                f"no domain plug-in named {name!r}; installed: {self.names}. "
                "Install the domain package or register it via the 'simcore.domains' entry point."
            ) from None

    @property
    def names(self) -> list[str]:
        return sorted(self._plugins)

    def __contains__(self, name: object) -> bool:
        return name in self._plugins

    def load(self, target: str) -> DomainPlugin:
        """Load ``module:attribute`` (instance or class) and register it."""
        module_name, _, attr = target.partition(":")
        try:
            obj: Any = getattr(importlib.import_module(module_name), attr or "plugin")
        except (ImportError, AttributeError) as exc:
            raise PluginError(f"cannot load plug-in {target!r}: {exc}") from exc
        plugin = obj() if isinstance(obj, type) else obj
        self.register(plugin)
        return plugin  # type: ignore[no-any-return]

    def discover(self, group: str = ENTRY_POINT_GROUP) -> list[str]:
        """Register every plug-in advertised through entry points, in name order."""
        found = []
        for ep in sorted(entry_points(group=group), key=lambda e: e.name):
            self.load(ep.value)
            found.append(ep.name)
        return found

    def load_tier0(self, path: Path | str) -> DomainPlugin:
        """Register a config-only domain from its ``domain.yaml`` (FR-DOM-01)."""
        eps = [ep for ep in entry_points(group=TIER0_GROUP) if ep.name == "process_network"]
        if not eps:
            raise PluginError(
                "no Tier-0 engine installed (entry point simcore.tier0:process_network)"
            )
        plugin = eps[0].load()(Path(path))
        self.register(plugin)
        return plugin  # type: ignore[no-any-return]

    def discover_tier0(self, search_path: list[Path] | None = None) -> list[str]:
        """Register every ``<dir>/<domain>/domain.yaml`` with ``tier: 0`` on the search path.

        Default search path: ``$SIMCORE_DOMAIN_PATH`` (os.pathsep-separated) followed
        by the installed ``domains`` package directory.
        """
        if search_path is None:
            search_path = [
                Path(p) for p in os.environ.get(DOMAIN_PATH_ENV, "").split(os.pathsep) if p
            ]
            spec = importlib.util.find_spec("domains")
            if spec is not None and spec.submodule_search_locations:
                search_path += [Path(p) for p in spec.submodule_search_locations]
        found = []
        for root in search_path:
            for path in sorted(root.glob("*/domain.yaml")):
                try:
                    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                    if data.get("tier") == 0 and data.get("id") not in self._plugins:
                        found.append(self.load_tier0(path).name)
                except Exception as exc:  # a broken domain must not hide the others
                    self.errors[str(path)] = str(exc)
                    log.warning("skipping domain %s: %s", path, exc)
        return found

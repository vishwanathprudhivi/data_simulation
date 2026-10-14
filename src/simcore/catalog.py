"""Lever / metric / dimension catalogs and capability manifests (FR-DOM-03, FR-SEM-01/02).

Define once, surface everywhere: the catalogs are built from the core catalog
below plus each domain's ``domain.yaml``. The API, chat tools and UI consume only
these (AC-SEM-01, AC-DOM-03). Levers are applied to scenarios through
:func:`apply_levers`, which enforces types, bounds and choices and reports
whether values lie inside the calibrated envelope (FR-SCG-09).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import TypeAdapter, ValidationError

from simcore.core.config import ConfigError, ConfigIssue, ScenarioBundle
from simcore.core.plugins import DomainPlugin
from simcore.core.units import UnitError, to_si
from simcore.specs.distributions import DistributionSpec
from simcore.specs.domain import (
    CapabilityManifest,
    DimensionCatalog,
    DimensionDef,
    DomainDefinition,
    LeverCatalog,
    LeverDef,
    MetricCatalog,
    MetricDef,
    NumberFormat,
)

__all__ = [
    "CORE_DIMENSIONS",
    "CORE_METRICS",
    "LeverApplication",
    "apply_levers",
    "capability_manifest",
    "dimension_catalog",
    "lever_catalog",
    "metric_catalog",
]

_RUN_DIMS = ["variant", "replication"]


def _m(
    id_: str,
    name: str,
    unit: str,
    direction: str,
    desc: str,
    kind: str = "number",
    decimals: int = 2,
    treatment: str = "replication_mean_ci",
) -> MetricDef:
    return MetricDef(
        id=id_,
        display_name=name,
        description=desc,
        unit=unit,
        source="kpi",
        formula=id_,
        grain="replication",
        allowed_dimensions=list(_RUN_DIMS),
        direction=direction,
        aggregation="mean",
        statistical_treatment=treatment,
        format=NumberFormat(kind=kind, decimals=decimals),
    )


#: Metrics every run produces (core KPIs, ``simcore.core.obs.kpi`` and interaction).
CORE_METRICS: list[MetricDef] = [
    _m(
        "tasks_completed",
        "Tasks completed",
        "1",
        "higher_is_better",
        "Tasks released after warm-up and completed by the end",
        "integer",
        0,
    ),
    _m(
        "throughput",
        "Throughput",
        "1/s",
        "higher_is_better",
        "Completed tasks per second over the observation window",
        decimals=4,
    ),
    _m(
        "cycle_time_mean",
        "Mean cycle time",
        "s",
        "lower_is_better",
        "Release to completion",
        "duration",
        1,
    ),
    _m(
        "cycle_time_p90",
        "Cycle time (p90)",
        "s",
        "lower_is_better",
        "90th percentile of release to completion",
        "duration",
        1,
    ),
    _m(
        "wait_time_mean",
        "Mean wait for assignment",
        "s",
        "lower_is_better",
        "Release to assignment",
        "duration",
        1,
    ),
    _m(
        "queue_length_mean",
        "Mean queue length",
        "1",
        "lower_is_better",
        "Time-weighted unassigned tasks",
    ),
    _m(
        "utilization",
        "Utilization",
        "1",
        "neutral",
        "Busy share of available agent time",
        "percent",
        1,
    ),
    _m(
        "due_time_adherence",
        "Due-time adherence",
        "1",
        "higher_is_better",
        "Share of completed tasks finished by their due time",
        "percent",
        1,
    ),
    _m(
        "blocked_time_per_agent",
        "Blocked time per agent",
        "s",
        "lower_is_better",
        "Time agents spent blocked by others or waiting for space",
        "duration",
        0,
    ),
    _m(
        "travel_distance_total",
        "Travel distance",
        "m",
        "lower_is_better",
        "Total distance travelled",
        decimals=0,
    ),
    _m(
        "colocation_time",
        "Co-location time",
        "s",
        "lower_is_better",
        "Agent-seconds of work while another agent works the same group",
        "duration",
        0,
    ),
    _m(
        "deadlocks_detected",
        "Deadlocks",
        "1",
        "lower_is_better",
        "Deadlocks detected (resolved or not)",
        "integer",
        0,
    ),
]

_EVENT_DIMS = ["variant", "replication", "time_bucket", "agent", "agent_type", "location"]


def _e(
    id_: str,
    name: str,
    unit: str,
    formula: str,
    desc: str,
    dims: list[str],
    direction: str = "neutral",
    kind: str = "number",
    grain: str = "time_bucket",
) -> MetricDef:
    return MetricDef(
        id=id_,
        display_name=name,
        description=desc,
        unit=unit,
        source="event_log",
        formula=formula,
        grain=grain,
        allowed_dimensions=dims,
        direction=direction,
        aggregation="sum",
        statistical_treatment="replication_mean_ci",
        format=NumberFormat(kind=kind, decimals=1),
    )


#: Event-level metrics. Formula grammar (governed, see services.semantic.metrics):
#: ``count(<event type>)``, ``sum|mean|max(<event type>.<data field>)`` and the
#: computed fields ``@dwell`` (time on an edge) and ``state_duration`` / ``state_intervals``.
CORE_METRICS += [
    _e(
        "completions",
        "Completions",
        "1",
        "count(task.completed)",
        "Completed tasks",
        _EVENT_DIMS,
        "higher_is_better",
        "integer",
    ),
    _e(
        "releases",
        "Releases",
        "1",
        "count(task.released)",
        "Released tasks",
        ["variant", "replication", "time_bucket", "location"],
    ),
    _e(
        "blocking_time",
        "Blocking time",
        "s",
        "sum(interaction.blocking.duration)",
        "Time agents spent blocked",
        _EVENT_DIMS,
        "lower_is_better",
        "duration",
    ),
    _e(
        "colocation_events",
        "Co-location events",
        "1",
        "count(interaction.colocation)",
        "Service starts with another agent in the same group",
        _EVENT_DIMS,
        "lower_is_better",
        "integer",
    ),
    _e(
        "edge_traversals",
        "Edge traversals",
        "1",
        "count(agent.move)",
        "Agents passing over an edge (location = edge id)",
        _EVENT_DIMS,
    ),
    _e(
        "edge_occupancy_time",
        "Edge occupancy",
        "s",
        "sum(agent.move.@dwell)",
        "Agent-seconds spent on each edge (location = edge id)",
        _EVENT_DIMS,
        kind="duration",
    ),
    _e(
        "service_time",
        "Service time",
        "s",
        "sum(service.start.duration)",
        "Work time started at each location",
        _EVENT_DIMS,
        kind="duration",
    ),
    _e(
        "state_time",
        "Time in state",
        "s",
        "state_duration",
        "Agent time by state (stacked breakdowns)",
        ["variant", "replication", "agent", "agent_type", "state"],
        kind="duration",
        grain="agent",
    ),
    _e(
        "state_intervals",
        "Agent state timeline",
        "s",
        "state_intervals",
        "Raw agent state intervals (Gantt); one replication",
        ["agent", "state", "time"],
        grain="agent",
    ),
]

CORE_DIMENSIONS: list[DimensionDef] = [  # ids must match specs.domain.CORE_DIMENSION_IDS
    DimensionDef(
        id="variant", display_name="Variant", kind="factor", description="Experiment alternative"
    ),
    DimensionDef(id="replication", display_name="Replication", kind="replication"),
    DimensionDef(
        id="time_bucket",
        display_name="Time",
        kind="time_bucket",
        description="Sim-time bucket (QuerySpec.time_bucket)",
    ),
    DimensionDef(id="agent", display_name="Agent", kind="categorical"),
    DimensionDef(id="agent_type", display_name="Agent type", kind="categorical", parent=None),
    DimensionDef(id="task_type", display_name="Task type", kind="categorical"),
    DimensionDef(id="location", display_name="Location", kind="categorical"),
    DimensionDef(id="zone", display_name="Zone", kind="categorical"),
    DimensionDef(id="state", display_name="Agent state", kind="categorical"),
    DimensionDef(
        id="time",
        display_name="Sim-time",
        kind="time_bucket",
        description="Raw event time (state intervals only)",
    ),
]


def _definition(plugin: DomainPlugin) -> DomainDefinition:
    d = plugin.domain_definition()
    if d is not None:
        return d
    # A plug-in without domain.yaml still gets a minimal catalog (core metrics only).
    return DomainDefinition(
        id=plugin.name,
        name=plugin.name,
        version=plugin.version,
        tier=2,
        plugin=type(plugin).__module__,
        description=plugin.description,
    )


def _merge(core: list[Any], domain: list[Any]) -> list[Any]:
    by_id = {x.id: x for x in core}
    for x in domain:
        by_id[x.id] = x  # domain definitions override core ones with the same id
    return list(by_id.values())


def metric_catalog(plugin: DomainPlugin) -> MetricCatalog:
    d = _definition(plugin)
    return MetricCatalog(
        domain=d.id, domain_version=d.version, metrics=_merge(CORE_METRICS, d.metrics)
    )


def dimension_catalog(plugin: DomainPlugin) -> DimensionCatalog:
    d = _definition(plugin)
    return DimensionCatalog(
        domain=d.id, domain_version=d.version, dimensions=_merge(CORE_DIMENSIONS, d.dimensions)
    )


def lever_catalog(plugin: DomainPlugin) -> LeverCatalog:
    d = _definition(plugin)
    return LeverCatalog(domain=d.id, domain_version=d.version, levers=list(d.levers))


def capability_manifest(plugin: DomainPlugin) -> CapabilityManifest:
    d = _definition(plugin)
    m = plugin.manifest()
    return CapabilityManifest(
        domain=d.id,
        domain_version=d.version,
        tier=d.tier,
        name=d.name,
        description=d.description,
        levers=lever_catalog(plugin).levers,
        metrics=metric_catalog(plugin).metrics,
        dimensions=dimension_catalog(plugin).dimensions,
        policies=sorted(set(m.policies) & set(d.policies) or m.policies),
        disruption_types=list(m.scenario_event_types),
        entity_types=sorted(
            {
                *m.entity_types,
                *(e.id for e in (*d.entity_types, *d.resource_types, *d.work_item_types)),
            }
        ),
        envelope=list(d.envelope),
    )


# ----------------------------------------------------------------------------- levers


@dataclass(frozen=True)
class LeverApplication:
    """Result of applying lever values to a scenario."""

    bundle: ScenarioBundle
    overrides: dict[str, Any]
    values: dict[str, Any]
    outside_envelope: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def extrapolated(self) -> bool:
        return bool(self.outside_envelope)


def _coerce(lever: LeverDef, value: Any) -> Any:
    t = lever.type
    if t == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"expects true/false, got {value!r}")
        return value
    if t == "enum":
        if value not in lever.choices:
            raise ValueError(f"must be one of {lever.choices}, got {value!r}")
        return value
    if t == "distribution":
        try:
            return TypeAdapter(DistributionSpec).validate_python(value).model_dump(mode="json")
        except ValidationError as exc:
            raise ValueError(f"is not a valid distribution: {exc.errors()[0]['msg']}") from exc
    if t == "duration":
        try:
            num = to_si(value, "time")
        except UnitError as exc:
            raise ValueError(str(exc)) from exc
    elif t == "int":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
            raise ValueError(f"expects an integer, got {value!r}")
        num = int(value)
    else:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"expects a number, got {value!r}")
        num = float(value)
    if lever.min is not None and num < lever.min:
        raise ValueError(f"{num} is below the minimum {lever.min:g}")
    if lever.max is not None and num > lever.max:
        raise ValueError(f"{num} is above the maximum {lever.max:g}")
    return num


def apply_levers(
    bundle: ScenarioBundle, plugin: DomainPlugin, values: Mapping[str, Any]
) -> LeverApplication:
    """Validate ``lever id -> value`` against the catalog and apply them as overrides."""
    d = _definition(plugin)
    levers = {lv.id: lv for lv in d.levers}
    issues: list[ConfigIssue] = []
    overrides: dict[str, Any] = {}
    clean: dict[str, Any] = {}
    for lever_id, value in values.items():
        lever = levers.get(lever_id)
        if lever is None:
            issues.append(
                ConfigIssue(
                    f"domain {d.id}",
                    f"levers.{lever_id}",
                    f"{lever_id!r} is not a lever of this domain",
                    f"available levers: {sorted(levers)}",
                )
            )
            continue
        try:
            coerced = _coerce(lever, value)
        except ValueError as exc:
            issues.append(
                ConfigIssue(
                    f"domain {d.id}",
                    f"levers.{lever_id}",
                    f"{lever.display_name} {exc}",
                    _bounds_hint(lever),
                )
            )
            continue
        clean[lever_id] = coerced
        overrides[lever.path] = coerced
    if issues:
        raise ConfigError(issues)
    outside: dict[str, dict[str, Any]] = {}
    for bound in d.envelope:
        if bound.lever in clean and isinstance(clean[bound.lever], (int, float)):
            v = clean[bound.lever]
            if (bound.min is not None and v < bound.min) or (
                bound.max is not None and v > bound.max
            ):
                outside[bound.lever] = {"value": v, "min": bound.min, "max": bound.max}
    return LeverApplication(bundle.with_overrides(overrides), overrides, clean, outside)


def _bounds_hint(lever: LeverDef) -> str:
    if lever.type == "enum":
        return f"choose one of {lever.choices}"
    parts = []
    if lever.min is not None:
        parts.append(f">= {lever.min:g}")
    if lever.max is not None:
        parts.append(f"<= {lever.max:g}")
    unit = f" {lever.unit}" if lever.unit else ""
    return f"use a {lever.type}{unit} " + " and ".join(parts) if parts else f"use a {lever.type}"

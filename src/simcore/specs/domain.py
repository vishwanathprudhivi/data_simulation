"""``DomainDefinition`` (``domain.yaml``) and derived catalogs (FR-DOM-01..03, FR-SEM-01/02).

Define once, surface everywhere: metrics, dimensions and levers declared here are
what the API, chat tools and UI expose. Nothing else may hard-code them.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from simcore.specs.common import Duration, Identifier, SchemaVersion, SpecModel
from simcore.specs.distributions import DistributionSpec

__all__ = [
    "CORE_DIMENSION_IDS",
    "CapabilityManifest",
    "DimensionCatalog",
    "DimensionDef",
    "DomainDefinition",
    "EnvelopeBound",
    "FailureDef",
    "LeverCatalog",
    "LeverDef",
    "MetricCatalog",
    "MetricDef",
    "NumberFormat",
    "derive_catalogs",
]


#: Dimensions every run supports (declared in ``simcore.catalog.CORE_DIMENSIONS``).
CORE_DIMENSION_IDS: tuple[str, ...] = (
    "variant",
    "replication",
    "time_bucket",
    "agent",
    "agent_type",
    "task_type",
    "location",
    "zone",
    "state",
    "time",
)


class NumberFormat(SpecModel):
    kind: Literal["number", "percent", "duration", "integer"] = "number"
    decimals: int = Field(default=2, ge=0, le=10)


class MetricDef(SpecModel):
    id: Identifier
    display_name: str
    description: str = ""
    unit: str = Field(description="SI unit symbol or '1' for dimensionless.")
    source: Literal["kpi", "event_log"] = "kpi"
    formula: str = Field(description="KPI key, or governed expression over event-log columns.")
    grain: Literal["replication", "task", "agent", "time_bucket"] = "replication"
    allowed_dimensions: list[str] = Field(default_factory=list)
    direction: Literal["higher_is_better", "lower_is_better", "neutral"] = "neutral"
    aggregation: Literal["mean", "sum", "ratio_of_means", "p50", "p90", "p99", "min", "max"] = (
        "mean"
    )
    statistical_treatment: Literal["replication_mean_ci", "ratio_of_means", "none"] = (
        "replication_mean_ci"
    )
    format: NumberFormat = Field(default_factory=NumberFormat)
    synonyms: list[str] = Field(default_factory=list, description="Words users say for it.")


class DimensionDef(SpecModel):
    id: Identifier
    display_name: str
    description: str = ""
    kind: Literal["time_bucket", "categorical", "factor", "replication"] = "categorical"
    column: str | None = Field(default=None, description="Source column; defaults to id.")
    parent: Identifier | None = Field(default=None, description="Hierarchy parent dimension.")


class LeverDef(SpecModel):
    id: Identifier
    display_name: str
    description: str = ""
    path: str = Field(description="Scenario path the lever sets, e.g. 'fleet.server.count'.")
    type: Literal["int", "float", "bool", "enum", "duration", "distribution"]
    unit: str | None = None
    min: float | None = None
    max: float | None = None
    step: float | None = None
    default: Any = None
    choices: list[Any] = Field(default_factory=list)
    depends_on: list[Identifier] = Field(default_factory=list)
    synonyms: list[str] = Field(
        default_factory=list, description="Words users say for this lever (compile hints)."
    )

    @model_validator(mode="after")
    def _check(self) -> LeverDef:
        if self.type == "enum" and not self.choices:
            raise ValueError(f"lever {self.id!r}: enum levers need 'choices'")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(f"lever {self.id!r}: min ({self.min}) > max ({self.max})")
        return self


class EnvelopeBound(SpecModel):
    """Calibrated envelope bound for one lever (FR-SCG-09, FR-DOM-06)."""

    lever: Identifier
    min: float | None = None
    max: float | None = None


class EntityTypeDef(SpecModel):
    id: Identifier
    description: str = ""
    attributes: dict[str, str] = Field(default_factory=dict, description="name -> type")


class FailureDef(SpecModel):
    """Random breakdowns: time between failures (while busy or idle) and repair time."""

    mtbf: DistributionSpec
    mttr: DistributionSpec


class StationDef(SpecModel):
    """Tier-0 ``process_network`` station (FR-DOM-01)."""

    id: Identifier
    description: str = ""
    resource_type: Identifier | None = Field(
        default=None, description="Operator type required while working (from the fleet)."
    )
    capacity: int = Field(default=1, ge=1, description="Parallel servers.")
    service_time: DistributionSpec | None = Field(
        default=None, description="Per batch; required unless the scenario overrides it."
    )
    batch_size: int = Field(default=1, ge=1)
    buffer_capacity: int | None = Field(
        default=None, ge=0, description="Max items waiting; full = upstream blocks."
    )
    changeover: Duration | None = Field(
        default=None, description="Setup time when the item type changes."
    )
    failures: FailureDef | None = None


class RoutingDef(SpecModel):
    work_item_type: Identifier
    steps: list[Identifier] = Field(min_length=1)


class ProcessDef(SpecModel):
    stations: list[StationDef] = Field(default_factory=list)
    routings: list[RoutingDef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> ProcessDef:
        ids = [st.id for st in self.stations]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate station ids: {dupes}")
        for r in self.routings:
            unknown = [step for step in r.steps if step not in ids]
            if unknown:
                raise ValueError(
                    f"routing for {r.work_item_type!r} uses unknown stations {unknown}; "
                    f"known: {ids}"
                )
        types = [r.work_item_type for r in self.routings]
        dupes = sorted({t for t in types if types.count(t) > 1})
        if dupes:
            raise ValueError(f"more than one routing for work item types {dupes}")
        return self


class DomainDefinition(SpecModel):
    schema_version: SchemaVersion = "1.0"
    id: Identifier
    name: str
    version: str = Field(description="Domain version recorded by every scenario (FR-DOM-10).")
    tier: Literal[0, 1, 2]
    description: str = ""
    entity_types: list[EntityTypeDef] = Field(default_factory=list)
    resource_types: list[EntityTypeDef] = Field(default_factory=list)
    work_item_types: list[EntityTypeDef] = Field(default_factory=list)
    process: ProcessDef | None = None
    zone_semantics: dict[str, str] = Field(default_factory=dict)
    policies: list[str] = Field(default_factory=lambda: ["fifo"])
    metrics: list[MetricDef] = Field(default_factory=list)
    dimensions: list[DimensionDef] = Field(default_factory=list)
    levers: list[LeverDef] = Field(default_factory=list)
    default_distributions: dict[str, DistributionSpec] = Field(default_factory=dict)
    asset_bindings: dict[str, str] = Field(default_factory=dict)
    plugin: str | None = Field(default=None, description="Python module for Tier 1/2 hooks.")
    envelope: list[EnvelopeBound] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> DomainDefinition:
        for label, items in (
            ("metrics", self.metrics),
            ("dimensions", self.dimensions),
            ("levers", self.levers),
        ):
            ids = [i.id for i in items]
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            if dupes:
                raise ValueError(f"duplicate {label} ids: {dupes}")
        dims = {d.id for d in self.dimensions} | set(CORE_DIMENSION_IDS)
        for d in self.dimensions:
            if d.parent is not None and d.parent not in dims:
                raise ValueError(f"dimension {d.id!r} has unknown parent {d.parent!r}")
        for m in self.metrics:
            unknown = sorted(set(m.allowed_dimensions) - dims)
            if unknown:
                raise ValueError(
                    f"metric {m.id!r} allows undeclared dimensions {unknown}; "
                    "declare them under 'dimensions'"
                )
        levers = {lv.id for lv in self.levers}
        for lv in self.levers:
            missing = sorted(set(lv.depends_on) - levers)
            if missing:
                raise ValueError(f"lever {lv.id!r} depends on unknown levers {missing}")
        for b in self.envelope:
            if b.lever not in levers:
                raise ValueError(f"envelope references unknown lever {b.lever!r}")
        if self.tier in (1, 2) and self.plugin is None:
            raise ValueError(f"tier {self.tier} domains must name a 'plugin' module")
        return self


class CapabilityManifest(SpecModel):
    """Everything a domain lets users change, measure and slice (FR-DOM-03)."""

    schema_version: SchemaVersion = "1.0"
    domain: Identifier
    domain_version: str
    tier: Literal[0, 1, 2]
    name: str
    description: str = ""
    levers: list[LeverDef]
    metrics: list[MetricDef]
    dimensions: list[DimensionDef]
    policies: list[str]
    disruption_types: list[str]
    entity_types: list[str]
    envelope: list[EnvelopeBound] = Field(default_factory=list)


class LeverCatalog(SpecModel):
    schema_version: SchemaVersion = "1.0"
    domain: Identifier
    domain_version: str
    levers: list[LeverDef]


class MetricCatalog(SpecModel):
    schema_version: SchemaVersion = "1.0"
    domain: Identifier
    domain_version: str
    metrics: list[MetricDef]


class DimensionCatalog(SpecModel):
    schema_version: SchemaVersion = "1.0"
    domain: Identifier
    domain_version: str
    dimensions: list[DimensionDef]


def derive_catalogs(
    definition: DomainDefinition,
) -> tuple[LeverCatalog, MetricCatalog, DimensionCatalog]:
    """Derive the lever/metric/dimension catalogs from a domain definition (FR-DOM-03)."""
    common = {"domain": definition.id, "domain_version": definition.version}
    return (
        LeverCatalog(levers=list(definition.levers), **common),
        MetricCatalog(metrics=list(definition.metrics), **common),
        DimensionCatalog(dimensions=list(definition.dimensions), **common),
    )

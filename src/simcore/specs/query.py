"""``QuerySpec``: governed request for metrics (FR-SEM-03/05). Compiled to DuckDB in M8."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from simcore.specs.common import Duration, Identifier, SchemaVersion, SpecModel

__all__ = ["Comparison", "FilterSpec", "QueryScope", "QuerySpec", "SortSpec"]

Scalar = str | int | float | bool


class QueryScope(SpecModel):
    experiment_id: Identifier | None = None
    run_ids: list[str] = Field(default_factory=list)
    scenarios: list[str] = Field(default_factory=list)
    variants: list[str] = Field(default_factory=list)


class FilterSpec(SpecModel):
    dimension: Identifier
    op: Literal["eq", "ne", "in", "not_in", "gte", "lte", "between"]
    value: Scalar | list[Scalar]


class Comparison(SpecModel):
    kind: Literal["paired_vs_baseline", "independent_vs_baseline"] = "paired_vs_baseline"
    baseline: str = "baseline"


class SortSpec(SpecModel):
    field: str
    descending: bool = False


class QuerySpec(SpecModel):
    schema_version: SchemaVersion = "1.0"
    id: Identifier | None = None
    scope: QueryScope = Field(default_factory=QueryScope)
    metrics: list[Identifier] = Field(min_length=1)
    dimensions: list[Identifier] = Field(default_factory=list)
    filters: list[FilterSpec] = Field(default_factory=list)
    comparison: Comparison | None = None
    time_bucket: Duration | None = None
    aggregate_replications: bool = True
    confidence: float = Field(default=0.95, gt=0, lt=1)
    sort: list[SortSpec] = Field(default_factory=list)
    limit: int = Field(default=1000, ge=1, le=100_000)

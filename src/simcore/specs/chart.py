"""``ChartSpec``: restricted Vega-Lite-like encoding bound to a ``QuerySpec`` (FR-VIZ-01/02/06).

There is deliberately no field that can hold data values: the renderer fetches
its own data by executing ``query``. Unknown fields (e.g. ``data``/``values``)
are rejected by validation.
"""

from __future__ import annotations

from typing import Literal, get_args

from pydantic import Field

from simcore.specs.common import Identifier, SchemaVersion, SpecModel
from simcore.specs.query import QuerySpec

__all__ = ["CHART_TEMPLATES", "ChartSpec", "Encoding", "FieldRef"]

ChartTemplate = Literal[
    "bar_ci",
    "point_ci",
    "timeseries_band",
    "box",
    "violin",
    "histogram",
    "ecdf",
    "heatmap",
    "stacked_breakdown",
    "waterfall",
    "tornado",
    "pareto",
    "gantt",
    "comparison_table",
    "floorplan_overlay",
]
CHART_TEMPLATES: tuple[str, ...] = get_args(ChartTemplate)


class FieldRef(SpecModel):
    field: str = Field(description="Metric or dimension id from the query (or '<metric>.ci_low').")
    title: str | None = None
    sort: Literal["ascending", "descending"] | None = None
    scale: Literal["linear", "log"] | None = None


class Encoding(SpecModel):
    x: FieldRef | None = None
    y: FieldRef | None = None
    color: FieldRef | None = None
    size: FieldRef | None = None
    row: FieldRef | None = None
    column: FieldRef | None = None
    tooltip: list[FieldRef] = Field(default_factory=list)


class ChartSpec(SpecModel):
    schema_version: SchemaVersion = "1.0"
    id: Identifier | None = None
    title: str = ""
    description: str = ""
    template: ChartTemplate
    query: QuerySpec
    encoding: Encoding = Field(default_factory=Encoding)
    show_uncertainty: bool = Field(
        default=True, description="Replicated metrics show CIs unless the user asks otherwise."
    )

"""ChartSpec validation and Vega-Lite rendering (FR-VIZ-01..06).

* ``validate_chart`` checks the spec against its schema, the metric catalog and the
  template's requirements, returning repair-ready issues (path, problem,
  suggestion, allowed values) so an assistant can fix it in <= 2 retries.
* Replicated metrics show uncertainty unless the user asked otherwise (FR-VIZ-06).
* ``render_chart`` turns a validated spec plus a governed query result into a
  Vega-Lite specification; the data comes only from the query, never from the spec.
"""

from __future__ import annotations

import difflib
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import ValidationError

from services.semantic.query import QueryIssue, QueryResult
from simcore.specs.chart import CHART_TEMPLATES, ChartSpec
from simcore.specs.domain import MetricDef

__all__ = ["TEMPLATE_RULES", "ChartValidationError", "render_chart", "validate_chart"]

VEGA_LITE = "https://vega.github.io/schema/vega-lite/v5.json"
DERIVED = ("ci_low", "ci_high", "n", "diff", "diff_ci_low", "diff_ci_high", "ci_excludes_zero")

#: template -> (description, rule) used for validation and for the chart builder UI
TEMPLATE_RULES: dict[str, str] = {
    "bar_ci": "metrics by a categorical dimension (default variant) with CI error bars",
    "point_ci": "point estimates with CI error bars",
    "timeseries_band": "event metric over time_bucket with a CI band (needs time_bucket)",
    "box": "distribution across replications (needs 'replication' in dimensions)",
    "violin": "density across replications (needs 'replication' in dimensions)",
    "histogram": "histogram of replication values (needs 'replication' in dimensions)",
    "ecdf": "empirical CDF of replication values (needs 'replication' in dimensions)",
    "heatmap": "one metric over two dimensions",
    "stacked_breakdown": "state_time by state (or several metrics) stacked per variant",
    "waterfall": "contributions of several metrics' differences vs baseline",
    "tornado": "differences vs baseline (needs a comparison)",
    "pareto": "two metrics against each other per variant (trade-off frontier)",
    "gantt": "agent state timeline (metric state_intervals)",
    "comparison_table": "table of estimates and differences",
    "floorplan_overlay": "metric by location on the facility floor plan (needs 'location')",
}


class ChartValidationError(ValueError):
    def __init__(self, issues: Sequence[QueryIssue]) -> None:
        self.issues = list(issues)
        super().__init__("; ".join(f"{i.path}: {i.problem}" for i in self.issues))

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": "invalid_chart_spec",
            "repairable": True,
            "issues": [i.to_dict() for i in self.issues],
        }


def _close(name: str, options: Sequence[str]) -> str:
    m = difflib.get_close_matches(name, list(options), n=1)
    return f"did you mean {m[0]!r}?" if m else ""


def validate_chart(
    chart: ChartSpec | Mapping[str, Any],
    catalog: Mapping[str, MetricDef],
    *,
    user_requested_no_uncertainty: bool = False,
) -> ChartSpec:
    """Validate ``chart``; raise :class:`ChartValidationError` with repair-ready issues."""
    issues: list[QueryIssue] = []
    if not isinstance(chart, ChartSpec):
        try:
            chart = ChartSpec.model_validate(dict(chart))
        except ValidationError as exc:
            for err in exc.errors(include_url=False):
                loc = ".".join(str(p) for p in err["loc"])
                if err["type"] == "extra_forbidden" and err["loc"][-1] in (
                    "data",
                    "values",
                    "datasets",
                ):
                    issues.append(
                        QueryIssue(
                            loc,
                            "charts may not contain data values",
                            "remove it; data comes from the query",
                        )
                    )
                elif err["type"] == "literal_error" and err["loc"][-1] == "template":
                    got = str(err.get("input"))
                    issues.append(
                        QueryIssue(
                            loc,
                            f"unknown template {got!r}",
                            _close(got, CHART_TEMPLATES),
                            CHART_TEMPLATES,
                        )
                    )
                else:
                    issues.append(QueryIssue(loc, err["msg"]))
            raise ChartValidationError(issues) from exc
    q = chart.query
    for n, mid in enumerate(q.metrics):
        if mid not in catalog:
            issues.append(
                QueryIssue(
                    f"query.metrics[{n}]",
                    f"unknown metric {mid!r}",
                    _close(mid, list(catalog)),
                    sorted(catalog),
                )
            )
    known = [catalog[m] for m in q.metrics if m in catalog]
    for m in known:
        for dim in q.dimensions:
            if dim not in m.allowed_dimensions and dim not in ("variant", "replication"):
                issues.append(
                    QueryIssue(
                        "query.dimensions",
                        f"metric {m.id!r} cannot be sliced by {dim!r}",
                        _close(dim, m.allowed_dimensions),
                        m.allowed_dimensions,
                    )
                )
    fields = {
        *q.dimensions,
        "variant",
        "replication",
        *q.metrics,
        *(f"{m}.{d}" for m in q.metrics for d in DERIVED),
        "start",
        "end",
        "agent",
        "state",
    }
    enc = chart.encoding
    for name in ("x", "y", "color", "size", "row", "column"):
        ref = getattr(enc, name)
        if ref is not None and ref.field not in fields:
            issues.append(
                QueryIssue(
                    f"encoding.{name}.field",
                    f"field {ref.field!r} is not in the query",
                    _close(ref.field, sorted(fields)),
                    sorted(fields),
                )
            )
    t = chart.template
    dims = set(q.dimensions)
    need_reps = t in ("box", "violin", "histogram", "ecdf")
    if need_reps and "replication" not in dims:
        issues.append(
            QueryIssue(
                "query.dimensions",
                f"template {t!r} needs per-replication values",
                "add 'replication' to dimensions",
            )
        )
    if t == "timeseries_band" and ("time_bucket" not in dims or q.time_bucket is None):
        issues.append(
            QueryIssue(
                "query.dimensions",
                "timeseries_band needs the time_bucket dimension and query.time_bucket",
                "add both",
            )
        )
    if t == "heatmap" and len([d for d in q.dimensions if d != "replication"]) < 2:
        issues.append(
            QueryIssue("query.dimensions", "heatmap needs two dimensions", "add a second dimension")
        )
    if t in ("tornado", "waterfall") and q.comparison is None:
        issues.append(
            QueryIssue(
                "query.comparison",
                f"{t} needs a comparison vs baseline",
                "add comparison: {kind: paired_vs_baseline, baseline: ...}",
            )
        )
    if t == "pareto" and len(q.metrics) < 2:
        issues.append(
            QueryIssue("query.metrics", "pareto needs two metrics", "add a second metric")
        )
    if t == "gantt" and q.metrics != ["state_intervals"]:
        issues.append(
            QueryIssue(
                "query.metrics",
                "gantt shows the state_intervals metric",
                "set metrics: [state_intervals]",
            )
        )
    if t == "stacked_breakdown" and not ("state" in dims or len(q.metrics) >= 2):
        issues.append(
            QueryIssue(
                "query.dimensions",
                "stacked_breakdown needs the 'state' dimension (state_time) or several metrics",
                "use metrics [state_time] with dimensions [variant, state]",
            )
        )
    if t == "floorplan_overlay" and "location" not in dims:
        issues.append(
            QueryIssue(
                "query.dimensions",
                "floorplan_overlay needs the 'location' dimension",
                "add 'location'",
            )
        )
    replicated = any(m.statistical_treatment != "none" for m in known)
    if (
        replicated
        and not chart.show_uncertainty
        and not user_requested_no_uncertainty
        and t not in ("gantt", "floorplan_overlay")
    ):
        issues.append(
            QueryIssue(
                "show_uncertainty",
                "replicated metrics must show "
                "uncertainty unless the user asks otherwise (FR-VIZ-06)",
                "set show_uncertainty: true",
            )
        )
    if issues:
        raise ChartValidationError(issues)
    return chart


# ----------------------------------------------------------------------------- rendering


def _f(name: str) -> str:
    """Escape dots so Vega-Lite does not treat them as nested access."""
    return name.replace(".", "\\.")


def _title(metric: MetricDef | None, field: str) -> str:
    if metric is None:
        return field
    unit = "" if metric.unit in ("1", "") else f" ({metric.unit})"
    return f"{metric.display_name}{unit}"


def _fmt(metric: MetricDef | None) -> str:
    if metric is None:
        return ".3~g"
    kind, dec = metric.format.kind, metric.format.decimals
    return {"percent": f".{dec}%", "integer": ",.0f"}.get(kind, f",.{dec}f")


def render_chart(
    chart: ChartSpec, result: QueryResult, facility: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Vega-Lite spec for ``chart`` filled with ``result``'s rows."""
    metrics = {m.id: m for m in result.metrics}
    q = chart.query
    m0 = q.metrics[0]
    md = metrics.get(m0)
    x_dim = next((d for d in q.dimensions if d not in ("replication", "time_bucket")), "variant")
    enc = chart.encoding
    xf = enc.x.field if enc.x else x_dim
    color = enc.color.field if enc.color else None
    unc = chart.show_uncertainty
    base: dict[str, Any] = {
        "$schema": VEGA_LITE,
        "title": chart.title or f"{_title(md, m0)} by {xf}",
        "description": chart.description,
        "data": {"values": result.rows},
        "usermeta": {"ref_id": result.ref_id, "template": chart.template},
        "width": 480,
        "height": 300,
    }
    t = chart.template
    y = {
        "field": _f(m0),
        "type": "quantitative",
        "title": _title(md, m0),
        "axis": {"format": _fmt(md)},
    }
    tooltip = [
        {"field": _f(c), "type": "nominal" if c in q.dimensions else "quantitative"}
        for c in result.columns[:8]
    ]
    if t in ("bar_ci", "point_ci"):
        mark = "bar" if t == "bar_ci" else "point"
        layers: list[dict[str, Any]] = [
            {"mark": {"type": mark, "tooltip": True}, "encoding": {"y": y, "tooltip": tooltip}}
        ]
        if unc:
            layers.append(
                {
                    "mark": {"type": "errorbar", "ticks": True},
                    "encoding": {
                        "y": {
                            "field": _f(f"{m0}.ci_low"),
                            "type": "quantitative",
                            "title": _title(md, m0),
                        },
                        "y2": {"field": _f(f"{m0}.ci_high")},
                    },
                }
            )
        spec = {
            "encoding": {
                "x": {"field": _f(xf), "type": "nominal"},
                **({"color": {"field": _f(color), "type": "nominal"}} if color else {}),
            },
            "layer": layers,
        }
    elif t == "timeseries_band":
        line = {"mark": "line", "encoding": {"y": y}}
        layers = [line]
        if unc:
            layers.insert(
                0,
                {
                    "mark": {"type": "area", "opacity": 0.25},
                    "encoding": {
                        "y": {"field": _f(f"{m0}.ci_low"), "type": "quantitative"},
                        "y2": {"field": _f(f"{m0}.ci_high")},
                    },
                },
            )
        spec = {
            "encoding": {
                "x": {"field": "time_bucket", "type": "quantitative", "title": "Sim-time (s)"},
                "color": {"field": _f(color or "variant"), "type": "nominal"},
            },
            "layer": layers,
        }
    elif t in ("box", "violin"):
        if t == "box":
            spec = {
                "mark": {"type": "boxplot", "extent": "min-max"},
                "encoding": {"x": {"field": _f(xf), "type": "nominal"}, "y": y},
            }
        else:
            spec = {
                "transform": [{"density": _f(m0), "groupby": [xf], "as": ["value", "density"]}],
                "mark": "area",
                "encoding": {
                    "x": {"field": "value", "type": "quantitative", "title": _title(md, m0)},
                    "y": {
                        "field": "density",
                        "type": "quantitative",
                        "stack": "center",
                        "axis": None,
                    },
                    "column": {"field": _f(xf), "type": "nominal"},
                },
            }
    elif t == "histogram":
        spec = {
            "mark": "bar",
            "encoding": {
                "x": {
                    "field": _f(m0),
                    "bin": True,
                    "type": "quantitative",
                    "title": _title(md, m0),
                },
                "y": {"aggregate": "count", "type": "quantitative"},
                "color": {"field": _f(xf), "type": "nominal"},
            },
        }
    elif t == "ecdf":
        spec = {
            "transform": [
                {
                    "window": [{"op": "cume_dist", "as": "ecdf"}],
                    "sort": [{"field": _f(m0)}],
                    "groupby": [xf],
                }
            ],
            "mark": {"type": "line", "interpolate": "step-after"},
            "encoding": {
                "x": {"field": _f(m0), "type": "quantitative", "title": _title(md, m0)},
                "y": {"field": "ecdf", "type": "quantitative"},
                "color": {"field": _f(xf), "type": "nominal"},
            },
        }
    elif t == "heatmap":
        d1, d2 = [d for d in q.dimensions if d != "replication"][:2]
        spec = {
            "mark": "rect",
            "encoding": {
                "x": {"field": _f(d1), "type": "ordinal"},
                "y": {"field": _f(d2), "type": "ordinal"},
                "color": {"field": _f(m0), "type": "quantitative", "title": _title(md, m0)},
                "tooltip": tooltip,
            },
        }
    elif t == "stacked_breakdown":
        if "state" in q.dimensions:
            spec = {
                "mark": "bar",
                "encoding": {
                    "x": {"field": _f(xf), "type": "nominal"},
                    "y": {
                        "field": _f(m0),
                        "type": "quantitative",
                        "stack": "normalize",
                        "title": "Share of time",
                    },
                    "color": {"field": "state", "type": "nominal"},
                    "tooltip": tooltip,
                },
            }
        else:
            spec = {
                "transform": [{"fold": [_f(m) for m in q.metrics], "as": ["metric", "value"]}],
                "mark": "bar",
                "encoding": {
                    "x": {"field": _f(xf), "type": "nominal"},
                    "y": {"field": "value", "type": "quantitative", "stack": True},
                    "color": {"field": "metric", "type": "nominal"},
                },
            }
    elif t in ("tornado", "waterfall"):
        baseline = q.comparison.baseline if q.comparison is not None else "baseline"
        if t == "tornado":
            spec = {
                "layer": [
                    {
                        "mark": "bar",
                        "encoding": {
                            "y": {"field": _f(xf), "type": "nominal", "sort": "-x"},
                            "x": {
                                "field": _f(f"{m0}.diff"),
                                "type": "quantitative",
                                "title": f"Δ {_title(md, m0)} vs {baseline}",
                            },
                            "color": {
                                "field": _f(f"{m0}.ci_excludes_zero"),
                                "type": "nominal",
                                "title": "CI excludes 0",
                            },
                        },
                    },
                    *(
                        [
                            {
                                "mark": {"type": "errorbar"},
                                "encoding": {
                                    "y": {"field": _f(xf), "type": "nominal"},
                                    "x": {"field": _f(f"{m0}.diff_ci_low"), "type": "quantitative"},
                                    "x2": {"field": _f(f"{m0}.diff_ci_high")},
                                },
                            }
                        ]
                        if unc
                        else []
                    ),
                ]
            }
        else:
            spec = {
                "transform": [
                    {"fold": [_f(f"{m}.diff") for m in q.metrics], "as": ["metric", "delta"]},
                    {"window": [{"op": "sum", "field": "delta", "as": "end"}], "groupby": [xf]},
                    {"calculate": "datum.end - datum.delta", "as": "start"},
                ],
                "mark": "bar",
                "encoding": {
                    "x": {"field": "metric", "type": "nominal", "sort": None},
                    "y": {"field": "start", "type": "quantitative", "title": "Δ vs baseline"},
                    "y2": {"field": "end"},
                    "color": {"field": _f(xf), "type": "nominal"},
                },
            }
    elif t == "pareto":
        m1 = q.metrics[1]
        layers = [
            {
                "mark": {"type": "point", "filled": True, "size": 80},
                "encoding": {
                    "x": {"field": _f(m0), "type": "quantitative", "title": _title(md, m0)},
                    "y": {
                        "field": _f(m1),
                        "type": "quantitative",
                        "title": _title(metrics.get(m1), m1),
                    },
                    "color": {"field": _f(xf), "type": "nominal"},
                    "tooltip": tooltip,
                },
            }
        ]
        if unc:
            layers.append(
                {
                    "mark": "errorbar",
                    "encoding": {
                        "x": {"field": _f(f"{m0}.ci_low"), "type": "quantitative"},
                        "x2": {"field": _f(f"{m0}.ci_high")},
                        "y": {"field": _f(m1), "type": "quantitative"},
                    },
                }
            )
        spec = {"layer": layers}
    elif t == "gantt":
        spec = {
            "mark": "bar",
            "encoding": {
                "y": {"field": "agent", "type": "nominal"},
                "x": {"field": "start", "type": "quantitative", "title": "Sim-time (s)"},
                "x2": {"field": "end"},
                "color": {"field": "state", "type": "nominal"},
                "tooltip": [
                    {"field": "agent"},
                    {"field": "state"},
                    {"field": "start", "type": "quantitative"},
                    {"field": "end", "type": "quantitative"},
                ],
            },
        }
        base["height"] = max(200, 14 * len({r["agent"] for r in result.rows}))
    elif t == "comparison_table":
        cols = [c for c in result.columns if not c.endswith(".n")]
        spec = {
            "transform": [
                {"window": [{"op": "row_number", "as": "row"}]},
                {"fold": [_f(c) for c in cols], "as": ["column", "value"]},
            ],
            "mark": "text",
            "encoding": {
                "y": {"field": "row", "type": "ordinal", "axis": None},
                "x": {
                    "field": "column",
                    "type": "nominal",
                    "axis": {"orient": "top"},
                    "sort": cols,
                },
                "text": {"field": "value", "type": "nominal"},
            },
        }
        base["usermeta"]["table"] = {"columns": cols, "rows": result.rows}
    elif t == "floorplan_overlay":
        spec = _floorplan(chart, result, facility, md)
    else:  # pragma: no cover - CHART_TEMPLATES and this switch must stay in sync
        raise ValueError(f"no renderer for template {t!r}")
    return {**base, **spec}


def _floorplan(
    chart: ChartSpec, result: QueryResult, facility: Mapping[str, Any] | None, md: MetricDef | None
) -> dict[str, Any]:
    """Edges as segments coloured by the metric, locations as sized points (FR-VIZ-03).

    With a ``time_bucket`` dimension a slider scrubs through time buckets.
    """
    if facility is None:
        raise ValueError("floorplan_overlay needs the run's facility")
    nodes = facility["nodes"]
    m0 = chart.query.metrics[0]
    by_loc: dict[str, list[dict[str, Any]]] = {}
    for r in result.rows:
        by_loc.setdefault(str(r.get("location")), []).append(r)
    edges, points = [], []
    for e in facility["edges"]:
        (x, y0), (x2, y2) = nodes[e["source"]][:2], nodes[e["target"]][:2]
        rows = by_loc.get(e["id"]) or [{}]
        for r in rows:
            edges.append(
                {
                    "edge": e["id"],
                    "x": x,
                    "y": y0,
                    "x2": x2,
                    "y2": y2,
                    "value": r.get(m0),
                    "time_bucket": r.get("time_bucket"),
                    "variant": r.get("variant"),
                }
            )
    for loc in facility["manifest"].get("locations", []):
        loc_rows = by_loc.get(loc["id"])
        if not loc_rows:
            continue
        px, py = nodes[loc["node"]][:2]
        for r in loc_rows:
            points.append(
                {
                    "location": loc["id"],
                    "x": px,
                    "y": py,
                    "value": r.get(m0),
                    "time_bucket": r.get("time_bucket"),
                    "variant": r.get("variant"),
                }
            )
    scrub = "time_bucket" in chart.query.dimensions
    params = (
        [
            {
                "name": "t",
                "value": min(
                    (r["time_bucket"] for r in result.rows if r.get("time_bucket") is not None),
                    default=0,
                ),
                "bind": {
                    "input": "range",
                    "min": 0,
                    "step": chart.query.time_bucket,
                    "max": max((r.get("time_bucket") or 0 for r in result.rows), default=0),
                },
            }
        ]
        if scrub
        else []
    )
    flt = [{"filter": "datum.time_bucket == null || datum.time_bucket == t"}] if scrub else []
    title = _title(md, m0)
    layers: list[dict[str, Any]] = [
        {
            "data": {"values": edges},
            "transform": flt,
            "mark": {"type": "rule", "strokeWidth": 4},
            "encoding": {
                "x": {"field": "x", "type": "quantitative", "title": "x (m)"},
                "y": {"field": "y", "type": "quantitative", "title": "y (m)"},
                "x2": {"field": "x2"},
                "y2": {"field": "y2"},
                "color": {
                    "field": "value",
                    "type": "quantitative",
                    "title": title,
                    "scale": {"scheme": "orangered"},
                },
                "tooltip": [{"field": "edge"}, {"field": "value", "type": "quantitative"}],
            },
        },
    ]
    if points:
        layers.append(
            {
                "data": {"values": points},
                "transform": flt,
                "mark": {"type": "circle", "opacity": 0.8},
                "encoding": {
                    "x": {"field": "x", "type": "quantitative"},
                    "y": {"field": "y", "type": "quantitative"},
                    "size": {"field": "value", "type": "quantitative", "title": title},
                    "tooltip": [{"field": "location"}, {"field": "value", "type": "quantitative"}],
                },
            }
        )
    out: dict[str, Any] = {"layer": layers, "width": 560, "height": 420}
    if params:
        out["params"] = params
    return out

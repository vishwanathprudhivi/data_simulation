"""Governed query engine: ``QuerySpec`` -> DuckDB over run outputs (FR-SEM-03/04/05).

Per metric, values are first computed per (variant, replication, dims...). Then,
unless replications are kept as a dimension, they are aggregated with Student-t
confidence intervals; comparisons give paired (CRN) or Welch differences vs a
baseline. Results are wide: one column per metric plus ``<metric>.ci_low``,
``<metric>.ci_high``, ``<metric>.n``, and for comparisons ``<metric>.diff`` etc.
Every result carries provenance and a stable ``ref_id`` (FR-SEM-04, FR-CHAT-03).
"""

from __future__ import annotations

import difflib
import math
import time
import warnings
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from services.semantic.metrics import EVENT_DIMENSIONS, FormulaError, parse_formula
from services.semantic.sources import ExperimentData
from simcore.catalog import metric_catalog
from simcore.core.hashing import content_hash
from simcore.core.plugins import DomainPlugin
from simcore.experiment.stats import mean_ci, paired_ci, welch_ci
from simcore.specs.domain import MetricDef
from simcore.specs.query import FilterSpec, QuerySpec

__all__ = ["QueryError", "QueryIssue", "QueryResult", "SemanticEngine"]

REPLICATION_DIMS = {"variant", "replication"}
INTERVAL_LIMIT = 20000


@dataclass(frozen=True)
class QueryIssue:
    path: str
    problem: str
    suggestion: str = ""
    allowed: Sequence[str] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "problem": self.problem,
            "suggestion": self.suggestion,
            "allowed": list(self.allowed),
        }


class QueryError(ValueError):
    """Structured, repair-ready query error (FR-VIZ-05)."""

    def __init__(self, issues: Sequence[QueryIssue]) -> None:
        self.issues = list(issues)
        super().__init__("; ".join(f"{i.path}: {i.problem}" for i in self.issues))


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[dict[str, Any]]
    provenance: dict[str, Any]
    ref_id: str
    metrics: list[MetricDef] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref_id": self.ref_id,
            "columns": self.columns,
            "rows": self.rows,
            "provenance": self.provenance,
            "warnings": self.warnings,
            "metrics": [m.model_dump(mode="json") for m in self.metrics],
        }

    def to_csv(self) -> str:
        import csv
        import io

        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=self.columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(self.rows)
        return buf.getvalue()


def _num(x: float) -> float | None:
    return None if x is None or math.isnan(x) or math.isinf(x) else float(x)


def _suggest(name: str, options: Sequence[str]) -> str:
    close = difflib.get_close_matches(name, list(options), n=1)
    return f"did you mean {close[0]!r}?" if close else ""


class SemanticEngine:
    """Answers ``QuerySpec``s for one experiment's outputs."""

    def __init__(
        self, data: ExperimentData, plugin: DomainPlugin, provenance: dict[str, Any] | None = None
    ) -> None:
        self.data = data
        self.plugin = plugin
        self.catalog = {m.id: m for m in metric_catalog(plugin).metrics}
        self.base_provenance = provenance or {}

    # ------------------------------------------------------------------ validation

    def validate(self, spec: QuerySpec) -> list[MetricDef]:
        issues: list[QueryIssue] = []
        metrics = []
        for n, mid in enumerate(spec.metrics):
            m = self.catalog.get(mid)
            if m is None:
                issues.append(
                    QueryIssue(
                        f"metrics[{n}]",
                        f"unknown metric {mid!r}",
                        _suggest(mid, list(self.catalog)),
                        sorted(self.catalog),
                    )
                )
                continue
            metrics.append(m)
            for dim in spec.dimensions:
                if dim not in m.allowed_dimensions and dim not in REPLICATION_DIMS:
                    issues.append(
                        QueryIssue(
                            "dimensions",
                            f"metric {mid!r} cannot be sliced by {dim!r}",
                            _suggest(dim, m.allowed_dimensions),
                            m.allowed_dimensions,
                        )
                    )
            if m.source == "event_log":
                try:
                    parse_formula(m.formula)
                except FormulaError as exc:
                    issues.append(QueryIssue(f"metrics[{n}]", str(exc)))
        if "time_bucket" in spec.dimensions and spec.time_bucket is None:
            issues.append(
                QueryIssue(
                    "time_bucket",
                    "dimension time_bucket needs query.time_bucket (seconds)",
                    "set time_bucket, e.g. 900",
                )
            )
        kinds = {m.source for m in metrics}
        if len(metrics) > 1 and any(m.formula == "state_intervals" for m in metrics):
            issues.append(
                QueryIssue(
                    "metrics",
                    "state_intervals cannot be combined with other metrics",
                    "query it alone",
                )
            )
        if kinds == {"kpi"}:
            extra = [d for d in spec.dimensions if d not in REPLICATION_DIMS]
            if extra:
                issues.append(
                    QueryIssue(
                        "dimensions",
                        f"replication-level KPIs only support variant and replication, not {extra}",
                        "use an event metric for finer slices",
                        sorted(REPLICATION_DIMS),
                    )
                )
        if spec.comparison is not None:
            variants = {
                r[0] for r in self.data.con.execute("SELECT DISTINCT variant FROM runs").fetchall()
            }
            if spec.comparison.baseline not in variants:
                issues.append(
                    QueryIssue(
                        "comparison.baseline",
                        f"unknown baseline {spec.comparison.baseline!r}",
                        _suggest(spec.comparison.baseline, sorted(variants)),
                        sorted(variants),
                    )
                )
        for n, f in enumerate(spec.filters):
            if f.dimension not in {*EVENT_DIMENSIONS, "time_bucket", "variant", "replication"}:
                issues.append(
                    QueryIssue(
                        f"filters[{n}].dimension",
                        f"cannot filter on {f.dimension!r}",
                        _suggest(f.dimension, list(EVENT_DIMENSIONS)),
                        sorted({*EVENT_DIMENSIONS, "time_bucket"}),
                    )
                )
        if issues:
            raise QueryError(issues)
        return metrics

    # ------------------------------------------------------------------ per-replication values

    def _filter_sql(
        self, filters: Sequence[FilterSpec], exprs: dict[str, str], params: list[Any]
    ) -> str:
        parts = []
        for f in filters:
            col = exprs.get(f.dimension)
            if col is None:
                continue
            vals = f.value if isinstance(f.value, list) else [f.value]
            if f.op in ("eq", "ne"):
                parts.append(f"{col} {'=' if f.op == 'eq' else '<>'} ?")
                params.append(vals[0])
            elif f.op in ("in", "not_in"):
                ph = ", ".join("?" for _ in vals)
                parts.append(f"{col} {'IN' if f.op == 'in' else 'NOT IN'} ({ph})")
                params.extend(vals)
            elif f.op in ("gte", "lte"):
                parts.append(f"{col} {'>=' if f.op == 'gte' else '<='} ?")
                params.append(vals[0])
            elif f.op == "between":
                parts.append(f"{col} BETWEEN ? AND ?")
                params.extend(vals[:2])
        return (" AND " + " AND ".join(parts)) if parts else ""

    def _scope_sql(self, spec: QuerySpec, params: list[Any]) -> str:
        if spec.scope.variants:
            params.extend(spec.scope.variants)
            return f" AND variant IN ({', '.join('?' for _ in spec.scope.variants)})"
        return ""

    def _kpi_values(self, m: MetricDef, spec: QuerySpec) -> list[dict[str, Any]]:
        params: list[Any] = [m.formula]
        where = self._scope_sql(spec, params)
        where += self._filter_sql(
            [f for f in spec.filters if f.dimension in REPLICATION_DIMS],
            {"variant": "variant", "replication": "replication"},
            params,
        )
        rows = self.data.con.execute(
            f"SELECT variant, replication, value FROM results WHERE metric = ?{where} "
            "ORDER BY variant, replication",
            params,
        ).fetchall()
        return [{"variant": v, "replication": r, "value": val} for v, r, val in rows]

    def _event_values(self, m: MetricDef, spec: QuerySpec) -> list[dict[str, Any]]:
        self.data.ensure_events()
        f = parse_formula(m.formula)
        dims = [d for d in spec.dimensions if d not in REPLICATION_DIMS]
        exprs = dict(EVENT_DIMENSIONS)
        bucket = spec.time_bucket
        if bucket:
            exprs["time_bucket"] = f"FLOOR(time / {float(bucket)!r}) * {float(bucket)!r}"
        params: list[Any] = []
        scope = self._scope_sql(spec, params)
        if f.kind == "state_duration":
            inner = (
                "SELECT variant, replication, agent, json_extract_string(data, '$.state') "
                "AS state, time, LEAD(time) OVER (PARTITION BY variant, replication, agent "
                "ORDER BY seq) AS t_next FROM events WHERE type = 'agent.state' "
                "AND agent IS NOT NULL" + scope
            )
            end = self.data.con.execute("SELECT MAX(time) FROM events").fetchone()[0]
            sel = ", ".join(
                (EVENT_DIMENSIONS["agent_type"] if d == "agent_type" else d) + f" AS {d}"
                for d in dims
            )
            fparams: list[Any] = []
            fw = self._filter_sql(
                spec.filters,
                {
                    "agent": "agent",
                    "state": "state",
                    "agent_type": EVENT_DIMENSIONS["agent_type"],
                    "variant": "variant",
                    "replication": "replication",
                },
                fparams,
            )
            sql = (
                f"SELECT variant, replication{', ' + sel if sel else ''}, "
                f"SUM(COALESCE(t_next, {float(end)!r}) - time) AS value FROM ({inner}) "
                f"WHERE 1=1{fw} GROUP BY ALL ORDER BY ALL"
            )
            rows = self.data.con.execute(sql, params + fparams).fetchall()
        elif f.kind == "state_intervals":
            inner = (
                "SELECT variant, replication, agent, json_extract_string(data, '$.state') "
                "AS state, time AS t_start, LEAD(time) OVER (PARTITION BY variant, "
                "replication, agent ORDER BY seq) AS t_end FROM events WHERE "
                "type = 'agent.state' AND agent IS NOT NULL" + scope
            )
            fparams = []
            fw = self._filter_sql(
                spec.filters,
                {
                    "agent": "agent",
                    "state": "state",
                    "variant": "variant",
                    "replication": "replication",
                },
                fparams,
            )
            rows = self.data.con.execute(
                f"SELECT variant, replication, agent, state, t_start, t_end FROM ({inner}) "
                f"WHERE t_end IS NOT NULL AND t_end > t_start{fw} ORDER BY agent, t_start LIMIT "
                f"{min(spec.limit, INTERVAL_LIMIT)}",
                params + fparams,
            ).fetchall()
            return [
                {
                    "variant": v,
                    "replication": r,
                    "agent": a,
                    "state": s,
                    "start": st,
                    "end": en,
                    "value": en - st,
                }
                for v, r, a, s, st, en in rows
            ]
        else:
            sel = ", ".join(f"{exprs[d]} AS {d}" for d in dims)
            params.append(f.event_type)
            fw = self._filter_sql(spec.filters, exprs, params)
            sql = (
                f"SELECT variant, replication{', ' + sel if sel else ''}, {f.value_sql()} "
                f"AS value FROM events WHERE 1=1{scope} AND type = ?{fw} GROUP BY ALL "
                "ORDER BY ALL"
            )
            rows = self.data.con.execute(sql, params).fetchall()
        cols = ["variant", "replication", *dims, "value"]
        out = [dict(zip(cols, r)) for r in rows]
        if f.kind == "agg" and f.agg in ("count", "sum"):
            out = self._zero_fill(out, dims, spec)
        return out

    def _zero_fill(
        self, rows: list[dict[str, Any]], dims: list[str], spec: QuerySpec
    ) -> list[dict[str, Any]]:
        """Missing (variant, replication, dims) combinations are zero, not absent."""
        if not dims:
            return rows
        reps = self.data.con.execute(
            "SELECT DISTINCT variant, replication FROM runs WHERE status = 'ok' ORDER BY ALL"
        ).fetchall()
        if spec.scope.variants:
            reps = [r for r in reps if r[0] in spec.scope.variants]
        levels = {d: sorted({r[d] for r in rows if r[d] is not None}, key=str) for d in dims}
        if "time_bucket" in dims and spec.time_bucket:
            end = self.data.con.execute("SELECT MAX(time) FROM events").fetchone()[0] or 0.0
            b = float(spec.time_bucket)
            levels["time_bucket"] = [i * b for i in range(int(end // b) + 1)]
        have = {(r["variant"], r["replication"], *(r[d] for d in dims)): r for r in rows}
        out = []
        import itertools

        for v, rep in reps:
            for combo in itertools.product(*(levels[d] for d in dims)):
                key = (v, rep, *combo)
                out.append(
                    have.get(key)
                    or {"variant": v, "replication": rep, **dict(zip(dims, combo)), "value": 0.0}
                )
        return out

    # ------------------------------------------------------------------ public

    def query(self, spec: QuerySpec | dict[str, Any]) -> QueryResult:
        if isinstance(spec, dict):
            spec = QuerySpec.model_validate(spec)
        metrics = self.validate(spec)
        keep_reps = "replication" in spec.dimensions or not spec.aggregate_replications
        group_dims = [d for d in spec.dimensions if d not in ("replication",)]
        if "variant" not in group_dims:
            group_dims = ["variant", *group_dims]
        merged: dict[tuple[Any, ...], dict[str, Any]] = {}
        notes: list[str] = []
        for m in metrics:
            per_rep = (
                self._kpi_values(m, spec) if m.source == "kpi" else self._event_values(m, spec)
            )
            if m.formula == "state_intervals":
                rows = per_rep[: spec.limit]
                interval_cols = [
                    "variant",
                    "replication",
                    "agent",
                    "state",
                    "start",
                    "end",
                    "value",
                ]
                return self._finish(spec, metrics, interval_cols, rows, notes)
            if keep_reps:
                for r in per_rep:
                    key = tuple(r.get(d) for d in (*group_dims, "replication"))
                    row = merged.setdefault(
                        key, {d: r.get(d) for d in (*group_dims, "replication")}
                    )
                    row[m.id] = _num(r["value"])
                continue
            groups: dict[tuple[Any, ...], dict[int, float]] = defaultdict(dict)
            for r in per_rep:
                groups[tuple(r.get(d) for d in group_dims)][int(r["replication"])] = (
                    float(r["value"]) if r["value"] is not None else math.nan
                )
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                for key, vals in groups.items():
                    ci = mean_ci(list(vals.values()), spec.confidence)
                    row = merged.setdefault(key, dict(zip(group_dims, key)))
                    row[m.id] = _num(ci.mean)
                    row[f"{m.id}.ci_low"] = _num(ci.low)
                    row[f"{m.id}.ci_high"] = _num(ci.high)
                    row[f"{m.id}.n"] = ci.n
                    if ci.n < 5:
                        notes.append(f"{m.id}: only {ci.n} replication(s) for {key}")
                    if spec.comparison is not None:
                        self._compare(spec, m, key, group_dims, groups, row)
        cols: list[str] = list(group_dims) + (["replication"] if keep_reps else [])
        for m in metrics:
            cols.append(m.id)
            if not keep_reps:
                cols += [f"{m.id}.ci_low", f"{m.id}.ci_high", f"{m.id}.n"]
                if spec.comparison is not None:
                    cols += [
                        f"{m.id}.diff",
                        f"{m.id}.diff_ci_low",
                        f"{m.id}.diff_ci_high",
                        f"{m.id}.ci_excludes_zero",
                    ]
        rows = list(merged.values())
        for s in reversed(spec.sort):

            def sort_key(r: dict[str, Any], f: str = s.field) -> tuple[bool, Any]:
                return (r.get(f) is None, r.get(f))

            rows.sort(key=sort_key, reverse=s.descending)
        return self._finish(spec, metrics, cols, rows[: spec.limit], sorted(set(notes)))

    def _compare(
        self,
        spec: QuerySpec,
        m: MetricDef,
        key: tuple[Any, ...],
        group_dims: list[str],
        groups: dict[tuple[Any, ...], dict[int, float]],
        row: dict[str, Any],
    ) -> None:
        assert spec.comparison is not None
        vi = group_dims.index("variant")
        base_key = (*key[:vi], spec.comparison.baseline, *key[vi + 1 :])
        base, mine = groups.get(base_key, {}), groups[key]
        if spec.comparison.kind == "paired_vs_baseline":
            common = sorted(set(base) & set(mine))
            diff = paired_ci([base[r] for r in common], [mine[r] for r in common], spec.confidence)
        else:
            diff = welch_ci(list(base.values()), list(mine.values()), spec.confidence)
        row[f"{m.id}.diff"] = _num(diff.mean)
        row[f"{m.id}.diff_ci_low"] = _num(diff.low)
        row[f"{m.id}.diff_ci_high"] = _num(diff.high)
        row[f"{m.id}.ci_excludes_zero"] = bool(diff.excludes_zero) if diff.n >= 2 else False

    def _finish(
        self,
        spec: QuerySpec,
        metrics: list[MetricDef],
        cols: list[str],
        rows: list[dict[str, Any]],
        notes: list[str],
    ) -> QueryResult:
        runs = self.data.con.execute(
            "SELECT variant, replication, master_seed, config_hash "
            "FROM runs WHERE status = 'ok' ORDER BY ALL"
        ).fetchall()
        used = {v for v, *_ in runs if not spec.scope.variants or v in spec.scope.variants}
        prov = {
            **self.base_provenance,
            "experiment_id": self.data.summary.get("experiment_id"),
            "variants": {v: h for v, _, _, h in runs if v in used},
            "seeds": {f"{v}/{r}": s for v, r, s, _ in runs if v in used},
            "domain": [self.plugin.name, self.plugin.version],
            "engine_version": self.data.summary.get("provenance", {}).get("engine_version"),
            "git_sha": self.data.summary.get("provenance", {}).get("git_sha"),
            "mode": self.data.summary.get("mode"),
            "confidence": spec.confidence,
            "data_digest": self.data.digest,
            "query": spec.model_dump(mode="json"),
            "computed_at": time.time(),
        }
        ref = "q_" + content_hash(
            {"q": prov["query"], "d": prov["data_digest"], "x": prov.get("job_id")}, length=10
        )
        return QueryResult(cols, rows, prov, ref, metrics, notes)

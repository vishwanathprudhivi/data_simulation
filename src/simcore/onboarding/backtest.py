"""Backtest and Validation Report (FR-DOM-06, AC-DOM-01/02).

Each observed period becomes one variant: the base scenario with the period's
length, its observed lever values (e.g. staffing) and its observed arrival rates,
started from an empty system. The simulated KPIs (replication mean, CI and
prediction interval) are compared with what was observed. The report rates every
KPI pass / warn / fail, explains failures with specific diagnostics (bias,
station-level utilization and capacity mismatches, data-quality problems) and
derives the calibrated envelope from the observed lever ranges.
"""

from __future__ import annotations

import itertools
import json
import math
import os
import statistics
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from scipy import stats as sstats

from simcore.catalog import apply_levers, metric_catalog
from simcore.core.config import ConfigError, ScenarioBundle, load_scenario
from simcore.core.plugins import DomainPlugin, PluginRegistry
from simcore.experiment.runner import ExperimentRunner
from simcore.onboarding.inputs import InputModel
from simcore.onboarding.mapping import DQReport, MappedData, Period
from simcore.specs.domain import DomainDefinition, EnvelopeBound
from simcore.specs.experiment import ExperimentSpec, VariantSpec
from simcore.specs.mapping import DataMappingSpec

__all__ = ["KpiCheck", "ValidationReport", "backtest", "plugin_for_definition"]

RATINGS = ("pass", "warn", "fail")


@dataclass
class PeriodResult:
    period: str
    observed: float
    mean: float
    ci: tuple[float, float]
    pi: tuple[float, float]
    n: int

    @property
    def error(self) -> float:
        return (self.mean - self.observed) / self.observed if self.observed else math.nan

    @property
    def covered(self) -> bool:
        return self.pi[0] <= self.observed <= self.pi[1]


@dataclass
class KpiCheck:
    metric: str
    periods: list[PeriodResult]
    rating: str = "pass"

    @property
    def mape(self) -> float:
        errs = [abs(p.error) for p in self.periods if not math.isnan(p.error)]
        return statistics.fmean(errs) if errs else math.nan

    @property
    def bias(self) -> float:
        errs = [p.error for p in self.periods if not math.isnan(p.error)]
        return statistics.fmean(errs) if errs else math.nan

    @property
    def coverage(self) -> float:
        return sum(p.covered for p in self.periods) / max(len(self.periods), 1)

    @property
    def z_ks_pvalue(self) -> float:
        """KS test of standardized residuals against N(0, 1) (distributional check)."""
        z = []
        for p in self.periods:
            half = (p.pi[1] - p.pi[0]) / 2
            if half > 0:
                z.append((p.observed - p.mean) / (half / 1.96))
        return float(sstats.kstest(z, "norm").pvalue) if len(z) >= 3 else math.nan


@dataclass
class ValidationReport:
    domain: str
    domain_version: str
    rating: str
    kpis: list[KpiCheck]
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    envelope: list[EnvelopeBound] = field(default_factory=list)
    dq: DQReport | None = None
    inputs: InputModel | None = None
    replications: int = 0
    periods: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.rating != "fail"

    def to_dict(self) -> dict[str, Any]:
        def num(x: float) -> float | None:
            return None if math.isnan(x) or math.isinf(x) else x

        return {
            "domain": self.domain,
            "domain_version": self.domain_version,
            "rating": self.rating,
            "replications": self.replications,
            "periods": self.periods,
            "kpis": [
                {
                    "metric": k.metric,
                    "rating": k.rating,
                    "mape": num(k.mape),
                    "bias": num(k.bias),
                    "coverage": k.coverage,
                    "residual_ks_pvalue": num(k.z_ks_pvalue),
                    "periods": [
                        {
                            "period": p.period,
                            "observed": p.observed,
                            "simulated_mean": p.mean,
                            "ci": list(p.ci),
                            "prediction_interval": list(p.pi),
                            "error": num(p.error),
                            "covered": p.covered,
                            "n": p.n,
                        }
                        for p in k.periods
                    ],
                }
                for k in self.kpis
            ],
            "diagnostics": self.diagnostics,
            "envelope": [b.model_dump(mode="json") for b in self.envelope],
            "data_quality": self.dq.to_dict() if self.dq else None,
            "inputs": [i.to_dict() for i in self.inputs.inputs] if self.inputs else [],
        }

    def markdown(self) -> str:
        badge = {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}[self.rating]
        lines = [
            f"# Validation Report — {self.domain} {self.domain_version}",
            "",
            f"**Overall: {badge}** · {len(self.periods)} historical periods · "
            f"{self.replications} replications each",
            "",
            "## KPI backtest",
            "",
            "| KPI | Rating | Mean abs. error | Bias | Coverage (95% PI) |",
            "|---|---|---|---|---|",
        ]
        for k in self.kpis:
            lines.append(
                f"| {k.metric} | {k.rating.upper()} | {k.mape:.1%} | {k.bias:+.1%} | "
                f"{k.coverage:.0%} |"
            )
        for k in self.kpis:
            lines += [
                "",
                f"### {k.metric} by period",
                "",
                "| Period | Observed | Simulated (mean) | 95% PI | Error |",
                "|---|---|---|---|---|",
            ]
            for p in k.periods:
                lines.append(
                    f"| {p.period} | {p.observed:.4g} | {p.mean:.4g} | "
                    f"{p.pi[0]:.4g} to {p.pi[1]:.4g} | {p.error:+.1%}"
                    f"{'' if p.covered else ' ✗'} |"
                )
        lines += ["", "## Findings and actions", ""]
        if not self.diagnostics:
            lines.append("No problems found.")
        for d in self.diagnostics:
            lines.append(f"- **[{d['severity'].upper()}] {d['title']}** — {d['detail']}")
            if d.get("action"):
                lines.append(f"  - Action: {d['action']}")
        lines += ["", "## Calibrated envelope", ""]
        if self.envelope:
            for b in self.envelope:
                lines.append(f"- {b.lever}: {b.min:g} to {b.max:g} (range observed in the data)")
        else:
            lines.append("None: the backtest failed, so the model is not trusted anywhere yet.")
        if self.inputs:
            lines += ["", self.inputs.markdown()]
        if self.dq:
            lines += ["", self.dq.markdown()]
        return "\n".join(lines) + "\n"

    def write(self, out: Path, fitted: DomainDefinition | None = None) -> dict[str, Path]:
        out.mkdir(parents=True, exist_ok=True)
        files = {"json": out / "validation_report.json", "md": out / "validation_report.md"}
        files["json"].write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n")
        files["md"].write_text(self.markdown())
        if fitted is not None:
            doc = fitted.model_dump(mode="json", by_alias=True, exclude_none=True)
            if self.envelope:
                doc["envelope"] = [
                    b.model_dump(mode="json", exclude_none=True) for b in self.envelope
                ]
            files["domain"] = out / "domain.fitted.yaml"
            files["domain"].write_text(
                "# DRAFT written by `simcore domain backtest`; review, then `simcore domain "
                "publish`.\n" + yaml.safe_dump(doc, sort_keys=False)
            )
        return files


def plugin_for_definition(definition: DomainDefinition, workdir: Path) -> PluginRegistry:
    """A registry holding only this (Tier-0) definition, loaded from a temp copy."""
    path = workdir / definition.id / "domain.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            definition.model_dump(mode="json", by_alias=True, exclude_none=True), sort_keys=False
        )
    )
    reg = PluginRegistry()
    reg.load_tier0(path)
    return reg


def _intervals(
    values: list[float], confidence: float = 0.95
) -> tuple[float, tuple[float, float], tuple[float, float]]:
    n = len(values)
    mean = statistics.fmean(values)
    if n < 2:
        return mean, (mean, mean), (mean, mean)
    sd = statistics.stdev(values)
    t = float(sstats.t.ppf(0.5 + confidence / 2, n - 1))
    ci = (mean - t * sd / math.sqrt(n), mean + t * sd / math.sqrt(n))
    pi = (mean - t * sd * math.sqrt(1 + 1 / n), mean + t * sd * math.sqrt(1 + 1 / n))
    return mean, ci, pi


def _overlap(spans: list[tuple[float, float]], a: float, b: float) -> float:
    return sum(max(0.0, min(e, b) - max(s, a)) for s, e in spans)


def _period_overrides(
    p: Period,
    spec: DataMappingSpec,
    data: MappedData,
    base: ScenarioBundle,
    plugin: DomainPlugin,
) -> dict[str, Any]:
    bt = spec.backtest
    assert bt is not None
    values: dict[str, Any] = {}
    for lever, col in bt.levers.items():
        v = p.values.get(col)
        if v is not None:
            values[lever] = int(v) if float(v).is_integer() else v
    overrides = dict(apply_levers(base, plugin, values).overrides) if values else {}
    for sku, path in bt.arrival_paths.items():
        times = [t for t in data.arrivals.get(sku, []) if p.start <= t < p.end]
        gaps = [b - a for a, b in itertools.pairwise(times) if b > a]
        if len(gaps) >= 5:
            overrides[path] = statistics.fmean(gaps)
    overrides["duration"] = p.duration
    overrides["warmup"] = 0
    return overrides


def backtest(
    definition: DomainDefinition,
    spec: DataMappingSpec,
    mapping_dir: Path,
    data: MappedData,
    dq: DQReport,
    *,
    inputs: InputModel | None = None,
    replications: int | None = None,
    plugins: PluginRegistry | None = None,
    parallelism: int | None = None,
) -> ValidationReport:
    """Run the backtest gate and build the Validation Report."""
    bt = spec.backtest
    if bt is None:
        raise ConfigError([])  # pragma: no cover - guarded by the CLI
    reps = replications or bt.replications
    diagnostics: list[dict[str, Any]] = []

    def diag(sev: str, title: str, detail: str, action: str = "") -> None:
        diagnostics.append({"severity": sev, "title": title, "detail": detail, "action": action})

    for issue in dq.issues:
        if issue.severity == "error":
            diag(
                "fail", f"data quality: {issue.check} in {issue.source}", issue.message, issue.hint
            )
    if inputs is not None:
        for fi in inputs.inputs:
            if fi.chosen == "empirical":
                diag(
                    "warn",
                    f"input {fi.target} uses the empirical distribution",
                    fi.note,
                    "inspect the samples for mixtures (shifts, products) before relying on "
                    "tail behaviour",
                )
    if not data.periods:
        diag(
            "fail",
            "no historical periods",
            "the periods source produced no rows",
            f"check source {bt.periods!r} in mapping.yaml",
        )
        return ValidationReport(
            definition.id,
            definition.version,
            "fail",
            [],
            diagnostics,
            dq=dq,
            inputs=inputs,
            replications=reps,
        )

    with tempfile.TemporaryDirectory(prefix="simcore-backtest-") as tmp:
        if plugins is None:
            if definition.tier == 0:
                plugins = plugin_for_definition(definition, Path(tmp))
            else:
                from simcore.api.engine import default_plugins

                plugins = default_plugins()
        plugin = plugins.get(definition.id)
        base = load_scenario(mapping_dir / bt.base_scenario)
        if base.scenario.domain != definition.id:
            raise ValueError(
                f"base scenario is for domain {base.scenario.domain!r}, not {definition.id!r}"
            )
        base = base.with_overrides({"domain_version": definition.version})
        variants = [
            VariantSpec(
                id=f"p{i:03d}", name=p.id, overrides=_period_overrides(p, spec, data, base, plugin)
            )
            for i, p in enumerate(data.periods)
        ]
        catalog = {m.id: m for m in metric_catalog(plugin).metrics}
        formulas = {}
        for metric in bt.kpis:
            if metric not in catalog:
                raise ValueError(f"backtest kpi {metric!r} is not a metric of {definition.id}")
            formulas[metric] = catalog[metric].formula
        exp = ExperimentSpec(
            id="backtest",
            base_scenario=base.source,
            include_baseline=False,
            design="explicit",
            variants=variants,
            replications=reps,
            crn=True,
            parallelism=parallelism or max(1, min(4, os.cpu_count() or 1)),
        )
        result = ExperimentRunner(exp, base, plugins).run()

    by_variant: dict[str, list[dict[str, float]]] = {}
    for o in result.outcomes:
        if o.ok:
            by_variant.setdefault(o.variant_id, []).append(o.kpis)
    failed = [o for o in result.outcomes if not o.ok]
    if failed:
        diag(
            "fail",
            f"{len(failed)} backtest replications failed",
            str(failed[0].error),
            "run `simcore run` on the base scenario to see the error",
        )

    checks: list[KpiCheck] = []
    for metric, col in bt.kpis.items():
        rows = []
        for v, p in zip(variants, data.periods):
            vals = [k[formulas[metric]] for k in by_variant.get(v.id, []) if formulas[metric] in k]
            obs = p.values.get(col)
            if not vals or obs is None:
                continue
            mean, ci, pi = _intervals(vals)
            rows.append(PeriodResult(p.id, obs, mean, ci, pi, len(vals)))
        check = KpiCheck(metric, rows)
        tol = bt.tolerance
        if not rows:
            check.rating = "fail"
        elif check.mape <= tol.pass_error and check.coverage >= tol.pass_coverage:
            check.rating = "pass"
        elif check.mape <= tol.warn_error and check.coverage >= tol.warn_coverage:
            check.rating = "warn"
        else:
            check.rating = "fail"
        checks.append(check)
        if check.rating != "pass" and rows:
            same_sign = all(r.error > 0 for r in rows) or all(r.error < 0 for r in rows)
            direction = "over" if check.bias > 0 else "under"
            diag(
                check.rating,
                f"{metric}: mean absolute error {check.mape:.1%}, coverage {check.coverage:.0%}",
                f"the model {direction}-predicts {metric} "
                + ("in every period" if same_sign else "on average")
                + f" (bias {check.bias:+.1%}; tolerance {tol.pass_error:.0%} to pass, "
                f"{tol.warn_error:.0%} to warn)",
                "see the station diagnostics below for the likely cause"
                if same_sign
                else "variation between periods is not explained: check period-specific inputs "
                "(staffing, product mix, arrivals) in the mapping",
            )

    _station_diagnostics(definition, data, variants, by_variant, diag)
    diagnostics.sort(key=lambda d: {"fail": 0, "warn": 1}.get(d["severity"], 2))

    worst = max((RATINGS.index(c.rating) for c in checks), default=2)
    if any(d["severity"] == "fail" for d in diagnostics):
        worst = 2
    rating = RATINGS[worst]
    envelope: list[EnvelopeBound] = []
    if rating != "fail":
        for lever, col in sorted(bt.levers.items()):
            seen = [p.values[col] for p in data.periods if col in p.values]
            if seen:
                envelope.append(EnvelopeBound(lever=lever, min=min(seen), max=max(seen)))
    return ValidationReport(
        definition.id,
        definition.version,
        rating,
        checks,
        diagnostics,
        envelope,
        dq,
        inputs,
        reps,
        [p.id for p in data.periods],
    )


def _station_diagnostics(
    definition: DomainDefinition,
    data: MappedData,
    variants: list[VariantSpec],
    by_variant: dict[str, list[dict[str, float]]],
    diag: Any,
) -> None:
    """Compare simulated and observed station utilization and parallel capacity."""
    if definition.process is None:
        return
    for st in definition.process.stations:
        seen = data.servers.get(st.id)
        if seen and len(seen) != st.capacity:
            diag(
                "fail" if len(seen) > st.capacity else "warn",
                f"station {st.id}: capacity {st.capacity} in domain.yaml, "
                f"{len(seen)} parallel servers in the data",
                f"the event data shows servers {sorted(seen)} working at {st.id}",
                f"set `capacity: {len(seen)}` for station {st.id} in domain.yaml "
                "(or fix the server column mapping)",
            )
        sizes = data.batch.get(st.id)
        if sizes:
            typical = statistics.median(sizes)
            if round(typical) != st.batch_size:
                diag(
                    "fail",
                    f"station {st.id}: batch_size {st.batch_size} in domain.yaml, "
                    f"{typical:g} items per service in the data",
                    f"median items per service at {st.id} over {len(sizes)} services",
                    f"set `batch_size: {round(typical)}` for station {st.id} in domain.yaml",
                )
        spans = data.busy.get(st.id)
        if not spans:
            continue
        sim_u, obs_u = [], []
        servers = max(1, len(seen or ()))
        for v, p in zip(variants, data.periods):
            runs = by_variant.get(v.id, [])
            key = f"station_{st.id}_utilization"
            vals = [k[key] for k in runs if key in k]
            if vals:
                sim_u.append(statistics.fmean(vals))
                obs_u.append(_overlap(spans, p.start, p.end) / (p.duration * servers))
        if not sim_u:
            continue
        s, o = statistics.fmean(sim_u), statistics.fmean(obs_u)
        if abs(s - o) > 0.15:
            more = s > o
            diag(
                "warn",
                f"station {st.id}: simulated utilization {s:.0%} vs observed {o:.0%}",
                f"averaged over {len(sim_u)} periods; the model keeps {st.id} "
                f"{'busier' if more else 'less busy'} than the data does",
                (
                    f"check capacity, batch_size and service_time of {st.id}"
                    if more
                    else f"check whether {st.id} is starved or blocked upstream; "
                    "compare buffer capacities with the line"
                ),
            )

"""Deterministic ``Findings`` synthesis and guarded narrative (FR-SCG-11, FR-CHAT-03/04, BRD 7.11).

Findings come from statistics code only. The narrative is generated *from the
Findings object*: every number it states is a Findings field with a ``ref_id``,
and its language is gated by the flags (an "improvement" is only claimed when the
paired CI excludes zero and the direction is good; replication count, confidence
and fidelity are always stated; extrapolated variants are called out).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from simcore.core.hashing import content_hash
from simcore.specs.domain import MetricDef
from simcore.specs.findings import (
    Comparison,
    Driver,
    Estimate,
    FidelitySummary,
    Findings,
)

__all__ = ["Narrative", "narrate", "synthesize_findings"]


def _est(d: Mapping[str, Any]) -> Estimate | None:
    lo, hi = d["ci"]
    if d.get("mean") is None or lo is None or hi is None:
        return None
    return Estimate(mean=float(d["mean"]), ci=(float(lo), float(hi)))


def ref_for(experiment_id: str, variant: str, metric: str, kind: str = "cmp") -> str:
    """Stable reference id of a Findings field (what a narrative number points to)."""
    return f"f_{content_hash([experiment_id, variant, metric, kind], length=10)}"


def _compare(
    src: Mapping[str, Any],
    mode: str,
    screen: bool,
    exp_id: str,
    reference: str,
    headline: Sequence[str],
    extrapolated: Mapping[str, bool],
    out: list[Comparison],
    effects: dict[str, float],
) -> None:
    by_variant_metric = {(s["variant"], s["metric"]): s for s in src["summaries"]}
    for c in src["comparisons"]:
        diff = _est(c["diff"])
        if diff is None:
            continue
        excludes = bool(c["ci_excludes_zero"])
        metric, variant = str(c["metric"]), str(c["variant"])
        half = (diff.ci[1] - diff.ci[0]) / 2
        if screen and half > 0:
            effects[metric] = max(effects.get(metric, 0.0), abs(diff.mean) / half)
        if metric not in headline:
            continue
        base = by_variant_metric.get((reference, metric))
        mine = by_variant_metric.get((variant, metric))
        out.append(
            Comparison(
                variant=variant,
                metric=metric,
                baseline=_est(base) if base else None,
                variant_estimate=_est(mine) if mine else None,
                paired_diff=diff,
                paired=bool(c["paired"]),
                ci_excludes_zero=excludes,
                significant=excludes,
                extrapolated=bool(extrapolated.get(variant, False)),
                fidelity=mode,
                ref_id=ref_for(exp_id, variant, metric, "cmp" if screen else f"cmp_{mode}"),
            )
        )


def synthesize_findings(
    summary: Mapping[str, Any],
    catalog: Mapping[str, MetricDef],
    *,
    headline: Sequence[str] | None = None,
    extrapolated: Mapping[str, bool] | None = None,
    fidelity: Mapping[str, Any] | None = None,
) -> Findings:
    """Build ``Findings`` from an experiment ``summary.json`` structure.

    ``headline`` metrics (default: catalog metrics present in the summary with a
    direction of good) become comparisons; the others are ranked as drivers by the
    size of their standardized paired effect.
    """
    exp_id = str(summary["experiment_id"])
    reference = str(summary["reference"])
    extrapolated = dict(extrapolated or {})
    metrics = list(summary["metrics"])
    if headline is None:
        headline = [m for m in metrics if m in catalog and catalog[m].direction != "neutral"]
        headline = headline or metrics[:3]
    fidelity = dict(fidelity if fidelity is not None else summary.get("fidelity") or {})
    mode = str(summary.get("mode", "logical"))
    comparisons: list[Comparison] = []
    effects: dict[str, float] = {}
    sources: list[tuple[str, Mapping[str, Any], bool]] = [(mode, summary, True)]
    if fidelity.get("promoted_summary") and fidelity.get("promoted_mode"):
        sources.append((str(fidelity["promoted_mode"]), fidelity["promoted_summary"], False))
    for src_mode, src, screen in sources:
        _compare(
            src, src_mode, screen, exp_id, reference, headline, extrapolated, comparisons, effects
        )
    drivers_raw = sorted(
        ((m, e) for m, e in effects.items() if m not in headline and e > 1.0),
        key=lambda x: (-x[1], x[0]),
    )[:5]
    total = sum(e for _, e in drivers_raw) or 1.0
    drivers = [
        Driver(
            metric=m, share_of_delta=round(e / total, 4), ref_id=ref_for(exp_id, "*", m, "driver")
        )
        for m, e in drivers_raw
    ]
    reps = summary.get("replications_ok", {})
    n = min(reps.values()) if reps else int(summary.get("replications_planned", 1))
    caveats: list[str] = []
    warnings: list[str] = list(summary.get("warnings", []))
    if n < 5:
        caveats.append(
            f"only {n} successful replications per alternative; intervals are wide and not reliable"
        )
    if summary.get("failures"):
        caveats.append(f"{len(summary['failures'])} replication(s) failed and were excluded")
    for v, flag in sorted(extrapolated.items()):
        if flag:
            caveats.append(
                f"{v} lies outside the calibrated envelope; its results are extrapolated"
            )
    if not summary.get("crn", True):
        caveats.append("alternatives used independent seeds (no CRN); differences are unpaired")
    warm = summary.get("warmup")
    if warm and warm.get("suggested") is not None and warm["suggested"] > warm["configured"]:
        caveats.append("Welch/MSER diagnostics suggest a longer warm-up than configured")
    fid = FidelitySummary(
        screen=mode,
        **{k: v for k, v in fidelity.items() if k in ("promoted", "promoted_mode") and v},
    )
    return Findings(
        experiment_id=exp_id,
        baseline=reference,
        fidelity=fid,
        n_replications=max(1, int(n)),
        confidence=float(summary.get("confidence", 0.95)),
        comparisons=comparisons,
        drivers=drivers,
        caveats=caveats,
        warnings=warnings,
    )


# ----------------------------------------------------------------------------- narrative


@dataclass(frozen=True)
class Narrative:
    """Text with ``{{ref_id:field}}`` placeholders plus the rendered version."""

    template: str
    text: str
    refs: dict[str, dict[str, Any]]


def _fmt(value: float, metric: MetricDef | None) -> str:
    if metric is not None and metric.format.kind == "percent":
        return f"{value * 100:.{metric.format.decimals}f}%"
    if abs(value) >= 100:
        return f"{value:,.0f}"
    return f"{value:.3g}"


def narrate(findings: Findings, catalog: Mapping[str, MetricDef]) -> Narrative:
    """Deterministic narrative obeying the Findings flags (FR-CHAT-04)."""
    refs: dict[str, dict[str, Any]] = {}
    lines: list[str] = []
    conf = round(findings.confidence * 100)

    def put(ref: str, field: str, value: float, metric: MetricDef | None) -> str:
        refs.setdefault(ref, {})[field] = value
        refs[ref][f"{field}__text"] = _fmt(value, metric)
        return f"{{{{{ref}:{field}}}}}"

    lines.append(
        f"Compared with {findings.baseline} over {findings.n_replications} "
        f"{'paired ' if all(c.paired for c in findings.comparisons) else ''}"
        f"replications ({conf}% confidence, {findings.fidelity.screen} mode):"
    )
    for c in findings.comparisons:
        m = catalog.get(c.metric)
        name = m.display_name if m else c.metric
        d = c.paired_diff
        direction = m.direction if m else "neutral"
        better = (d.mean < 0) if direction == "lower_is_better" else (d.mean > 0)
        mean = put(c.ref_id, "diff", d.mean, m)
        lo = put(c.ref_id, "ci_low", d.ci[0], m)
        hi = put(c.ref_id, "ci_high", d.ci[1], m)
        if not c.ci_excludes_zero:
            verdict = (
                f"shows no statistically significant change in {name} (difference "
                f"{mean}, {conf}% CI {lo} to {hi} includes zero)"
            )
        elif direction == "neutral":
            verdict = f"changes {name} by {mean} ({conf}% CI {lo} to {hi})"
        else:
            word = "improves" if better else "worsens"
            verdict = f"{word} {name}: change {mean} ({conf}% CI {lo} to {hi})"
        extra = (
            " This variant is outside the calibrated envelope, so treat it as extrapolated."
            if c.extrapolated
            else ""
        )
        label = f" [{c.fidelity}]" if findings.fidelity.promoted else ""
        lines.append(f"- {c.variant}{label} {verdict}.{extra}")
    if findings.drivers:
        names = ", ".join(
            (catalog[d.metric].display_name if d.metric in catalog else d.metric)
            for d in findings.drivers[:3]
        )
        lines.append(f"Largest accompanying changes: {names}.")
    for cav in findings.caveats:
        lines.append(f"Caveat: {cav}.")
    template = "\n".join(lines)
    text = template
    for ref, fields in refs.items():
        for field, value in fields.items():
            if field.endswith("__text"):
                text = text.replace(f"{{{{{ref}:{field[:-6]}}}}}", str(value))
    return Narrative(
        template,
        text,
        {k: {f: v for f, v in fv.items() if not f.endswith("__text")} for k, fv in refs.items()},
    )

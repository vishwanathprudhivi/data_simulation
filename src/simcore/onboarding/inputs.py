"""Auto input modeling (FR-DOM-05): fit distributions to mapped data and write them
into a *draft* domain definition with goodness-of-fit evidence.

The draft gets a bumped patch version and is written next to the report; it is
never published automatically (``simcore domain publish`` is a human step).
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from typing import Any

from pydantic import TypeAdapter

from simcore.experiment.fitting import FitReport, fit_distributions
from simcore.onboarding.mapping import MappedData
from simcore.specs.distributions import DistributionSpec
from simcore.specs.domain import DomainDefinition

__all__ = ["FittedInput", "InputModel", "apply_inputs", "fit_inputs"]

MIN_SAMPLES = 30
KS_REJECT = 0.01


@dataclass
class FittedInput:
    target: str  # e.g. "filler.service_time"
    n: int
    spec: dict[str, Any]
    chosen: str
    report: FitReport | None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        best = self.report.best if self.report else None
        return {
            "target": self.target,
            "n": self.n,
            "chosen": self.chosen,
            "spec": self.spec,
            "note": self.note,
            "ks_pvalue": best.ks_pvalue if best else None,
            "aic": best.aic if best else None,
            "summary": self.report.summary if self.report else None,
        }


@dataclass
class InputModel:
    inputs: list[FittedInput] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)

    def get(self, target: str) -> FittedInput | None:
        return next((i for i in self.inputs if i.target == target), None)

    def markdown(self) -> str:
        lines = [
            "## Input models",
            "",
            "| Input | n | Chosen | Fit | KS p | Mean (s) | CV |",
            "|---|---|---|---|---|---|---|",
        ]
        for i in self.inputs:
            best = i.report.best if i.report else None
            s = i.report.summary if i.report else {}
            if best is None:
                lines.append(f"| {i.target} | {i.n} | {i.chosen} | - | - | - | - |")
                continue
            mean, cv = s.get("mean", math.nan), s.get("cv", math.nan)
            lines.append(
                f"| {i.target} | {i.n} | {i.chosen} | {best.name} | {best.ks_pvalue:.3f} | "
                f"{mean:.4g} | {cv:.3g} |"
            )
        if self.skipped:
            lines.append("")
        for t, why in sorted(self.skipped.items()):
            lines.append(f"- {t}: kept the domain default ({why})")
        return "\n".join(lines) + "\n"


def _fit(target: str, values: list[float], model: InputModel) -> None:
    if len(values) < MIN_SAMPLES:
        model.skipped[target] = f"only {len(values)} samples (need {MIN_SAMPLES})"
        return
    rep = fit_distributions(values)
    best = rep.best
    if best.ks_pvalue < KS_REJECT:
        model.inputs.append(
            FittedInput(
                target,
                rep.n,
                rep.empirical,
                "empirical",
                rep,
                f"best parametric fit ({best.name}) rejected by KS at {KS_REJECT}",
            )
        )
    else:
        model.inputs.append(FittedInput(target, rep.n, dict(best.spec), best.name, rep))


def fit_inputs(data: MappedData, definition: DomainDefinition) -> InputModel:
    """Fit service times, repair times, times between failures and arrivals."""
    model = InputModel()
    stations = {s.id: s for s in (definition.process.stations if definition.process else [])}
    for sid in sorted(stations):
        if sid in data.service_times:
            _fit(f"{sid}.service_time", data.service_times[sid], model)
        else:
            model.skipped[f"{sid}.service_time"] = "no station events in the data"
        if stations[sid].failures is not None:
            if sid in data.repairs:
                _fit(f"{sid}.mttr", data.repairs[sid], model)
            if sid in data.uptimes:
                _fit(f"{sid}.mtbf", data.uptimes[sid], model)
    for sku, times in sorted(data.arrivals.items()):
        gaps = [b - a for a, b in itertools.pairwise(times) if b > a]
        # gaps across period boundaries are not interarrival times
        if data.periods:
            longest = max(p.duration for p in data.periods)
            gaps = [g for g in gaps if g < 0.25 * longest]
        _fit(f"arrivals.{sku}.interarrival", gaps, model)
    return model


def apply_inputs(definition: DomainDefinition, model: InputModel) -> DomainDefinition:
    """Copy of ``definition`` with fitted inputs written in and the patch version bumped."""
    doc = definition.model_dump(mode="json", by_alias=True, exclude_none=True)
    adapter: TypeAdapter[Any] = TypeAdapter(DistributionSpec)
    for st in (doc.get("process") or {}).get("stations", []):
        fit = model.get(f"{st['id']}.service_time")
        if fit is not None:
            st["service_time"] = adapter.validate_python(fit.spec).model_dump(mode="json")
        for key in ("mttr", "mtbf"):
            fit = model.get(f"{st['id']}.{key}")
            if fit is not None and st.get("failures"):
                st["failures"][key] = adapter.validate_python(fit.spec).model_dump(mode="json")
    major, minor, patch = [*definition.version.split("."), "0", "0"][:3]
    doc["version"] = f"{major}.{minor}.{int(patch) + 1}"
    note = "Inputs fitted from data by `simcore domain backtest` (draft, not published)."
    doc["description"] = f"{doc.get('description', '').strip()} {note}".strip()
    return DomainDefinition.model_validate(doc)

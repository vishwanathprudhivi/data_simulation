"""Deterministic half of the scenario compile step (FR-SCG-02..04, FR-SCG-09).

``ScenarioEdit`` (from the reasoning client, or written by hand) -> lever values
validated against the Lever Catalog -> draft scenario(s), a human-readable diff
vs the baseline, envelope flags, capability gaps and an ``ExperimentSpec`` with
CRN. Nothing here runs a simulation: compile never auto-runs (FR-SCG-02).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from simcore.catalog import (
    apply_levers,
    envelope_check,
    lever_catalog,
    lever_values,
    metric_catalog,
)
from simcore.core.config import ConfigError, ConfigIssue, ScenarioBundle
from simcore.core.hashing import content_hash
from simcore.core.plugins import DomainPlugin
from simcore.specs.domain import LeverDef
from simcore.specs.edit import LeverChange, ScenarioEdit, UnsupportedRequest
from simcore.specs.experiment import ExperimentSpec, VariantSpec

__all__ = ["CompiledScenario", "DiffLine", "compile_edit", "extrapolated_variants"]

DEFAULT_REPLICATIONS = 10
MAX_COMPILED_VARIANTS = 50


@dataclass(frozen=True)
class DiffLine:
    lever: str
    display_name: str
    path: str
    baseline: Any
    value: Any
    unit: str = ""

    def text(self) -> str:
        unit = f" {self.unit}" if self.unit and self.unit != "1" else ""
        return f"{self.display_name}: {self.baseline}{unit} -> {self.value}{unit}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "lever": self.lever,
            "display_name": self.display_name,
            "path": self.path,
            "baseline": self.baseline,
            "value": self.value,
            "unit": self.unit,
            "text": self.text(),
        }


@dataclass
class CompiledScenario:
    """Everything the confirmation card shows (FR-SCG-02/07/09)."""

    edit: ScenarioEdit
    experiment: ExperimentSpec | None
    variant_bundles: dict[str, ScenarioBundle]
    diffs: dict[str, list[DiffLine]]
    lever_values: dict[str, dict[str, Any]]
    extrapolated: dict[str, dict[str, dict[str, Any]]]
    assumptions: list[str]
    ambiguities: list[str]
    gaps: list[UnsupportedRequest]
    goal: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def runnable(self) -> bool:
        return self.experiment is not None or self.goal is not None

    @property
    def draft(self) -> ScenarioBundle | None:
        """The single what-if scenario, when the edit describes exactly one."""
        if len(self.variant_bundles) == 1:
            return next(iter(self.variant_bundles.values()))
        return None

    def card(self) -> dict[str, Any]:
        return {
            "intent": self.edit.intent,
            "summary": self.edit.summary,
            "runnable": self.runnable,
            "variants": [
                {
                    "id": vid,
                    "diff": [d.to_dict() for d in self.diffs[vid]],
                    "levers": self.lever_values[vid],
                    "extrapolated": bool(self.extrapolated.get(vid)),
                    "outside_envelope": self.extrapolated.get(vid, {}),
                    "config_hash": b.config_hash,
                }
                for vid, b in self.variant_bundles.items()
            ],
            "experiment": self.experiment.model_dump(mode="json") if self.experiment else None,
            "goal": self.goal,
            "assumptions": self.assumptions,
            "ambiguities": self.ambiguities,
            "capability_gaps": [g.model_dump(mode="json") for g in self.gaps],
            "notes": self.notes,
        }


def _resolve(change: LeverChange, lever: LeverDef, base_value: Any) -> Any:
    if change.op == "set":
        return change.value
    if not isinstance(base_value, (int, float)) or isinstance(base_value, bool):
        raise ValueError(f"{lever.display_name} is not numeric; use op 'set'")
    if isinstance(change.value, bool) or not isinstance(change.value, (int, float)):
        raise ValueError(f"{change.op} needs a number, got {change.value!r}")
    out = base_value + change.value if change.op == "add" else base_value * change.value
    if lever.type == "int":
        out = round(out)
    return out


def _issue(where: str, message: str, hint: str = "") -> ConfigIssue:
    return ConfigIssue("scenario_edit", where, message, hint)


def compile_edit(
    edit: ScenarioEdit,
    base: ScenarioBundle,
    plugin: DomainPlugin,
    *,
    experiment_id: str | None = None,
    replications: int | None = None,
    master_seed: int = 0,
) -> CompiledScenario:
    """Validate ``edit`` against the catalogs and build variants + ``ExperimentSpec``.

    Raises :class:`ConfigError` with actionable issues (the repair loop feeds
    them back to the reasoning client). Unknown levers named in
    ``edit.unsupported`` are capability gaps, not errors.
    """
    levers = {lv.id: lv for lv in lever_catalog(plugin).levers}
    metrics = {m.id: m for m in metric_catalog(plugin).metrics}
    base_values = lever_values(base, plugin)
    issues: list[ConfigIssue] = []
    gaps = list(edit.unsupported)
    for gap in gaps:
        if gap.nearest_lever is not None and gap.nearest_lever not in levers:
            issues.append(
                _issue(
                    "unsupported.nearest_lever",
                    f"{gap.nearest_lever!r} is not a lever",
                    f"use one of {sorted(levers)} or null",
                )
            )

    def values_for(changes: list[LeverChange], where: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for i, ch in enumerate(changes):
            lever = levers.get(ch.lever)
            if lever is None:
                issues.append(
                    _issue(
                        f"{where}[{i}].lever",
                        f"{ch.lever!r} is not a lever of domain {plugin.name}",
                        f"use one of {sorted(levers)}, or list it under 'unsupported'",
                    )
                )
                continue
            try:
                out[ch.lever] = _resolve(ch, lever, base_values.get(ch.lever))
            except ValueError as exc:
                issues.append(_issue(f"{where}[{i}]", str(exc)))
        return out

    named: list[tuple[str, str, dict[str, Any]]] = []
    if edit.changes:
        named.append(("what_if", edit.summary or "what-if", values_for(edit.changes, "changes")))
    for j, v in enumerate(edit.variants):
        named.append((v.id, v.name or v.id, values_for(v.changes, f"variants[{j}].changes")))
    if edit.sweep is not None:
        lever = levers.get(edit.sweep.lever)
        if lever is None:
            issues.append(
                _issue(
                    "sweep.lever",
                    f"{edit.sweep.lever!r} is not a lever",
                    f"use one of {sorted(levers)}",
                )
            )
        else:
            for level in edit.sweep.levels():
                named.append(
                    (f"{lever.id}-{level}", f"{lever.display_name} = {level}", {lever.id: level})
                )
    goal: dict[str, Any] | None = None
    if edit.goal is not None:
        g = edit.goal
        lever = levers.get(g.lever)
        if lever is None:
            issues.append(_issue("goal.lever", f"{g.lever!r} is not a lever"))
        elif lever.type not in ("int", "float", "duration"):
            issues.append(_issue("goal.lever", f"{lever.display_name} is not numeric"))
        if g.metric not in metrics:
            issues.append(
                _issue(
                    "goal.metric", f"{g.metric!r} is not a metric", f"use one of {sorted(metrics)}"
                )
            )
        if lever is not None and lever.type in ("int", "float", "duration"):
            cands: list[Any] = [int(c) if lever.type == "int" else c for c in g.candidates]
            if not cands:
                if lever.type != "int" or lever.min is None or lever.max is None:
                    issues.append(_issue("goal.candidates", "give candidate values for this lever"))
                else:
                    cands = list(range(int(lever.min), int(lever.max) + 1))
            goal = {**g.model_dump(mode="json"), "candidates": sorted(cands)}
    for i, m in enumerate(edit.metrics):
        if m not in metrics:
            issues.append(
                _issue(f"metrics[{i}]", f"{m!r} is not a metric", f"use one of {sorted(metrics)}")
            )
    ids = [n[0] for n in named]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        issues.append(_issue("variants", f"duplicate variant ids {dupes}"))
    if len(named) > MAX_COMPILED_VARIANTS:
        issues.append(
            _issue("sweep", f"{len(named)} variants exceed the limit {MAX_COMPILED_VARIANTS}")
        )
    if issues:
        raise ConfigError(issues)

    variant_bundles: dict[str, ScenarioBundle] = {}
    diffs: dict[str, list[DiffLine]] = {}
    values: dict[str, dict[str, Any]] = {}
    extrapolated: dict[str, dict[str, dict[str, Any]]] = {}
    variant_specs: list[VariantSpec] = []
    for vid, name, vals in named:
        app = apply_levers(base, plugin, vals)  # raises ConfigError on bad values
        plugin.validate_scenario(app.bundle)
        variant_bundles[vid] = app.bundle
        values[vid] = {**base_values, **app.values}
        diffs[vid] = [
            DiffLine(
                lv,
                levers[lv].display_name,
                levers[lv].path,
                base_values.get(lv),
                v,
                levers[lv].unit or "",
            )
            for lv, v in sorted(app.values.items())
            if base_values.get(lv) != v
        ]
        outside = envelope_check(plugin, values[vid])
        if outside:
            extrapolated[vid] = outside
        variant_specs.append(VariantSpec(id=vid, name=name, overrides=dict(app.overrides)))
    baseline_outside = envelope_check(plugin, base_values)
    if baseline_outside:
        extrapolated["baseline"] = baseline_outside

    assumptions = list(edit.assumptions)
    reps = replications or edit.replications
    if reps is None:
        reps = DEFAULT_REPLICATIONS
        assumptions.append(f"{reps} replications per alternative with common random numbers")
    notes: list[str] = []
    if not variant_specs and goal is None:
        notes.append("nothing to run: the request named no supported change")
    for vid, d in diffs.items():
        if not d:
            notes.append(f"{vid} is identical to the baseline")
    experiment = None
    if variant_specs:
        exp_id = experiment_id or "cmp_" + content_hash(
            [
                base.config_hash,
                [v.model_dump(mode="json") for v in variant_specs],
                reps,
                master_seed,
                list(edit.metrics),
            ],
            length=10,
        )
        experiment = ExperimentSpec(
            id=exp_id,
            name=edit.summary or exp_id,
            base_scenario=base.source,
            include_baseline=True,
            variants=variant_specs,
            design="explicit",
            replications=reps,
            master_seed=master_seed,
            crn=True,
            kpis=list(edit.metrics),
        )
    return CompiledScenario(
        edit=edit,
        experiment=experiment,
        variant_bundles=variant_bundles,
        diffs=diffs,
        lever_values=values,
        extrapolated=extrapolated,
        assumptions=assumptions,
        ambiguities=list(edit.ambiguities),
        gaps=gaps,
        goal=goal,
        notes=notes,
    )


def extrapolated_variants(
    summary: Mapping[str, Any], base: ScenarioBundle, plugin: DomainPlugin
) -> dict[str, bool]:
    """Envelope flag per variant of a finished experiment (from its overrides)."""
    out: dict[str, bool] = {}
    for v in summary.get("variants", []):
        bundle = base.with_overrides(v.get("overrides") or {})
        out[str(v["id"])] = bool(envelope_check(plugin, lever_values(bundle, plugin)))
    return out

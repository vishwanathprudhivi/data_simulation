"""Experiment design: variants, factorial expansion, seed mapping (FR-EXP-02/03, FR-SCG-04).

Seeds: with CRN every alternative uses ``(master_seed, replication)``, so the same
demand and service draws hit every alternative. Without CRN each alternative gets
its own master seed derived from ``(master_seed, variant_id)``.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from simcore.core.config import ConfigError, ConfigIssue, ScenarioBundle
from simcore.core.hashing import stable_int
from simcore.specs.experiment import ExperimentSpec

__all__ = ["BASELINE_ID", "Variant", "build_variants", "seed_for"]

BASELINE_ID = "baseline"


@dataclass(frozen=True)
class Variant:
    id: str
    name: str
    overrides: Mapping[str, Any] = field(default_factory=dict)
    factors: Mapping[str, Any] = field(default_factory=dict)
    bundle: ScenarioBundle | None = None


def _level_token(value: Any) -> str:
    text = re.sub(r"[^A-Za-z0-9._]+", "-", str(value)).strip("-")
    return text or "x"


def _expand_factors(spec: ExperimentSpec) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    if not spec.factors:
        return []
    if spec.design == "latin_hypercube":
        raise ConfigError(
            [
                ConfigIssue(
                    spec.id,
                    "design",
                    "latin_hypercube designs are not implemented yet (BRD FR-EXP-03 stretch)",
                    "use 'full_factorial' or list explicit variants",
                )
            ]
        )
    out = []
    for combo in itertools.product(*(f.levels for f in spec.factors)):
        levels = {f.name: lv for f, lv in zip(spec.factors, combo)}
        overrides = {f.path: lv for f, lv in zip(spec.factors, combo)}
        vid = "__".join(f"{name}_{_level_token(lv)}" for name, lv in levels.items())
        out.append((vid, overrides, levels))
    return out


def build_variants(spec: ExperimentSpec, base: ScenarioBundle) -> list[Variant]:
    """Baseline (optional) + named variants + factorial combinations, each validated.

    Every variant's scenario is validated up front so a bad override fails the
    experiment before anything runs (FR-ENG-08).
    """
    raw: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = []
    if spec.include_baseline:
        raw.append((BASELINE_ID, "Baseline", {}, {}))
    for v in spec.variants:
        raw.append((v.id, v.name or v.id, dict(v.overrides), {}))
    for vid, overrides, levels in _expand_factors(spec):
        raw.append((vid, vid, overrides, levels))
    if not raw:
        raise ConfigError(
            [
                ConfigIssue(
                    spec.id,
                    "variants",
                    "experiment has no alternatives to run",
                    "set include_baseline, add variants, or add factors",
                )
            ]
        )
    ids = [r[0] for r in raw]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ConfigError(
            [
                ConfigIssue(
                    spec.id,
                    "variants",
                    f"duplicate alternative ids {dupes}",
                    "rename variants so factor-generated ids do not collide",
                )
            ]
        )
    issues: list[ConfigIssue] = []
    variants = []
    for vid, name, overrides, levels in raw:
        common: dict[str, Any] = {}
        if spec.warmup is not None:
            common["warmup"] = spec.warmup
        if spec.run_length is not None:
            common["duration"] = spec.run_length
        try:
            bundle = base.with_overrides({**common, **overrides})
        except ConfigError as exc:
            issues.extend(
                ConfigIssue(i.source, f"variant {vid}: {i.location}", i.message, i.hint)
                for i in exc.issues
            )
            continue
        variants.append(Variant(vid, name, overrides, levels, bundle))
    if issues:
        raise ConfigError(issues)
    return variants


def seed_for(spec: ExperimentSpec, variant_id: str, replication: int) -> tuple[int, int]:
    """``(master_seed, replication_id)`` used for one replication of one alternative."""
    if spec.crn:
        return spec.master_seed, replication
    return stable_int(f"{spec.master_seed}:{variant_id}", bits=32), replication

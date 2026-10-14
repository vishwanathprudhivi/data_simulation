"""``ExperimentSpec`` / ``experiment.yaml`` (FR-EXP-03..07, FR-SCG-04/06)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from simcore.specs.common import Duration, Identifier, SchemaVersion, SpecModel

__all__ = [
    "AdaptiveSpec",
    "ExperimentSpec",
    "FactorSpec",
    "FidelityMode",
    "FidelityPlan",
    "VariantSpec",
]

FidelityMode = Literal["logical", "kinematic", "physics"]


class VariantSpec(SpecModel):
    """A named alternative: overrides applied to the base scenario.

    Override keys are scenario paths, e.g. ``fleet.server.count`` (list items are
    selected by their ``agent_type``/``id``) or ``params.arrival_rate``.
    """

    id: Identifier
    name: str = ""
    description: str = ""
    overrides: dict[str, Any] = Field(default_factory=dict)


class FactorSpec(SpecModel):
    name: Identifier
    path: str = Field(description="Scenario path the factor sets (same syntax as overrides).")
    levels: list[Any] = Field(min_length=1)


class FidelityPlan(SpecModel):
    screen: FidelityMode = "logical"
    promote_top_k: int = Field(default=0, ge=0)
    promoted_mode: FidelityMode = "kinematic"
    rank_metric: str | None = None
    rank_direction: Literal["higher_is_better", "lower_is_better"] = "higher_is_better"
    promote: list[str] = Field(
        default_factory=list, description="Variant ids to promote regardless of rank."
    )


class AdaptiveSpec(SpecModel):
    """Sequential replication (FR-EXP-07)."""

    metric: str
    target_half_width: float = Field(gt=0)
    relative: bool = True
    max_replications: int = Field(default=100, ge=2)


class ExperimentSpec(SpecModel):
    schema_version: SchemaVersion = "1.0"
    id: Identifier
    name: str = ""
    description: str = ""
    base_scenario: str = Field(description="Path or id of the baseline scenario.")
    include_baseline: bool = True
    variants: list[VariantSpec] = Field(default_factory=list)
    factors: list[FactorSpec] = Field(default_factory=list)
    design: Literal["explicit", "full_factorial", "latin_hypercube"] = "full_factorial"
    replications: int = Field(default=10, ge=1)
    master_seed: int = Field(default=0, ge=0)
    crn: bool = Field(default=True, description="Common random numbers across alternatives.")
    confidence: float = Field(default=0.95, gt=0, lt=1)
    warmup: Duration | None = Field(default=None, description="Overrides scenario warm-up.")
    run_length: Duration | None = Field(default=None, description="Overrides scenario duration.")
    kpis: list[str] = Field(default_factory=list, description="Empty = all registered KPIs.")
    fidelity: FidelityPlan = Field(default_factory=FidelityPlan)
    adaptive: AdaptiveSpec | None = None
    parallelism: int = Field(default=1, ge=1)
    timeout_s: float | None = Field(default=None, gt=0, description="Per-replication timeout.")

    @model_validator(mode="after")
    def _check(self) -> ExperimentSpec:
        ids = [v.id for v in self.variants]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate variant ids: {dupes}")
        if "baseline" in ids and self.include_baseline:
            raise ValueError("variant id 'baseline' is reserved while include_baseline is true")
        if self.design == "explicit" and self.factors:
            raise ValueError(
                "design 'explicit' uses variants only; remove factors or change design"
            )
        return self

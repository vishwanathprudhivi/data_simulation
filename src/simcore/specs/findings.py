"""``Findings``: deterministic synthesis of an experiment (BRD 7.11, FR-SCG-11).

Computed by statistics code, never by the LLM. The narrative may only use numbers
present here and may not contradict the flags.
"""

from __future__ import annotations

from pydantic import Field

from simcore.specs.common import Identifier, SchemaVersion, SpecModel
from simcore.specs.experiment import FidelityMode

__all__ = [
    "Comparison",
    "Driver",
    "Estimate",
    "FidelitySummary",
    "Findings",
    "GoalResult",
    "GoalStep",
]


class Estimate(SpecModel):
    mean: float
    ci: tuple[float, float]


class FidelitySummary(SpecModel):
    screen: FidelityMode = "logical"
    promoted: list[str] = Field(default_factory=list)
    promoted_mode: FidelityMode | None = None


class Comparison(SpecModel):
    variant: str
    metric: str
    baseline: Estimate | None = None
    variant_estimate: Estimate | None = None
    paired_diff: Estimate
    paired: bool = Field(default=True, description="False when CRN was not used.")
    ci_excludes_zero: bool
    significant: bool
    extrapolated: bool = False
    fidelity: FidelityMode = Field(default="logical", description="Mode that produced it.")
    ref_id: str


class Driver(SpecModel):
    metric: str
    share_of_delta: float
    ref_id: str


class GoalStep(SpecModel):
    variant: str
    value: float
    estimate: Estimate | None = None
    meets: bool
    ref_id: str


class GoalResult(SpecModel):
    """Outcome of a goal-seeking sweep (FR-SCG-05)."""

    lever: str
    metric: str
    target: float
    sense: str
    minimize_lever: bool
    status: str = Field(description="found | not_met")
    best_variant: str | None = None
    best_value: float | None = None
    trace: list[GoalStep] = Field(default_factory=list)


class Findings(SpecModel):
    schema_version: SchemaVersion = "1.0"
    experiment_id: Identifier
    baseline: str
    fidelity: FidelitySummary = Field(default_factory=FidelitySummary)
    n_replications: int = Field(ge=1)
    confidence: float = Field(gt=0, lt=1)
    comparisons: list[Comparison] = Field(default_factory=list)
    drivers: list[Driver] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    goal: GoalResult | None = None

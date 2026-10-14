"""``Findings``: deterministic synthesis of an experiment (BRD 7.11, FR-SCG-11).

Computed by statistics code, never by the LLM. The narrative may only use numbers
present here and may not contradict the flags.
"""

from __future__ import annotations

from pydantic import Field

from simcore.specs.common import Identifier, SchemaVersion, SpecModel
from simcore.specs.experiment import FidelityMode

__all__ = ["Comparison", "Driver", "Estimate", "FidelitySummary", "Findings"]


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

"""``ScenarioEdit``: the structured output of the scenario compile step (FR-SCG-02..05).

The reasoning client translates a natural-language request into this object; it
names levers from the domain's Lever Catalog and never computes results. The
deterministic compiler (:mod:`simcore.compiler`) turns it into a draft scenario,
a diff against the baseline and an ``ExperimentSpec``.
"""

from __future__ import annotations

from typing import Literal, Union

from pydantic import Field, model_validator

from simcore.specs.common import SchemaVersion, SpecModel

#: Scalar lever value. bool first so JSON true/false is not read as 1/0.
LeverValue = Union[bool, int, float, str]  # noqa: UP007 - keeps the 3.10 schema simple

__all__ = [
    "GoalSpec",
    "LeverChange",
    "LeverValue",
    "NamedVariant",
    "ScenarioEdit",
    "SweepSpec",
    "UnsupportedRequest",
]


class LeverChange(SpecModel):
    lever: str = Field(description="Lever id from the domain's Lever Catalog.")
    op: Literal["set", "add", "multiply"] = Field(
        default="set", description="'add'/'multiply' are relative to the baseline value."
    )
    value: LeverValue = Field(description="New value (set) or delta/factor (add/multiply).")


class NamedVariant(SpecModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    name: str = ""
    changes: list[LeverChange] = Field(min_length=1)


class SweepSpec(SpecModel):
    """One-factor range, e.g. "sweep jacks from 10 to 30 by 5"."""

    lever: str
    values: list[LeverValue] = Field(default_factory=list)
    start: float | None = None
    stop: float | None = None
    step: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _check(self) -> SweepSpec:
        has_range = self.start is not None and self.stop is not None and self.step is not None
        if bool(self.values) == has_range:
            raise ValueError("give either values or start/stop/step")
        return self

    def levels(self) -> list[LeverValue]:
        if self.values:
            return list(self.values)
        assert self.start is not None and self.stop is not None and self.step is not None
        out: list[LeverValue] = []
        x, n = self.start, 0
        while x <= self.stop + 1e-9 and n < 1000:
            out.append(int(x) if float(x).is_integer() else round(x, 9))
            n += 1
            x = self.start + n * self.step
        return out


class GoalSpec(SpecModel):
    """Goal-seeking request (FR-SCG-05), e.g. smallest fleet meeting an on-time target."""

    lever: str
    metric: str
    target: float
    sense: Literal[">=", "<="] = ">="
    minimize_lever: bool = Field(default=True, description="Search for the smallest value.")
    candidates: list[float] = Field(default_factory=list, description="Empty: lever bounds.")
    max_runs: int = Field(default=60, ge=1, le=2000, description="Hard cap on replications.")


class UnsupportedRequest(SpecModel):
    request: str = Field(description="The part of the request that cannot be expressed.")
    reason: str
    nearest_lever: str | None = Field(default=None, description="Closest supported lever.")


class ScenarioEdit(SpecModel):
    schema_version: SchemaVersion = "1.0"
    intent: Literal["compare", "sweep", "goal_seek", "clarify"] = "compare"
    summary: str = Field(default="", description="One sentence restating the request.")
    changes: list[LeverChange] = Field(
        default_factory=list, description="A single what-if variant vs the baseline."
    )
    variants: list[NamedVariant] = Field(default_factory=list)
    sweep: SweepSpec | None = None
    goal: GoalSpec | None = None
    metrics: list[str] = Field(default_factory=list, description="Metric ids to report.")
    replications: int | None = Field(default=None, ge=1, le=1000)
    assumptions: list[str] = Field(default_factory=list)
    ambiguities: list[str] = Field(default_factory=list)
    unsupported: list[UnsupportedRequest] = Field(default_factory=list)

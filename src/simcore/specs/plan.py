"""``PlanSpec``: an externally computed dispatch plan (FR-DSP-05, FR-POL-02, AC-DSP-02).

Produced by an optimizer (CP-SAT/MIP, heuristics, or an LLM offline compile) and
executed deterministically by the ``plan_follower`` policy.
"""

from __future__ import annotations

from pydantic import Field, model_validator

from simcore.specs.common import Duration, Identifier, SchemaVersion, SpecModel

__all__ = ["PlanSpec", "PlannedTask"]


class PlannedTask(SpecModel):
    task: str
    planned_start: Duration | None = Field(
        default=None, description="Planned assignment time (s); used for deviation KPIs."
    )
    planned_finish: Duration | None = None


class AgentPlan(SpecModel):
    agent: str
    sequence: list[PlannedTask] = Field(default_factory=list)


class PlanSpec(SpecModel):
    schema_version: SchemaVersion = "1.0"
    id: Identifier
    source: str = Field(default="external", description="Who produced the plan.")
    agents: list[AgentPlan] = Field(default_factory=list)
    unplanned: str = Field(
        default="fifo",
        description="Policy for tasks/agents not in the plan ('none' = leave them idle).",
    )
    expected_kpis: dict[str, float] = Field(
        default_factory=dict, description="KPIs the planner expects; compared after the run."
    )

    @model_validator(mode="after")
    def _check(self) -> PlanSpec:
        seen: set[str] = set()
        for a in self.agents:
            for t in a.sequence:
                if t.task in seen:
                    raise ValueError(f"task {t.task!r} is planned more than once")
                seen.add(t.task)
        agents = [a.agent for a in self.agents]
        dupes = sorted({x for x in agents if agents.count(x) > 1})
        if dupes:
            raise ValueError(f"agents planned more than once: {dupes}")
        return self

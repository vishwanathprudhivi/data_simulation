"""Agent behavior specs, independent of any facility (FR-AGT-01/03/04/05/07)."""

from __future__ import annotations

from typing import Any

from pydantic import Field, model_validator

from simcore.specs.common import Duration, Identifier, Length, SchemaVersion, SpecModel, Speed
from simcore.specs.distributions import DistributionSpec

__all__ = [
    "AgentTypeSpec",
    "BehaviorsSpec",
    "FootprintSpec",
    "ShiftSpec",
    "StateMachineSpec",
    "TransitionSpec",
]


class FootprintSpec(SpecModel):
    length: Length = 1.0
    width: Length = 1.0


class TransitionSpec(SpecModel):
    source: Identifier = Field(alias="from")
    target: Identifier = Field(alias="to")
    on: str = Field(description="Event or condition that triggers the transition.")


class StateMachineSpec(SpecModel):
    """Declarative, visualizable state machine (FR-AGT-03)."""

    initial: Identifier
    states: list[Identifier] = Field(min_length=1)
    transitions: list[TransitionSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> StateMachineSpec:
        known = set(self.states)
        if self.initial not in known:
            raise ValueError(f"initial state {self.initial!r} is not in states {self.states}")
        for t in self.transitions:
            for end in (t.source, t.target):
                if end not in known:
                    raise ValueError(
                        f"transition {t.source}->{t.target} references unknown state {end!r}"
                    )
        return self


class BreakSpec(SpecModel):
    start: Duration
    duration: Duration


class ShiftSpec(SpecModel):
    """Availability schedule relative to sim start (FR-AGT-07)."""

    start: Duration = 0.0
    end: Duration | None = None
    breaks: list[BreakSpec] = Field(default_factory=list)


class AgentTypeSpec(SpecModel):
    id: Identifier
    description: str = ""
    capabilities: list[str] = Field(default_factory=list)
    footprint: FootprintSpec = Field(default_factory=FootprintSpec)
    speeds: dict[str, Speed] = Field(
        default_factory=dict, description="Named speed profile, e.g. {empty: 1.5, loaded: 1.1}."
    )
    capacity: float = Field(default=1.0, gt=0, description="Work units carried per trip.")
    service_times: dict[str, DistributionSpec] = Field(
        default_factory=dict, description="Named service-time distributions (seconds)."
    )
    speed_multiplier: DistributionSpec | None = Field(
        default=None, description="Per-agent multiplier drawn once per agent (stream agent_speed)."
    )
    state_machine: StateMachineSpec | None = None
    shift: ShiftSpec | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class BehaviorsSpec(SpecModel):
    """``behaviors.yaml``."""

    schema_version: SchemaVersion = "1.0"
    agent_types: list[AgentTypeSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> BehaviorsSpec:
        ids = [a.id for a in self.agent_types]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate agent type ids: {dupes}")
        return self

    def agent_type(self, type_id: str) -> AgentTypeSpec:
        for a in self.agent_types:
            if a.id == type_id:
                return a
        raise KeyError(
            f"agent type {type_id!r} not defined; known: {[a.id for a in self.agent_types]}"
        )

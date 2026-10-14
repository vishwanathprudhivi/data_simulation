"""``ScenarioSpec`` / ``scenario.yaml`` (FR-SCN-01..06, FR-SCG-01)."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from simcore.specs.common import Duration, Identifier, SchemaVersion, SpecModel
from simcore.specs.distributions import DistributionSpec

__all__ = [
    "CORE_DISRUPTION_TYPES",
    "BeatSpec",
    "DemandSpec",
    "DisruptionSpec",
    "FleetEntry",
    "ListDemand",
    "PolicyRef",
    "ReplayDemand",
    "ScenarioSpec",
    "StochasticDemand",
    "TaskSpec",
    "TriggerSpec",
]

#: Core-generic disruption types (FR-SCN-03). Domains may declare more.
CORE_DISRUPTION_TYPES: tuple[str, ...] = (
    "resource_unavailable",
    "location_blocked",
    "location_degraded",
    "capacity_change",
    "demand_surge",
    "speed_change",
)


class FleetEntry(SpecModel):
    agent_type: Identifier
    count: int = Field(ge=0)
    attributes: dict[str, Any] = Field(default_factory=dict)


class PolicyRef(SpecModel):
    name: str = "fifo"
    assignment: Literal["pull", "push"] = Field(
        default="pull", description="pull: idle agents request work; push: dispatcher assigns."
    )
    params: dict[str, Any] = Field(default_factory=dict)


class TaskSpec(SpecModel):
    """A hand-authored work item (FR-DSP-01)."""

    id: Identifier
    type: str = "task"
    origin: str | None = None
    destination: str | None = None
    quantity: float = Field(default=1.0, ge=0)
    release_time: Duration = 0.0
    due_time: Duration | None = None
    priority: int = 0
    attributes: dict[str, Any] = Field(default_factory=dict)


class _DemandBase(SpecModel):
    stream: str = Field(default="demand", description="Named RNG stream for this source.")


class StochasticDemand(_DemandBase):
    kind: Literal["stochastic"] = "stochastic"
    task_type: str = "task"
    interarrival: DistributionSpec
    start: Duration = 0.0
    stop: Duration | None = None
    max_tasks: int | None = Field(default=None, ge=0)
    due_after: DistributionSpec | None = Field(
        default=None, description="Due time = release + draw (seconds)."
    )
    quantity: DistributionSpec | None = None
    priority: int = 0
    origins: list[str] = Field(default_factory=list, description="Uniform choice if non-empty.")
    destinations: list[str] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)


class ReplayDemand(_DemandBase):
    """Replay of a historical CSV (columns per ``columns`` mapping, times in seconds)."""

    kind: Literal["replay"] = "replay"
    path: str
    columns: dict[str, str] = Field(
        default_factory=dict,
        description="Map task field -> CSV column; defaults to identical names.",
    )
    time_offset: Duration = 0.0


class ListDemand(_DemandBase):
    kind: Literal["list"] = "list"
    tasks: list[TaskSpec] = Field(default_factory=list)


DemandSpec = Annotated[StochasticDemand | ReplayDemand | ListDemand, Field(discriminator="kind")]


class TriggerSpec(SpecModel):
    """Condition trigger (FR-SCN-04): fires the first time ``signal op threshold`` holds."""

    signal: Literal["queue_length", "completed_tasks", "released_tasks"]
    op: Literal[">", ">=", "<", "<=", "=="]
    threshold: float


class DisruptionSpec(SpecModel):
    id: Identifier
    type: str = Field(description="Core type (see CORE_DISRUPTION_TYPES) or a domain type.")
    at: Duration | None = Field(default=None, description="Sim-time start (seconds).")
    trigger: TriggerSpec | None = None
    duration: Duration | None = Field(default=None, description="None = until end of run.")
    target: str | None = Field(
        default=None,
        description="Entity id, 'type:<entity type>' or 'tag:<tag>' selecting affected entities.",
    )
    params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _when(self) -> DisruptionSpec:
        if (self.at is None) == (self.trigger is None):
            raise ValueError(
                f"disruption {self.id!r}: set exactly one of 'at' (scheduled) or 'trigger'"
            )
        return self


class BeatSpec(SpecModel):
    """Scripted demo beat (FR-SCN-06)."""

    name: str
    at: Duration
    narration: str = ""
    pause: bool = False


class ScenarioSpec(SpecModel):
    """One fully specified scenario. ``extends`` is resolved by the loader."""

    schema_version: SchemaVersion = "1.0"
    id: Identifier
    name: str = ""
    description: str = ""
    domain: Identifier
    domain_version: str | None = None
    extends: str | None = Field(
        default=None, description="Parent scenario (path), resolved at load."
    )
    facility: str | None = Field(default=None, description="Path to facility.yaml.")
    behaviors: str | None = Field(default=None, description="Path to behaviors.yaml.")
    duration: Duration = Field(description="Run length (seconds or '8 h').")
    warmup: Duration = 0.0
    seed: int = Field(default=0, ge=0, description="Master seed when run standalone.")
    fleet: list[FleetEntry] = Field(default_factory=list)
    demand: list[DemandSpec] = Field(default_factory=list)
    policy: PolicyRef = Field(default_factory=PolicyRef)
    disruptions: list[DisruptionSpec] = Field(default_factory=list)
    params: dict[str, Any] = Field(
        default_factory=dict, description="Domain parameters (validated by the domain plug-in)."
    )
    beats: list[BeatSpec] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> ScenarioSpec:
        if self.duration <= 0:
            raise ValueError("duration must be > 0")
        if self.warmup >= self.duration:
            raise ValueError(
                f"warmup ({self.warmup}s) must be shorter than duration ({self.duration}s)"
            )
        types = [f.agent_type for f in self.fleet]
        dupes = sorted({t for t in types if types.count(t) > 1})
        if dupes:
            raise ValueError(f"fleet lists agent types more than once: {dupes}; merge the entries")
        streams = [d.stream for d in self.demand]
        dupes = sorted({s for s in streams if streams.count(s) > 1})
        if dupes:
            raise ValueError(f"demand sources share RNG streams {dupes}; give each a unique stream")
        ids = [d.id for d in self.disruptions]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate disruption ids: {dupes}")
        return self

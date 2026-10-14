"""``Policy`` contract and built-in dispatch policies (FR-AGT-02, FR-POL-01/05, FR-DSP-04).

Every policy sees the same observation and returns the same action type, so any
two policies can be A/B compared under CRN. Observations are plain immutable
data; policies must be deterministic functions of their observation and params.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal

from pydantic import TypeAdapter

__all__ = [
    "BUILTIN_POLICIES",
    "AgentView",
    "Assignment",
    "DispatchAction",
    "DispatchObservation",
    "FifoPolicy",
    "NearestTaskPolicy",
    "Policy",
    "PolicyError",
    "PolicyFactory",
    "TaskView",
    "ZoneAffinityPolicy",
    "compatible",
    "make_policy",
]

Position = tuple[float, ...]


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class AgentView:
    id: str
    type: str
    position: Position | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)
    capabilities: tuple[str, ...] = ()


@dataclass(frozen=True)
class TaskView:
    id: str
    type: str
    priority: int
    release_time: float
    due_time: float | None = None
    origin: str | None = None
    destination: str | None = None
    origin_position: Position | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)
    requires: str | None = None


def compatible(agent: AgentView, task: TaskView) -> bool:
    """A task with ``requires`` needs that capability on the agent (FR-AGT-05)."""
    return task.requires is None or task.requires in agent.capabilities


@dataclass(frozen=True)
class DispatchObservation:
    """Idle, available agents and pending tasks, both in deterministic order.

    ``trigger`` is ``pull`` when agents became free (agents choose tasks) and
    ``push`` when tasks arrived (dispatcher chooses agents).
    """

    time: float
    trigger: Literal["pull", "push"]
    agents: tuple[AgentView, ...]
    tasks: tuple[TaskView, ...]


@dataclass(frozen=True)
class Assignment:
    agent_id: str
    task_id: str


@dataclass(frozen=True)
class DispatchAction:
    assignments: tuple[Assignment, ...] = ()


class Policy(ABC):
    """Decision logic behind a uniform contract: ``decide(observation) -> action``."""

    name: ClassVar[str]
    observation_type: ClassVar[type[Any]] = DispatchObservation
    action_type: ClassVar[type[Any]] = DispatchAction

    def __init__(self, **params: Any) -> None:
        self.params = dict(params)

    @abstractmethod
    def decide(self, observation: DispatchObservation) -> DispatchAction: ...

    def kpi_calculators(self) -> Sequence[Any]:
        """Policy-specific KPI calculators (e.g. plan adherence)."""
        return ()

    def report(self, kpis: Mapping[str, float]) -> dict[str, Any] | None:
        """Optional post-run report placed in run metadata (e.g. plan vs realized)."""
        return None

    @classmethod
    def observation_schema(cls) -> dict[str, Any]:
        return TypeAdapter(cls.observation_type).json_schema()

    @classmethod
    def action_schema(cls) -> dict[str, Any]:
        return TypeAdapter(cls.action_type).json_schema()


def _greedy(
    obs: DispatchObservation, base_score: Callable[[AgentView, TaskView], float]
) -> DispatchAction:
    """Greedy matching in trigger order; ties broken by observation order."""

    def score(agent: AgentView, task: TaskView) -> float:
        return base_score(agent, task) if compatible(agent, task) else math.inf

    out: list[Assignment] = []
    if obs.trigger == "pull":
        free = list(obs.tasks)
        for agent in obs.agents:
            if not free:
                break
            best = min(range(len(free)), key=lambda i: (score(agent, free[i]), i))
            if math.isinf(score(agent, free[best])):
                continue
            out.append(Assignment(agent.id, free.pop(best).id))
    else:
        idle = list(obs.agents)
        for task in obs.tasks:
            if not idle:
                break
            best = min(range(len(idle)), key=lambda i: (score(idle[i], task), i))
            if math.isinf(score(idle[best], task)):
                continue
            out.append(Assignment(idle.pop(best).id, task.id))
    return DispatchAction(tuple(out))


class FifoPolicy(Policy):
    """Queue order (priority, release time, arrival) to agents in registry order."""

    name = "fifo"

    def decide(self, observation: DispatchObservation) -> DispatchAction:
        return _greedy(observation, lambda a, t: 0.0)


class NearestTaskPolicy(Policy):
    """Pair agents and tasks by straight-line distance to the task origin."""

    name = "nearest_task"

    def decide(self, observation: DispatchObservation) -> DispatchAction:
        def dist(agent: AgentView, task: TaskView) -> float:
            if agent.position is None or task.origin_position is None:
                return 1e300  # unknown positions: fall back to queue order
            return math.dist(agent.position[:2], task.origin_position[:2])

        return _greedy(observation, dist)


class ZoneAffinityPolicy(Policy):
    """Prefer tasks in an agent's zones (agent attribute ``zones``, task attribute ``zone``).

    Params: ``zone_attribute`` (default ``zone``), ``fallback`` (default true): when
    false, agents never take tasks outside their zones.
    """

    name = "zone_affinity"

    def decide(self, observation: DispatchObservation) -> DispatchAction:
        attr = str(self.params.get("zone_attribute", "zone"))
        fallback = bool(self.params.get("fallback", True))

        def score(agent: AgentView, task: TaskView) -> float:
            zones: Sequence[Any] = agent.attributes.get("zones") or ()
            if task.attributes.get(attr) in zones:
                return 0.0
            return 1.0 if fallback else math.inf

        return _greedy(observation, score)


PolicyFactory = Callable[..., Policy]

BUILTIN_POLICIES: dict[str, PolicyFactory] = {
    FifoPolicy.name: FifoPolicy,
    NearestTaskPolicy.name: NearestTaskPolicy,
    ZoneAffinityPolicy.name: ZoneAffinityPolicy,
}


def _register_plan_policies() -> None:
    from simcore.core.policy.learned import LearnedPolicy, LlmDispatchPolicy
    from simcore.core.policy.plan import PlanFollowerPolicy, SolverCallbackPolicy

    BUILTIN_POLICIES[PlanFollowerPolicy.name] = PlanFollowerPolicy
    BUILTIN_POLICIES[SolverCallbackPolicy.name] = SolverCallbackPolicy
    BUILTIN_POLICIES[LearnedPolicy.name] = LearnedPolicy
    BUILTIN_POLICIES[LlmDispatchPolicy.name] = LlmDispatchPolicy


def make_policy(
    name: str, params: Mapping[str, Any], extra: Mapping[str, PolicyFactory] | None = None
) -> Policy:
    """Instantiate policy ``name``; domain policies (``extra``) shadow built-ins."""
    available = {**BUILTIN_POLICIES, **(extra or {})}
    if name not in available:
        raise PolicyError(f"unknown policy {name!r}; available: {sorted(available)}")
    return available[name](**params)

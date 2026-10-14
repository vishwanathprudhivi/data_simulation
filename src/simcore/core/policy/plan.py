"""Plan follower and solver-callback policies (FR-DSP-05, FR-POL-02, AC-DSP-02).

``plan_follower`` executes an external ``PlanSpec``: each agent takes its planned
tasks in sequence as soon as the next one is released (waiting otherwise); tasks
or agents outside the plan are handled by the ``unplanned`` fallback policy. After
the run it reports realized KPIs against the plan's expectations and adherence
statistics.

``solver_callback`` hands every decision point to a deterministic callable
(``module:function``), e.g. an assignment solver. ``assignment_min_distance`` is a
built-in min-cost matching (Hungarian algorithm via SciPy).
"""

from __future__ import annotations

import importlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from simcore.core.obs.eventlog import EventLog, EventType
from simcore.core.obs.kpi import KpiContext
from simcore.core.policy.base import (
    Assignment,
    DispatchAction,
    DispatchObservation,
    FifoPolicy,
    Policy,
    PolicyError,
    compatible,
)
from simcore.specs.plan import PlanSpec

__all__ = ["PlanFollowerPolicy", "SolverCallbackPolicy", "assignment_min_distance", "load_plan"]


def load_plan(plan: PlanSpec | Mapping[str, Any] | str) -> PlanSpec:
    if isinstance(plan, PlanSpec):
        return plan
    if isinstance(plan, str):
        path = Path(plan)
        if not path.exists():
            raise PolicyError(f"plan file not found: {plan}")
        import yaml

        data = yaml.safe_load(path.read_text())
        return PlanSpec.model_validate(data)
    return PlanSpec.model_validate(dict(plan))


class PlanAdherenceKpis:
    name = "core.plan"

    def __init__(self, plan: PlanSpec) -> None:
        self.plan = plan
        self.owner = {t.task: a.agent for a in plan.agents for t in a.sequence}
        self.planned_start = {t.task: t.planned_start for a in plan.agents for t in a.sequence}

    def compute(self, log: EventLog, ctx: KpiContext) -> dict[str, float]:
        assigned = {
            r.task: (r.agent, r.time) for r in log.of_type(EventType.TASK_ASSIGNED) if r.task
        }
        planned = [t for t in self.owner if t in assigned]
        on_agent = [t for t in planned if assigned[t][0] == self.owner[t]]
        devs = [
            assigned[t][1] - float(self.planned_start[t])  # type: ignore[arg-type]
            for t in planned
            if self.planned_start.get(t) is not None
        ]
        # sequence adherence: share of agents whose realized order equals the planned order
        ok = 0
        for a in self.plan.agents:
            want = [t.task for t in a.sequence if t.task in assigned]
            got = sorted(
                (assigned[t][1], i, t) for i, t in enumerate(want) if assigned[t][0] == a.agent
            )
            ok += [t for _, _, t in got] == [t for t in want if assigned[t][0] == a.agent]
        unplanned = sum(1 for t in assigned if t not in self.owner)
        return {
            "plan_tasks_planned": float(len(self.owner)),
            "plan_tasks_executed": float(len(planned)),
            "plan_on_agent_rate": len(on_agent) / len(planned) if planned else float("nan"),
            "plan_sequence_adherence": ok / len(self.plan.agents) if self.plan.agents else 1.0,
            "plan_start_deviation_mean": sum(devs) / len(devs) if devs else float("nan"),
            "plan_tasks_unplanned": float(unplanned),
        }


class PlanFollowerPolicy(Policy):
    name = "plan_follower"

    def __init__(self, plan: Any = None, **params: Any) -> None:
        super().__init__(plan=plan, **params)
        if plan is None:
            raise PolicyError("plan_follower needs params.plan (inline plan or path)")
        self.plan = load_plan(plan)
        self.queue: dict[str, list[str]] = {
            a.agent: [t.task for t in a.sequence] for a in self.plan.agents
        }
        self.owner = {t: a for a, ts in self.queue.items() for t in ts}
        fallback = self.plan.unplanned
        self.fallback: Policy | None = None if fallback == "none" else FifoPolicy()
        if fallback not in ("none", "fifo"):
            raise PolicyError(f"plan.unplanned must be 'fifo' or 'none', got {fallback!r}")

    def decide(self, observation: DispatchObservation) -> DispatchAction:
        pending = {t.id: t for t in observation.tasks}
        out: list[Assignment] = []
        used_agents: set[str] = set()
        for agent in observation.agents:
            seq = self.queue.get(agent.id)
            if not seq:
                continue
            nxt = seq[0]
            if nxt in pending:  # next planned task released: take it
                out.append(Assignment(agent.id, nxt))
                seq.pop(0)
                used_agents.add(agent.id)
                del pending[nxt]
        if self.fallback is not None:
            free_agents = tuple(
                a
                for a in observation.agents
                if a.id not in used_agents and not self.queue.get(a.id)
            )
            free_tasks = tuple(t for t in pending.values() if t.id not in self.owner)
            if free_agents and free_tasks:
                sub = DispatchObservation(
                    observation.time, observation.trigger, free_agents, free_tasks
                )
                out.extend(self.fallback.decide(sub).assignments)
        return DispatchAction(tuple(out))

    def kpi_calculators(self) -> Sequence[Any]:
        return (PlanAdherenceKpis(self.plan),)

    def report(self, kpis: Mapping[str, float]) -> dict[str, Any] | None:
        rows = {}
        for k, expected in sorted(self.plan.expected_kpis.items()):
            realized = kpis.get(k)
            rows[k] = {
                "expected": expected,
                "realized": realized,
                "delta": None if realized is None else realized - expected,
                "relative": None
                if realized is None or expected == 0
                else (realized - expected) / abs(expected),
            }
        return {"plan_id": self.plan.id, "source": self.plan.source, "kpis": rows}


Callback = Callable[[DispatchObservation, Mapping[str, Any]], Sequence[tuple[str, str]]]


def _resolve(target: str) -> Callback:
    module, _, attr = target.partition(":")
    try:
        fn = getattr(importlib.import_module(module), attr)
    except (ImportError, AttributeError) as exc:
        raise PolicyError(f"cannot load solver callback {target!r}: {exc}") from exc
    return fn  # type: ignore[no-any-return]


class SolverCallbackPolicy(Policy):
    """Delegates each decision point to ``params.callback`` (``module:function``).

    The callback receives ``(observation, params)`` and returns ``(agent_id,
    task_id)`` pairs. It must be deterministic; its decisions are validated like
    any policy's.
    """

    name = "solver_callback"

    def __init__(
        self, callback: str = "simcore.core.policy.plan:assignment_min_distance", **params: Any
    ) -> None:
        super().__init__(callback=callback, **params)
        self.fn = _resolve(callback)

    def decide(self, observation: DispatchObservation) -> DispatchAction:
        pairs = self.fn(observation, self.params)
        return DispatchAction(tuple(Assignment(str(a), str(t)) for a, t in pairs))


def assignment_min_distance(
    observation: DispatchObservation, params: Mapping[str, Any]
) -> list[tuple[str, str]]:
    """Min-total-distance agent/task matching (Hungarian algorithm), ties by order."""
    from scipy.optimize import linear_sum_assignment

    agents, tasks = observation.agents, observation.tasks
    if not agents or not tasks:
        return []
    big = 1e12
    cost = []
    for a in agents:
        row = []
        for t in tasks:
            if not compatible(a, t):
                row.append(big)
            elif a.position is None or t.origin_position is None:
                row.append(0.0)
            else:
                row.append(math.dist(a.position[:2], t.origin_position[:2]))
        cost.append(row)
    rows, cols = linear_sum_assignment(cost)
    return [(agents[r].id, tasks[c].id) for r, c in zip(rows, cols) if cost[r][c] < big]


def plan_to_json(plan: PlanSpec) -> str:
    return json.dumps(plan.model_dump(mode="json"), sort_keys=True)

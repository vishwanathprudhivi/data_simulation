"""KPI calculators computed post-run from the event log (FR-OBS-02/03, FR-INT-06).

Calculators are registered per domain; core calculators are always present.
Values are SI (seconds, 1/second, fractions).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np

from simcore.core.obs.eventlog import EventLog, EventType

__all__ = [
    "AVAILABLE_EXCLUDED_STATES",
    "IDLE_STATES",
    "KpiCalculator",
    "KpiContext",
    "KpiRegistry",
    "core_kpi_calculators",
]

#: Agent states that count as not working.
IDLE_STATES: frozenset[str] = frozenset({"idle"})
#: Agent states during which an agent is not available at all (excluded from utilization).
AVAILABLE_EXCLUDED_STATES: frozenset[str] = frozenset({"unavailable", "off_shift", "failed"})


@dataclass(frozen=True)
class KpiContext:
    """Observation window for KPIs: ``[warmup, end]``."""

    warmup: float
    end: float
    params: Mapping[str, object] = field(default_factory=dict)

    @property
    def window(self) -> float:
        return self.end - self.warmup


class KpiCalculator(Protocol):
    """Computes named KPI values from an event log."""

    name: str

    def compute(self, log: EventLog, ctx: KpiContext) -> dict[str, float]: ...


class KpiRegistry:
    def __init__(self, calculators: Iterable[KpiCalculator] = ()) -> None:
        self._calcs: dict[str, KpiCalculator] = {}
        for c in calculators:
            self.register(c)

    def register(self, calc: KpiCalculator) -> None:
        if calc.name in self._calcs:
            raise ValueError(f"KPI calculator {calc.name!r} already registered")
        self._calcs[calc.name] = calc

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._calcs)

    def compute(self, log: EventLog, ctx: KpiContext) -> dict[str, float]:
        out: dict[str, float] = {}
        for calc in self._calcs.values():
            for key, value in calc.compute(log, ctx).items():
                if key in out:
                    raise ValueError(f"KPI {key!r} produced by more than one calculator")
                out[key] = float(value)
        return dict(sorted(out.items()))


def _pct(values: Sequence[float], q: float) -> float:
    return float(np.percentile(values, q)) if values else float("nan")


@dataclass
class _TaskTimes:
    release: float
    assigned: float | None = None
    started: float | None = None
    completed: float | None = None
    due: float | None = None


def _task_times(log: EventLog) -> dict[str, _TaskTimes]:
    tasks: dict[str, _TaskTimes] = {}
    for r in log.of_type(
        EventType.TASK_RELEASED,
        EventType.TASK_ASSIGNED,
        EventType.TASK_STARTED,
        EventType.TASK_COMPLETED,
    ):
        if r.task is None:
            continue
        if r.type == EventType.TASK_RELEASED:
            due = r.data.get("due_time")
            tasks[r.task] = _TaskTimes(r.time, due=None if due is None else float(due))
            continue
        t = tasks.get(r.task)
        if t is None:
            continue
        if r.type == EventType.TASK_ASSIGNED and t.assigned is None:
            t.assigned = r.time
        elif r.type == EventType.TASK_STARTED and t.started is None:
            t.started = r.time
        elif r.type == EventType.TASK_COMPLETED:
            t.completed = r.time
    return tasks


class TaskKpis:
    """Throughput, cycle time, wait time and due-time adherence for tasks released after warm-up."""

    name = "core.tasks"

    def compute(self, log: EventLog, ctx: KpiContext) -> dict[str, float]:
        tasks = [t for t in _task_times(log).values() if t.release >= ctx.warmup]
        done = [t for t in tasks if t.completed is not None and t.completed <= ctx.end]
        cycle = [t.completed - t.release for t in done if t.completed is not None]
        waits = [t.assigned - t.release for t in tasks if t.assigned is not None]
        with_due = [t for t in done if t.due is not None]
        on_time = [
            t
            for t in with_due
            if t.completed is not None and t.due is not None and t.completed <= t.due
        ]
        out = {
            "tasks_released": float(len(tasks)),
            "tasks_completed": float(len(done)),
            "throughput": len(done) / ctx.window if ctx.window > 0 else float("nan"),
            "cycle_time_mean": float(np.mean(cycle)) if cycle else float("nan"),
            "cycle_time_p50": _pct(cycle, 50),
            "cycle_time_p90": _pct(cycle, 90),
            "cycle_time_p99": _pct(cycle, 99),
            "wait_time_mean": float(np.mean(waits)) if waits else float("nan"),
            "wait_time_p90": _pct(waits, 90),
        }
        if with_due:
            out["due_time_adherence"] = len(on_time) / len(with_due)
        return out


class QueueKpis:
    """Time-weighted queue length over the window (unassigned released tasks)."""

    name = "core.queue"

    def compute(self, log: EventLog, ctx: KpiContext) -> dict[str, float]:
        level = 0
        prev = 0.0
        area = 0.0
        peak = 0
        seen_window = False
        for r in log.of_type(
            EventType.TASK_RELEASED, EventType.TASK_ASSIGNED, EventType.TASK_REQUEUED
        ):
            lo, hi = max(prev, ctx.warmup), min(r.time, ctx.end)
            if hi > lo:
                area += level * (hi - lo)
            if not seen_window and r.time >= ctx.warmup:
                seen_window = True
                peak = max(peak, level)  # level carried into the window
            prev = r.time
            level += -1 if r.type == EventType.TASK_ASSIGNED else 1
            if ctx.warmup <= r.time <= ctx.end:
                peak = max(peak, level)
        lo = max(prev, ctx.warmup)
        if ctx.end > lo:
            area += level * (ctx.end - lo)
        if not seen_window:
            peak = level
        return {
            "queue_length_mean": area / ctx.window if ctx.window > 0 else float("nan"),
            "queue_length_max": float(peak),
        }


class AgentStateKpis:
    """Utilization and state-time breakdown per state (fractions of agent-time)."""

    name = "core.agents"

    def compute(self, log: EventLog, ctx: KpiContext) -> dict[str, float]:
        current: dict[str, tuple[str, float]] = {}
        totals: dict[str, float] = {}

        def account(state: str, start: float, stop: float) -> None:
            lo, hi = max(start, ctx.warmup), min(stop, ctx.end)
            if hi > lo:
                totals[state] = totals.get(state, 0.0) + (hi - lo)

        for r in log.of_type(EventType.AGENT_STATE):
            if r.agent is None:
                continue
            state = str(r.data.get("state"))
            if r.agent in current:
                prev, since = current[r.agent]
                account(prev, since, r.time)
            current[r.agent] = (state, r.time)
        for prev, since in current.values():
            account(prev, since, ctx.end)
        total = sum(totals.values())
        if total <= 0:
            return {}
        out = {f"state_share_{s}": v / total for s, v in sorted(totals.items())}
        available = sum(v for s, v in totals.items() if s not in AVAILABLE_EXCLUDED_STATES)
        busy = sum(
            v
            for s, v in totals.items()
            if s not in AVAILABLE_EXCLUDED_STATES and s not in IDLE_STATES
        )
        out["utilization"] = busy / available if available > 0 else float("nan")
        out["agents"] = float(len(current))
        return out


def core_kpi_calculators() -> list[KpiCalculator]:
    return [TaskKpis(), QueueKpis(), AgentStateKpis()]

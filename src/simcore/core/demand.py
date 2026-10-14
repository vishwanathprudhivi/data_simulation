"""Demand sources: stochastic generator, historical replay, hand-authored list (FR-SCN-02)."""

from __future__ import annotations

import csv
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from simcore.core.dispatch.task import Task
from simcore.core.sampling import make_sampler
from simcore.core.scheduler import Priority
from simcore.specs.scenario import DemandSpec, ListDemand, ReplayDemand, StochasticDemand

if TYPE_CHECKING:
    from simcore.core.model import SimContext

__all__ = ["ARRIVAL", "Arrival", "DemandSource", "make_source"]

ARRIVAL = "demand.arrival"


@dataclass(frozen=True)
class Arrival:
    source: str
    task: Task
    sequence: int


Release = Callable[[Task], None]


class DemandSource:
    """Base class. Subclasses schedule ``demand.arrival`` events on the context."""

    def __init__(self, spec: DemandSpec) -> None:
        self.spec = spec
        self.name = spec.stream
        self.rate_factor = 1.0
        self.released = 0

    def start(self, ctx: SimContext) -> None:
        raise NotImplementedError

    def on_arrival(self, ctx: SimContext, arrival: Arrival) -> None:
        """Called after the arrival's task is released."""


class StochasticSource(DemandSource):
    spec: StochasticDemand

    def __init__(self, spec: StochasticDemand) -> None:
        super().__init__(spec)
        self._interarrival = make_sampler(spec.interarrival)
        self._due = make_sampler(spec.due_after) if spec.due_after else None
        self._qty = make_sampler(spec.quantity) if spec.quantity else None

    def _schedule_next(self, ctx: SimContext, after: float) -> None:
        if self.spec.max_tasks is not None and self.released >= self.spec.max_tasks:
            return
        gap = self._interarrival(ctx.rng.stream(self.spec.stream)) / self.rate_factor
        when = after + gap
        if self.spec.stop is not None and when > self.spec.stop:
            return
        n = self.released + 1
        task = self._make_task(ctx, n, when)
        self.released = n
        ctx.scheduler.schedule(when, ARRIVAL, Arrival(self.name, task, n), Priority.RELEASE)

    def _make_task(self, ctx: SimContext, n: int, when: float) -> Task:
        g = ctx.rng.stream(self.spec.stream)
        # Draw order is fixed: due, quantity, origin, destination (FR-EXP-01).
        due = when + self._due(g) if self._due else None
        qty = self._qty(g) if self._qty else 1.0
        origin = str(g.choice(self.spec.origins)) if self.spec.origins else None
        dest = str(g.choice(self.spec.destinations)) if self.spec.destinations else None
        return Task(
            id=f"{self.spec.stream}-{n:06d}",
            type=self.spec.task_type,
            origin=origin,
            destination=dest,
            quantity=qty,
            release_time=when,
            due_time=due,
            priority=self.spec.priority,
            attributes=dict(self.spec.attributes),
        )

    def start(self, ctx: SimContext) -> None:
        self._schedule_next(ctx, max(self.spec.start, ctx.now))

    def on_arrival(self, ctx: SimContext, arrival: Arrival) -> None:
        self._schedule_next(ctx, ctx.now)


class ListSource(DemandSource):
    spec: ListDemand

    def start(self, ctx: SimContext) -> None:
        for n, t in enumerate(sorted(self.spec.tasks, key=lambda t: t.release_time), start=1):
            task = Task.from_spec(t)
            ctx.scheduler.schedule(
                task.release_time, ARRIVAL, Arrival(self.name, task, n), Priority.RELEASE
            )
        self.released = len(self.spec.tasks)


_TASK_FIELDS = (
    "id",
    "type",
    "origin",
    "destination",
    "quantity",
    "release_time",
    "due_time",
    "priority",
)


class ReplaySource(DemandSource):
    spec: ReplayDemand

    def _rows(self) -> list[dict[str, Any]]:
        path = Path(self.spec.path)
        if not path.exists():
            raise FileNotFoundError(f"replay demand file not found: {path}")
        cols = {f: self.spec.columns.get(f, f) for f in _TASK_FIELDS}
        rows = []
        with path.open(newline="", encoding="utf-8") as fh:
            for n, raw in enumerate(csv.DictReader(fh), start=2):
                if cols["release_time"] not in raw:
                    raise ValueError(
                        f"{path}: missing release time column {cols['release_time']!r}; "
                        "map it with demand.columns.release_time"
                    )
                rows.append(self._row(raw, cols, n, path))
        return rows

    def _row(
        self, raw: dict[str, str], cols: dict[str, str], line: int, path: Path
    ) -> dict[str, Any]:
        def get(name: str) -> str | None:
            value = raw.get(cols[name])
            return value if value not in (None, "") else None

        try:
            release = float(get("release_time") or 0.0) + self.spec.time_offset
            due = get("due_time")
            return {
                "id": get("id") or f"{self.spec.stream}-{line - 1:06d}",
                "type": get("type") or "task",
                "origin": get("origin"),
                "destination": get("destination"),
                "quantity": float(get("quantity") or 1.0),
                "release_time": release,
                "due_time": None if due is None else float(due) + self.spec.time_offset,
                "priority": int(get("priority") or 0),
                "attributes": {
                    k: v for k, v in raw.items() if k not in cols.values() and v not in ("", None)
                },
            }
        except ValueError as exc:
            raise ValueError(f"{path}:{line}: {exc}") from exc

    def start(self, ctx: SimContext) -> None:
        rows = sorted(self._rows(), key=lambda r: r["release_time"])
        for n, row in enumerate(rows, start=1):
            task = Task(**row)
            ctx.scheduler.schedule(
                task.release_time, ARRIVAL, Arrival(self.name, task, n), Priority.RELEASE
            )
        self.released = len(rows)


def make_source(spec: DemandSpec) -> DemandSource:
    if isinstance(spec, StochasticDemand):
        return StochasticSource(spec)
    if isinstance(spec, ListDemand):
        return ListSource(spec)
    return ReplaySource(spec)

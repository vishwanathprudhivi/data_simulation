"""Deterministic future-event list (FR-ENG-02/03, BRD 7.3).

Ordering key is ``(sim_time, priority, sequence_number)``; sequence numbers are
assigned in scheduling order, so ties never depend on hashing or wall-clock.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field

__all__ = ["EventScheduler", "Priority", "ScheduledEvent"]


class Priority:
    """Conventional priorities (lower runs first at equal sim-time)."""

    DISRUPTION = 10
    RELEASE = 20
    DEFAULT = 50
    COMPLETION = 40
    DISPATCH = 80
    OBSERVE = 90


@dataclass(frozen=True, order=True)
class ScheduledEvent:
    time: float
    priority: int
    seq: int
    kind: str = field(compare=False)
    payload: object = field(compare=False, default=None)


class EventScheduler:
    def __init__(self) -> None:
        self._heap: list[ScheduledEvent] = []
        self._seq = 0
        self._cancelled: set[int] = set()

    def schedule(
        self, time: float, kind: str, payload: object = None, priority: int = Priority.DEFAULT
    ) -> ScheduledEvent:
        """Schedule ``kind`` at absolute sim-time ``time``; returns a handle for cancel."""
        if math.isnan(time) or math.isinf(time):
            raise ValueError(f"cannot schedule {kind!r} at non-finite time {time}")
        ev = ScheduledEvent(float(time), int(priority), self._seq, kind, payload)
        self._seq += 1
        heapq.heappush(self._heap, ev)
        return ev

    def cancel(self, event: ScheduledEvent) -> None:
        self._cancelled.add(event.seq)

    def _drop_cancelled(self) -> None:
        while self._heap and self._heap[0].seq in self._cancelled:
            self._cancelled.discard(heapq.heappop(self._heap).seq)

    def peek_time(self) -> float | None:
        self._drop_cancelled()
        return self._heap[0].time if self._heap else None

    def pop(self) -> ScheduledEvent:
        self._drop_cancelled()
        if not self._heap:
            raise IndexError("pop from empty scheduler")
        return heapq.heappop(self._heap)

    def __len__(self) -> int:
        return len(self._heap) - len(self._cancelled)

    @property
    def scheduled_count(self) -> int:
        """Total events ever scheduled (next sequence number)."""
        return self._seq

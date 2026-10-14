"""Simulation clock (FR-ENG-02). Sim-time is independent of wall-time."""

from __future__ import annotations

__all__ = ["SimClock"]


class SimClock:
    def __init__(self, start: float = 0.0) -> None:
        self._now = float(start)
        self.start = float(start)

    @property
    def now(self) -> float:
        return self._now

    def advance_to(self, time: float) -> None:
        if time < self._now:
            raise ValueError(f"clock cannot go backwards: {time} < {self._now}")
        self._now = float(time)

    @property
    def elapsed(self) -> float:
        return self._now - self.start

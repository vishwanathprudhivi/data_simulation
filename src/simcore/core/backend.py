"""``WorldBackend``: the execution substrate abstraction (FR-ENG-09, BRD 5.1/7.4).

The core and domains only talk to the world through this interface, so the same
scenario runs in ``logical`` (pure Python), ``kinematic`` and ``physics``
(Isaac Sim) modes. Implementations live in ``simcore.adapters``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

from simcore.specs.facility import FacilityManifest

__all__ = ["MoveSegment", "WorldBackend"]

Position = tuple[float, ...]


@dataclass(frozen=True)
class MoveSegment:
    """Straight-line motion of one agent between two positions over a sim-time interval."""

    agent_id: str
    start_time: float
    end_time: float
    start: Position
    end: Position

    def at(self, time: float) -> Position:
        if self.end_time <= self.start_time:
            return self.end
        f = min(max((time - self.start_time) / (self.end_time - self.start_time), 0.0), 1.0)
        return tuple(a + (b - a) * f for a, b in zip(self.start, self.end))


class WorldBackend(ABC):
    """Abstract world. All methods take sim-time; none may read wall-clock."""

    mode: ClassVar[str]

    @abstractmethod
    def build(self, facility: FacilityManifest | None) -> None:
        """Load the facility (stage + manifest)."""

    @abstractmethod
    def spawn_agent(self, agent_id: str, agent_type: str, position: Position | None) -> None:
        """Create the agent's representation (asset binding happens in the adapter)."""

    @abstractmethod
    def move(self, agent_id: str, waypoints: Sequence[Position], times: Sequence[float]) -> None:
        """Move ``agent_id`` through ``waypoints`` reaching each at the matching sim-time."""

    @abstractmethod
    def position(self, agent_id: str, time: float) -> Position | None:
        """Agent position at sim-time ``time`` (``None`` if unknown)."""

    def step(self, time: float, dt: float) -> None:  # noqa: B027 - optional hook
        """Advance backend state to ``time`` (fixed-step mode)."""

    def teardown(self) -> None:  # noqa: B027 - optional hook
        """Release resources."""

    def describe(self) -> dict[str, Any]:
        """Backend provenance for run metadata (FR-OBS-06)."""
        return {"mode": self.mode}

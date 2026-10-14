"""``InteractionModel``: geometry-derived lanes, passing and effective speed (FR-INT-02).

The model is pluggable; the movement system asks it three questions: how many
lanes an edge offers an agent, whether two agents can pass side by side, and
what speed an agent achieves given who else is on the edge.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass

__all__ = ["EdgeOccupant", "InteractionModel"]


@dataclass(frozen=True)
class EdgeOccupant:
    agent_id: str
    footprint_width: float
    direction: int  # +1 along the edge, -1 against, 0 stationary


class InteractionModel(ABC):
    @abstractmethod
    def lanes(self, edge_width: float, footprint_width: float) -> int:
        """Side-by-side lanes available to agents of this width (>= 1)."""

    @abstractmethod
    def can_pass(self, edge_width: float, mover_width: float, other_width: float) -> bool:
        """Whether a mover fits past another agent (opposing or stationary)."""

    @abstractmethod
    def effective_speed(
        self,
        free_speed: float,
        edge_width: float,
        directed: bool,
        mover: EdgeOccupant,
        others: Sequence[EdgeOccupant],
    ) -> float:
        """Speed (m/s) of ``mover`` on an edge shared with ``others``."""

"""``InteractionModel``: geometry-derived effective speed and blocking (FR-INT-02).

M3 provides implementations; this module fixes the contract early.
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
    direction: int  # +1 along the edge, -1 against


class InteractionModel(ABC):
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

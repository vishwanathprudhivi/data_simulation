"""Default geometric interaction model (FR-INT-02).

* lanes = floor((width + clearance) / (footprint + clearance)), at least 1
* passing (opposing or around a stationary agent) needs
  ``width >= mover + other + clearance``
* speed = free x max(min_factor, (1 - alpha x others_width / width) x passing_factor?)
  where the passing factor applies only when an opposing or stationary agent is
  present. Speed is fixed at edge entry (link-entry approximation, ADR-007).
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from simcore.core.interaction.base import EdgeOccupant, InteractionModel
from simcore.specs.scenario import InteractionSpec

__all__ = ["GeometricInteractionModel"]


class GeometricInteractionModel(InteractionModel):
    def __init__(self, spec: InteractionSpec | None = None) -> None:
        self.spec = spec or InteractionSpec()

    def lanes(self, edge_width: float, footprint_width: float) -> int:
        c = self.spec.clearance
        return max(1, math.floor((edge_width + c) / (footprint_width + c) + 1e-9))

    def can_pass(self, edge_width: float, mover_width: float, other_width: float) -> bool:
        return edge_width + 1e-9 >= mover_width + other_width + self.spec.clearance

    def effective_speed(
        self,
        free_speed: float,
        edge_width: float,
        directed: bool,
        mover: EdgeOccupant,
        others: Sequence[EdgeOccupant],
    ) -> float:
        if not others:
            return free_speed
        occupied = sum(o.footprint_width for o in others)
        factor = 1.0 - self.spec.density_alpha * occupied / edge_width
        if any(o.direction != mover.direction for o in others):
            factor *= self.spec.passing_speed_factor
        return free_speed * max(self.spec.min_speed_factor, factor)

"""Interaction and congestion: movement, blocking, deadlocks, co-location (FR-INT-*)."""

from simcore.core.interaction.base import EdgeOccupant, InteractionModel
from simcore.core.interaction.geometric import GeometricInteractionModel
from simcore.core.interaction.kpis import InteractionKpis, edge_utilization
from simcore.core.interaction.movement import MovementHooks, MovementSystem
from simcore.core.interaction.service import ServiceSystem

INTERACTION_MODELS = {"geometric": GeometricInteractionModel}

__all__ = [
    "INTERACTION_MODELS",
    "EdgeOccupant",
    "GeometricInteractionModel",
    "InteractionKpis",
    "InteractionModel",
    "MovementHooks",
    "MovementSystem",
    "ServiceSystem",
    "edge_utilization",
]

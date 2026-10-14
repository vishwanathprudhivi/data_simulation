"""Runtime work item (FR-DSP-01)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from simcore.specs.scenario import TaskSpec

__all__ = ["Task"]


@dataclass(frozen=True)
class Task:
    id: str
    type: str = "task"
    origin: str | None = None
    destination: str | None = None
    quantity: float = 1.0
    release_time: float = 0.0
    due_time: float | None = None
    priority: int = 0
    attributes: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_spec(cls, spec: TaskSpec) -> Task:
        return cls(
            id=spec.id,
            type=spec.type,
            origin=spec.origin,
            destination=spec.destination,
            quantity=spec.quantity,
            release_time=spec.release_time,
            due_time=spec.due_time,
            priority=spec.priority,
            attributes=dict(spec.attributes),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "origin": self.origin,
            "destination": self.destination,
            "quantity": self.quantity,
            "release_time": self.release_time,
            "due_time": self.due_time,
            "priority": self.priority,
            "attributes": dict(self.attributes),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Task:
        return cls(**{**data, "attributes": dict(data.get("attributes") or {})})

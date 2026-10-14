"""Entity registry with stable ids and tag/attribute/spatial lookup (FR-ENG-04).

Iteration order is insertion order, so lookups are deterministic.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = ["Entity", "EntityRegistry", "Region"]

Position = tuple[float, ...]


@dataclass
class Entity:
    id: str
    type: str
    tags: tuple[str, ...] = ()
    attributes: dict[str, Any] = field(default_factory=dict)
    position: Position | None = None

    def has_tag(self, tag: str) -> bool:
        return tag in self.tags


@dataclass(frozen=True)
class Region:
    """Axis-aligned box in the x/y plane (metres)."""

    min_x: float
    min_y: float
    max_x: float
    max_y: float

    def contains(self, position: Position) -> bool:
        x, y = position[0], position[1]
        return self.min_x <= x <= self.max_x and self.min_y <= y <= self.max_y


class EntityRegistry:
    def __init__(self) -> None:
        self._entities: dict[str, Entity] = {}
        self._counters: dict[str, int] = {}

    def next_id(self, prefix: str) -> str:
        """Stable, sequential id ``<prefix>-<n>`` (zero-padded to 4 digits)."""
        n = self._counters.get(prefix, 0)
        self._counters[prefix] = n + 1
        return f"{prefix}-{n:04d}"

    def add(self, entity: Entity) -> Entity:
        if entity.id in self._entities:
            raise ValueError(f"duplicate entity id {entity.id!r}")
        self._entities[entity.id] = entity
        return entity

    def create(
        self,
        type_: str,
        *,
        prefix: str | None = None,
        tags: tuple[str, ...] = (),
        attributes: Mapping[str, Any] | None = None,
        position: Position | None = None,
    ) -> Entity:
        return self.add(
            Entity(self.next_id(prefix or type_), type_, tags, dict(attributes or {}), position)
        )

    def get(self, entity_id: str) -> Entity:
        try:
            return self._entities[entity_id]
        except KeyError:
            raise KeyError(f"no entity with id {entity_id!r}") from None

    def __contains__(self, entity_id: object) -> bool:
        return entity_id in self._entities

    def __iter__(self) -> Iterator[Entity]:
        return iter(self._entities.values())

    def __len__(self) -> int:
        return len(self._entities)

    def by_type(self, type_: str) -> list[Entity]:
        return [e for e in self._entities.values() if e.type == type_]

    def by_tag(self, tag: str) -> list[Entity]:
        return [e for e in self._entities.values() if tag in e.tags]

    def by_attribute(self, name: str, value: Any) -> list[Entity]:
        return [e for e in self._entities.values() if e.attributes.get(name) == value]

    def in_region(self, region: Region) -> list[Entity]:
        return [
            e
            for e in self._entities.values()
            if e.position is not None and region.contains(e.position)
        ]

    def select(self, selector: str) -> list[Entity]:
        """Resolve ``<id>``, ``type:<type>`` or ``tag:<tag>`` to entities."""
        if selector.startswith("type:"):
            return self.by_type(selector[5:])
        if selector.startswith("tag:"):
            return self.by_tag(selector[4:])
        return [self.get(selector)]

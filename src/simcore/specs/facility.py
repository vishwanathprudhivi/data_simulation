"""Facility manifest: semantic sidecar to a USD stage (FR-FAC-01/02/07, BRD 7.1).

Kinds (zone kinds, location kinds) are free strings whose meaning is given by
the domain definition; the core attaches no semantics to them.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field, model_validator

from simcore.specs.common import Identifier, Length, SchemaVersion, SpecModel, Speed

__all__ = ["EdgeSpec", "FacilityManifest", "LocationSpec", "NodeSpec", "ZoneSpec"]

Point = tuple[float, float] | tuple[float, float, float]


class ZoneSpec(SpecModel):
    id: Identifier
    kind: str = Field(description="Domain-defined semantic tag, e.g. a zone type.")
    polygon: list[tuple[float, float]] = Field(
        default_factory=list, description="Floor-plan polygon in metres (x, y)."
    )
    capacity: int | None = Field(default=None, ge=0)
    attributes: dict[str, Any] = Field(default_factory=dict)


class NodeSpec(SpecModel):
    id: Identifier
    position: Point
    zone: Identifier | None = None


class LocationSpec(SpecModel):
    """A place where work happens; attached to a navigation node."""

    id: Identifier
    kind: str
    node: Identifier = Field(description="Navigation node this location is reached from.")
    zone: Identifier | None = None
    capacity: int = Field(default=1, ge=0)
    attributes: dict[str, Any] = Field(default_factory=dict)


class EdgeSpec(SpecModel):
    id: Identifier
    source: Identifier = Field(alias="from")
    target: Identifier = Field(alias="to")
    length: Length | None = Field(
        default=None, description="Metres; computed from node positions when omitted."
    )
    width: Length = Field(description="Clear traversable width in metres.")
    directed: bool = Field(default=False, description="True for one-way segments.")
    max_speed: Speed | None = None
    zone: Identifier | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class FacilityManifest(SpecModel):
    """``facility.yaml``. Immutable during a run except via scenario events (FR-FAC-03)."""

    schema_version: SchemaVersion = "1.0"
    id: Identifier
    name: str = ""
    version: str = Field(default="0.1.0", description="Manifest version (FR-FAC-07).")
    usd_stage: str | None = Field(
        default=None, description="Path to the USD stage; optional in logical mode."
    )
    zones: list[ZoneSpec] = Field(default_factory=list)
    nodes: list[NodeSpec] = Field(default_factory=list)
    edges: list[EdgeSpec] = Field(default_factory=list)
    locations: list[LocationSpec] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _unique_ids(self) -> FacilityManifest:
        for label, items in (
            ("zones", self.zones),
            ("nodes", self.nodes),
            ("edges", self.edges),
            ("locations", self.locations),
        ):
            seen: set[str] = set()
            for item in items:
                if item.id in seen:
                    raise ValueError(f"duplicate id {item.id!r} in {label}; ids must be unique")
                seen.add(item.id)
        return self

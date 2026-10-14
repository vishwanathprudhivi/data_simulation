"""Warehouse layouts built on the generic corridor generator (FR-FAC-06, BRD 9).

Aisles are corridors, bays are pick faces along them (each pick face occupies
the aisle segment in front of it, so a jack picking there narrows the aisle),
and staging lanes sit in front of the aisles. Each staging lane belongs to a
dock door.
"""

from __future__ import annotations

from simcore.core.facility.generate import corridor_layout
from simcore.specs.facility import FacilityManifest

__all__ = ["warehouse_layout"]


def warehouse_layout(
    *,
    facility_id: str = "dc_outbound",
    aisles: int = 6,
    bays_per_aisle: int = 12,
    bay_pitch: float = 2.7,
    aisle_width: float = 2.0,
    aisle_spacing: float = 9.0,
    cross_aisle_width: float = 4.0,
    staging_lanes: int = 4,
    lanes_per_door: int = 2,
    one_way: bool = False,
    bays_per_colocation_group: int = 2,
) -> FacilityManifest:
    m = corridor_layout(
        facility_id=facility_id,
        corridors=aisles,
        slots_per_corridor=bays_per_aisle,
        slot_pitch=bay_pitch,
        corridor_width=aisle_width,
        corridor_spacing=aisle_spacing,
        cross_width=cross_aisle_width,
        end_locations=staging_lanes,
        one_way=one_way,
        slot_kind="pick_face",
        end_kind="staging_lane",
        corridor_kind="aisle",
        slot_capacity=2,
        end_capacity=6,
        colocation_group_size=bays_per_colocation_group,
    )
    locations = []
    for loc in m.locations:
        if loc.kind == "staging_lane":
            n = int(loc.id.rsplit("_", 1)[1])
            door = f"door_{(n - 1) // lanes_per_door + 1:02d}"
            loc = loc.model_copy(update={"attributes": {**loc.attributes, "dock_door": door}})
        locations.append(loc)
    return m.model_copy(
        update={
            "name": f"Outbound DC: {aisles} aisles x {bays_per_aisle} bays",
            "locations": locations,
            "attributes": {**m.attributes, "aisle_width": aisle_width, "bay_pitch": bay_pitch},
        }
    )

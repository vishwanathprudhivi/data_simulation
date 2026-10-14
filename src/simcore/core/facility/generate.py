"""Procedural facility generator (FR-FAC-06): parallel corridors with work slots.

Domain-agnostic: produces corridors (parallel two-way or one-way edges split
into segments at each slot), a front and back cross corridor, slot locations
along each corridor, and a row of end locations along the front. Domains map
these generic kinds to their own vocabulary through the ``*_kind`` arguments.
"""

from __future__ import annotations

from typing import Any

from simcore.specs.facility import FacilityManifest

__all__ = ["corridor_layout"]


def corridor_layout(
    *,
    facility_id: str = "generated",
    corridors: int = 4,
    slots_per_corridor: int = 10,
    slot_pitch: float = 2.7,
    corridor_width: float = 3.0,
    corridor_spacing: float = 8.0,
    cross_width: float = 4.0,
    front_depth: float = 6.0,
    end_locations: int = 4,
    one_way: bool = False,
    slot_kind: str = "slot",
    end_kind: str = "end",
    corridor_kind: str = "corridor",
    slot_capacity: int = 1,
    end_capacity: int = 4,
    slots_occupy_edge: bool = True,
    colocation_group_size: int = 1,
) -> FacilityManifest:
    """Generate a manifest; all dimensions in metres.

    Corridor ``c`` runs along +y at ``x = c * corridor_spacing``; slot ``s`` sits at
    ``y = (s + 1) * slot_pitch``. With ``one_way`` corridors alternate direction
    (serpentine). ``colocation_group_size`` groups consecutive slots that share a
    co-location group (e.g. two slots facing each other across a corridor).
    """
    if corridors < 1 or slots_per_corridor < 1:
        raise ValueError("need at least one corridor and one slot")
    length = (slots_per_corridor + 1) * slot_pitch
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    locations: list[dict[str, Any]] = []
    zones: list[dict[str, Any]] = []
    half = corridor_width / 2
    for c in range(corridors):
        x = c * corridor_spacing
        zid = f"{corridor_kind}_{c + 1:02d}"
        zones.append(
            {
                "id": zid,
                "kind": corridor_kind,
                "polygon": [
                    [x - half, 0.0],
                    [x + half, 0.0],
                    [x + half, length],
                    [x - half, length],
                ],
            }
        )
        ids = (
            [f"c{c + 1:02d}_s00"]
            + [f"c{c + 1:02d}_s{s + 1:02d}" for s in range(slots_per_corridor)]
            + [f"c{c + 1:02d}_end"]
        )
        ys = [0.0] + [(s + 1) * slot_pitch for s in range(slots_per_corridor)] + [length]
        for nid, y in zip(ids, ys):
            nodes.append({"id": nid, "position": [x, y], "zone": zid})
        northbound = not one_way or c % 2 == 0
        for k in range(len(ids) - 1):
            a, b = (ids[k], ids[k + 1]) if northbound else (ids[k + 1], ids[k])
            edges.append(
                {
                    "id": f"e_c{c + 1:02d}_{k:02d}",
                    "from": a,
                    "to": b,
                    "width": corridor_width,
                    "directed": one_way,
                    "zone": zid,
                }
            )
        for s in range(slots_per_corridor):
            group = f"c{c + 1:02d}_g{s // colocation_group_size:02d}"
            attrs: dict[str, Any] = {"corridor": zid, "colocation_group": group}
            if slots_occupy_edge:
                # the slot faces the segment that leads into it
                attrs["occupies_edge"] = f"e_c{c + 1:02d}_{s:02d}"
            locations.append(
                {
                    "id": f"{slot_kind}_c{c + 1:02d}_{s + 1:02d}",
                    "kind": slot_kind,
                    "node": ids[s + 1],
                    "zone": zid,
                    "capacity": slot_capacity,
                    "attributes": attrs,
                }
            )
    # cross corridors (two-way) at the front (y=0) and back (y=length)
    for label, y_idx, suffix in (("front", 0, "s00"), ("back", -1, "end")):
        for c in range(corridors - 1):
            a, b = f"c{c + 1:02d}_{suffix}", f"c{c + 2:02d}_{suffix}"
            edges.append(
                {
                    "id": f"e_{label}_{c + 1:02d}",
                    "from": a,
                    "to": b,
                    "width": cross_width,
                    "directed": False,
                }
            )
        del y_idx
    # end locations in front of the corridors
    span = (corridors - 1) * corridor_spacing
    zones.append(
        {
            "id": f"{end_kind}_area",
            "kind": end_kind,
            "polygon": [
                [-half - 1, -front_depth],
                [span + half + 1, -front_depth],
                [span + half + 1, -1.0],
                [-half - 1, -1.0],
            ],
        }
    )
    for k in range(end_locations):
        x = span * (k + 0.5) / end_locations if corridors > 1 else 0.0
        nid = f"end_{k + 1:02d}"
        nodes.append({"id": nid, "position": [x, -front_depth / 2], "zone": f"{end_kind}_area"})
        nearest = min(range(corridors), key=lambda c: (abs(c * corridor_spacing - x), c))
        edges.append(
            {
                "id": f"e_end_{k + 1:02d}",
                "from": nid,
                "to": f"c{nearest + 1:02d}_s00",
                "width": cross_width,
                "directed": False,
            }
        )
        locations.append(
            {
                "id": f"{end_kind}_{k + 1:02d}",
                "kind": end_kind,
                "node": nid,
                "zone": f"{end_kind}_area",
                "capacity": end_capacity,
            }
        )
    return FacilityManifest.model_validate(
        {
            "id": facility_id,
            "name": f"{corridors}x{slots_per_corridor} corridor layout",
            "nodes": nodes,
            "edges": edges,
            "locations": locations,
            "zones": zones,
            "attributes": {
                "generator": "corridor_layout",
                "slot_pitch": slot_pitch,
                "corridor_width": corridor_width,
            },
        }
    )

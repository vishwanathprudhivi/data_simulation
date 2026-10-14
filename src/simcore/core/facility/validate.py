"""Facility validation (FR-FAC-04, AC-FAC-01).

Checks: dangling references, connectivity (every location reachable from every
other, honouring one-way edges), zero/negative widths, declared lengths that
contradict node geometry, overlapping zone polygons, inconsistent coordinate
dimensions, and capacities. Each issue says what, where and how to fix.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from simcore.core.facility.graph import NavGraph
from simcore.specs.facility import FacilityManifest

__all__ = ["FacilityIssue", "polygons_overlap", "validate_facility"]

Point = tuple[float, float]


@dataclass(frozen=True)
class FacilityIssue:
    severity: str  # "error" | "warning"
    code: str
    where: str
    message: str
    hint: str

    def __str__(self) -> str:
        head = f"[{self.severity}] {self.code} at {self.where}: {self.message}"
        return f"{head}\n    fix: {self.hint}"


def _segments_cross(a: Point, b: Point, c: Point, d: Point) -> bool:
    def orient(p: Point, q: Point, r: Point) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    d1, d2 = orient(c, d, a), orient(c, d, b)
    d3, d4 = orient(a, b, c), orient(a, b, d)
    return (d1 * d2 < 0) and (d3 * d4 < 0)


def _inside(p: Point, poly: Sequence[Point]) -> bool:
    x, y = p
    inside = False
    n = len(poly)
    for i in range(n):
        (x1, y1), (x2, y2) = poly[i], poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xin = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xin:
                inside = not inside
    return inside


def _area(poly: Sequence[Point]) -> float:
    return (
        abs(
            sum(
                poly[i][0] * poly[(i + 1) % len(poly)][1]
                - poly[(i + 1) % len(poly)][0] * poly[i][1]
                for i in range(len(poly))
            )
        )
        / 2
    )


def polygons_overlap(p: Sequence[Point], q: Sequence[Point]) -> bool:
    """True if the polygon interiors intersect (shared edges/corners do not count)."""
    for i in range(len(p)):
        for j in range(len(q)):
            if _segments_cross(p[i], p[(i + 1) % len(p)], q[j], q[(j + 1) % len(q)]):
                return True

    # containment (or identical polygons): test a strictly interior sample point
    def centroid(poly: Sequence[Point]) -> Point:
        return (sum(v[0] for v in poly) / len(poly), sum(v[1] for v in poly) / len(poly))

    return _inside(centroid(p), q) or _inside(centroid(q), p)


def validate_facility(m: FacilityManifest, length_tolerance: float = 0.05) -> list[FacilityIssue]:
    """Return all issues found (errors first, then warnings, each in manifest order)."""
    issues: list[FacilityIssue] = []

    def add(sev: str, code: str, where: str, msg: str, hint: str) -> None:
        issues.append(FacilityIssue(sev, code, where, msg, hint))

    nodes = {n.id: n for n in m.nodes}
    zones = {z.id for z in m.zones}
    dims = {len(n.position) for n in m.nodes}
    if len(dims) > 1:
        add(
            "error",
            "mixed_dimensions",
            "nodes",
            "node positions mix 2D and 3D coordinates",
            "give every node [x, y] or every node [x, y, z] (metres)",
        )
    for n in m.nodes:
        if n.zone is not None and n.zone not in zones:
            add(
                "error",
                "unknown_zone",
                f"nodes.{n.id}.zone",
                f"zone {n.zone!r} is not defined",
                f"define zone {n.zone!r} under 'zones' or fix the reference",
            )
    for e in m.edges:
        where = f"edges.{e.id}"
        for end, label in ((e.source, "from"), (e.target, "to")):
            if end not in nodes:
                add(
                    "error",
                    "unknown_node",
                    f"{where}.{label}",
                    f"node {end!r} does not exist",
                    "add the node under 'nodes' or fix the edge endpoint",
                )
        if e.source == e.target:
            add(
                "error",
                "self_loop",
                where,
                "edge starts and ends at the same node",
                "remove the edge or connect two different nodes",
            )
        if e.width <= 0:
            add(
                "error",
                "zero_width",
                f"{where}.width",
                f"edge width is {e.width} m; agents cannot traverse it",
                "set the clear traversable width in metres (e.g. 2.4 m)",
            )
        if e.zone is not None and e.zone not in zones:
            add(
                "error",
                "unknown_zone",
                f"{where}.zone",
                f"zone {e.zone!r} is not defined",
                "define the zone or remove the reference",
            )
        if e.source in nodes and e.target in nodes:
            geo = math.dist(nodes[e.source].position[:2], nodes[e.target].position[:2])
            if e.length is not None and geo > 0 and abs(e.length - geo) / geo > length_tolerance:
                add(
                    "warning",
                    "length_mismatch",
                    f"{where}.length",
                    f"declared length {e.length:g} m differs from node distance {geo:.3g} m by "
                    f"more than {length_tolerance:.0%} (unit error?)",
                    "check the units (lengths are metres) or remove 'length' to derive it",
                )
            if e.length is None and geo == 0:
                add(
                    "error",
                    "zero_length",
                    where,
                    "edge has coincident endpoints and no length",
                    "move a node or declare 'length'",
                )
    for loc in m.locations:
        if loc.node not in nodes:
            add(
                "error",
                "unknown_node",
                f"locations.{loc.id}.node",
                f"node {loc.node!r} does not exist",
                "attach the location to an existing node",
            )
        if loc.zone is not None and loc.zone not in zones:
            add(
                "error",
                "unknown_zone",
                f"locations.{loc.id}.zone",
                f"zone {loc.zone!r} is not defined",
                "define the zone or fix the reference",
            )
        occ = loc.attributes.get("occupies_edge")
        if occ is not None and occ not in {e.id for e in m.edges}:
            add(
                "error",
                "unknown_edge",
                f"locations.{loc.id}.attributes.occupies_edge",
                f"edge {occ!r} does not exist",
                "name an existing edge or remove the attribute",
            )
        if loc.capacity == 0:
            add(
                "warning",
                "zero_capacity",
                f"locations.{loc.id}.capacity",
                "location has capacity 0; no agent can ever work there",
                "set capacity >= 1",
            )
    for z in m.zones:
        if z.polygon and len(z.polygon) < 3:
            add(
                "error",
                "bad_polygon",
                f"zones.{z.id}.polygon",
                "polygon needs >= 3 points",
                "list at least three [x, y] vertices",
            )
        elif z.polygon and _area(z.polygon) == 0:
            add(
                "error",
                "bad_polygon",
                f"zones.{z.id}.polygon",
                "polygon has zero area",
                "check for collinear or repeated vertices",
            )
    polys = [(z.id, z.polygon) for z in m.zones if len(z.polygon) >= 3]
    for i in range(len(polys)):
        for j in range(i + 1, len(polys)):
            if polygons_overlap(polys[i][1], polys[j][1]):
                add(
                    "error",
                    "overlapping_zones",
                    f"zones.{polys[i][0]}",
                    f"zone {polys[i][0]!r} overlaps zone {polys[j][0]!r}",
                    "adjust the polygons so zones only share boundaries",
                )
    # connectivity over traversable edges
    graph = NavGraph(m)
    ok = {e.id: e.width > 0 for e in m.edges}
    loc_nodes = [(loc.id, loc.node) for loc in m.locations if loc.node in nodes]
    targets = loc_nodes if loc_nodes else [(n, n) for n in nodes]
    if targets:
        ref_id, ref = targets[0]
        fwd = graph.reachable(ref, ok)
        for lid, node in targets[1:]:
            if node not in fwd:
                add(
                    "error",
                    "unreachable",
                    f"locations.{lid}" if loc_nodes else f"nodes.{lid}",
                    f"{lid!r} cannot be reached from {ref_id!r}",
                    "connect it with a traversable edge (check widths and one-way directions)",
                )
            elif ref not in graph.reachable(node, ok):
                add(
                    "error",
                    "no_return",
                    f"locations.{lid}" if loc_nodes else f"nodes.{lid}",
                    f"{ref_id!r} cannot be reached from {lid!r} (one-way trap)",
                    "add a return path or make an edge two-way",
                )
    isolated = [
        n
        for n in nodes
        if not graph.neighbors(n) and not any(e.target == n for e in graph.edges.values())
    ]
    for node_id in isolated:
        if len(nodes) > 1:
            add(
                "error",
                "isolated_node",
                f"nodes.{node_id}",
                "node has no edges",
                "connect it or remove it",
            )
    zones_with_nodes = sorted({str(nodes[k].zone) for k in nodes if nodes[k].zone})
    if targets:
        reach = graph.reachable(targets[0][1], ok)
        for zone_id in zones_with_nodes:
            zone_nodes = [k for k in nodes if nodes[k].zone == zone_id]
            if zone_nodes and not any(k in reach for k in zone_nodes):
                add(
                    "error",
                    "disconnected_zone",
                    f"zones.{zone_id}",
                    f"no node of zone {zone_id!r} is reachable from the rest of the facility",
                    "add an edge connecting the zone (and check it has non-zero width)",
                )
    issues.sort(key=lambda i: 0 if i.severity == "error" else 1)
    return issues

"""Facility ingestion helpers (FR-DOM-07): CSV layout -> facility manifest, plus a
minimal USD scene so a 3D asset is not needed to start.

Inputs (headers are case-insensitive; extra columns go to ``attributes``):

* ``nodes.csv``: ``id, x, y[, z][, zone]``
* ``edges.csv``: ``id, from, to, width[, length][, directed][, max_speed][, zone]``
* ``zones.csv`` (optional): ``id, kind, polygon[, capacity]`` with polygon as
  ``"x1 y1; x2 y2; ..."``
* ``locations.csv`` (optional): ``id, kind, node[, zone][, capacity]``
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from simcore.core.config import ConfigError, ConfigIssue, validate_spec
from simcore.specs.facility import FacilityManifest

__all__ = ["facility_from_csv", "facility_usda"]

_TRUE = {"1", "true", "yes", "y", "t"}


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return [
            {k.strip().lower(): (v or "").strip() for k, v in r.items() if k}
            for r in csv.DictReader(fh)
        ]


def _extra(row: dict[str, str], known: set[str]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k not in known and v != ""}


def facility_from_csv(directory: Path, facility_id: str, name: str = "") -> FacilityManifest:
    """Build and validate a :class:`FacilityManifest` from layout CSVs in ``directory``."""
    issues: list[ConfigIssue] = []
    for req in ("nodes.csv", "edges.csv"):
        if not (directory / req).exists():
            issues.append(ConfigIssue(str(directory), req, "required file is missing"))
    if issues:
        raise ConfigError(issues)
    doc: dict[str, Any] = {
        "id": facility_id,
        "name": name or facility_id,
        "zones": [],
        "nodes": [],
        "edges": [],
        "locations": [],
    }
    for i, r in enumerate(_rows(directory / "nodes.csv"), start=2):
        try:
            pos = [float(r["x"]), float(r["y"])] + ([float(r["z"])] if r.get("z") else [])
        except (KeyError, ValueError):
            issues.append(ConfigIssue("nodes.csv", f"row {i}", "x and y must be numbers"))
            continue
        node: dict[str, Any] = {"id": r.get("id", ""), "position": pos}
        if r.get("zone"):
            node["zone"] = r["zone"]
        doc["nodes"].append(node)
    for i, r in enumerate(_rows(directory / "edges.csv"), start=2):
        if not r.get("width"):
            issues.append(ConfigIssue("edges.csv", f"row {i}", "width is required (metres)"))
            continue
        edge: dict[str, Any] = {
            "id": r.get("id") or f"e{i - 1}",
            "from": r.get("from", ""),
            "to": r.get("to", ""),
            "width": float(r["width"]),
            "directed": r.get("directed", "").lower() in _TRUE,
        }
        for k in ("length", "max_speed"):
            if r.get(k):
                edge[k] = float(r[k])
        if r.get("zone"):
            edge["zone"] = r["zone"]
        attrs = _extra(r, {"id", "from", "to", "width", "directed", "length", "max_speed", "zone"})
        if attrs:
            edge["attributes"] = attrs
        doc["edges"].append(edge)
    if (directory / "zones.csv").exists():
        for i, r in enumerate(_rows(directory / "zones.csv"), start=2):
            try:
                poly = [
                    [float(v) for v in pt.split()]
                    for pt in r.get("polygon", "").split(";")
                    if pt.strip()
                ]
            except ValueError:
                issues.append(
                    ConfigIssue("zones.csv", f"row {i}", "polygon must be 'x y; x y; ...'")
                )
                continue
            zone: dict[str, Any] = {
                "id": r.get("id", ""),
                "kind": r.get("kind", "zone"),
                "polygon": poly,
            }
            if r.get("capacity"):
                zone["capacity"] = int(r["capacity"])
            doc["zones"].append(zone)
    if (directory / "locations.csv").exists():
        for r in _rows(directory / "locations.csv"):
            loc: dict[str, Any] = {
                "id": r.get("id", ""),
                "kind": r.get("kind", "location"),
                "node": r.get("node", ""),
            }
            if r.get("zone"):
                loc["zone"] = r["zone"]
            if r.get("capacity"):
                loc["capacity"] = int(r["capacity"])
            attrs = _extra(r, {"id", "kind", "node", "zone", "capacity"})
            if attrs:
                loc["attributes"] = attrs
            doc["locations"].append(loc)
    if issues:
        raise ConfigError(issues)
    manifest = validate_spec(FacilityManifest, doc, str(directory))
    from simcore.core.facility.validate import validate_facility

    problems = [i for i in validate_facility(manifest) if i.severity == "error"]
    if problems:
        raise ConfigError(
            [
                ConfigIssue(str(directory), p.where, f"{p.code}: {p.message}", p.hint)
                for p in problems
            ]
        )
    return manifest


def facility_usda(manifest: FacilityManifest, height: float = 0.05) -> str:
    """Minimal USD (ASCII ``.usda``) scene: floor slabs for zones and edges.

    Plain text, so no USD library is needed; open it in any USD viewer or reference
    it from the facility manifest's ``usd_stage`` to start in ``kinematic`` mode.
    """
    from simcore.core.facility.graph import NavGraph

    nav = NavGraph(manifest)
    out = [
        "#usda 1.0",
        "(",
        '    defaultPrim = "Facility"',
        "    metersPerUnit = 1",
        '    upAxis = "Z"',
        ")",
        "",
        'def Xform "Facility"',
        "{",
    ]

    def box(name: str, cx: float, cy: float, sx: float, sy: float, z: float, color: str) -> None:
        out.extend(
            [
                f'    def Cube "{name}"',
                "    {",
                "        double size = 1",
                f"        float3 xformOp:scale = ({sx:.4f}, {sy:.4f}, {height:.4f})",
                f"        double3 xformOp:translate = ({cx:.4f}, {cy:.4f}, {z:.4f})",
                '        uniform token[] xformOpOrder = ["xformOp:translate", "xformOp:scale"]',
                f"        color3f[] primvars:displayColor = [{color}]",
                "    }",
            ]
        )

    for z in manifest.zones:
        xs = [p[0] for p in z.polygon]
        ys = [p[1] for p in z.polygon]
        if xs and ys:
            box(
                f"zone_{z.id}",
                (min(xs) + max(xs)) / 2,
                (min(ys) + max(ys)) / 2,
                max(xs) - min(xs),
                max(ys) - min(ys),
                0.0,
                "(0.75, 0.8, 0.9)",
            )
    import math

    for e in nav.edges.values():
        a, b = nav.positions[e.source], nav.positions[e.target]
        length = math.dist(a[:2], b[:2])
        if length <= 0:
            continue
        angle = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))
        out.extend(
            [
                f'    def Cube "edge_{e.id}"',
                "    {",
                "        double size = 1",
                f"        float3 xformOp:scale = ({length:.4f}, {e.width:.4f}, {height:.4f})",
                f"        float xformOp:rotateZ = {angle:.4f}",
                f"        double3 xformOp:translate = ({(a[0] + b[0]) / 2:.4f}, "
                f"{(a[1] + b[1]) / 2:.4f}, {height:.4f})",
                '        uniform token[] xformOpOrder = ["xformOp:translate", "xformOp:rotateZ", '
                '"xformOp:scale"]',
                "        color3f[] primvars:displayColor = [(0.55, 0.55, 0.55)]",
                "    }",
            ]
        )
    out.append("}")
    return "\n".join(out) + "\n"

"""Facility layer: graph, routing, validation, generation (FR-FAC-01..07, AC-FAC-01)."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from simcore.cli.main import cli
from simcore.core.config import load_spec
from simcore.core.facility import NavGraph, NoRouteError, validate_facility
from simcore.core.facility.generate import corridor_layout
from simcore.core.facility.validate import polygons_overlap
from simcore.specs import FacilityManifest
from tests.conftest import EXAMPLES
from tests.support.courier import corridor


def test_AC_FAC_01_broken_facility_flagged_with_actionable_messages() -> None:
    res = CliRunner().invoke(cli, ["validate", str(EXAMPLES / "facilities/broken.yaml")])
    assert res.exit_code == 1
    out = res.output
    assert "zero_width at edges.bc.width" in out and "set the clear traversable width" in out
    assert "disconnected_zone at zones.island" in out and "add an edge connecting" in out
    assert "unreachable at locations.far" in out  # only reachable through the zero-width edge


def test_FR_FAC_04_validator_checks() -> None:
    m = FacilityManifest.model_validate(
        {
            "id": "x",
            "zones": [
                {"id": "z1", "kind": "k", "polygon": [[0, 0], [4, 0], [4, 4], [0, 4]]},
                {"id": "z2", "kind": "k", "polygon": [[2, 2], [6, 2], [6, 6], [2, 6]]},
            ],
            "nodes": [
                {"id": "a", "position": [0, 0]},
                {"id": "b", "position": [10, 0, 0]},
                {"id": "lonely", "position": [5, 5]},
            ],
            "edges": [
                {"id": "ab", "from": "a", "to": "b", "width": 2, "length": 100},
                {"id": "bq", "from": "b", "to": "q", "width": 2},
            ],
            "locations": [{"id": "L", "kind": "k", "node": "nowhere"}],
        }
    )
    codes = {i.code for i in validate_facility(m)}
    assert {
        "mixed_dimensions",
        "length_mismatch",
        "unknown_node",
        "overlapping_zones",
        "isolated_node",
    } <= codes


def test_polygon_overlap_rules() -> None:
    sq = [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)]
    assert polygons_overlap(sq, [(1.0, 1.0), (3.0, 1.0), (3.0, 3.0), (1.0, 3.0)])
    assert not polygons_overlap(sq, [(2.0, 0.0), (4.0, 0.0), (4.0, 2.0), (2.0, 2.0)])  # touching
    assert polygons_overlap(sq, [(0.5, 0.5), (1.0, 0.5), (1.0, 1.0)])  # contained


def test_FR_FAC_02_routing_and_one_way() -> None:
    m = corridor(
        {"a": (0, 0), "b": (10, 0), "c": (10, 10)},
        [("ab", "a", "b", 2), ("bc", "b", "c", 2), ("ca", "c", "a", 2)],
        directed=("ab", "bc", "ca"),
    )
    g = NavGraph(m)
    assert g.route("at_a", "at_c").nodes == ("a", "b", "c")
    assert g.route("at_c", "at_b").nodes == ("c", "a", "b")  # one-way: the long way round
    assert g.edges["ca"].length == pytest.approx(14.1421, rel=1e-4)  # derived from geometry
    g.block(["bc"], True)
    with pytest.raises(NoRouteError, match="no route"):
        g.route("at_a", "at_c")
    g.block(["bc"], False)
    assert g.route("at_a", "at_c").length == pytest.approx(20.0)


def test_FR_FAC_03_overlay_degrade_is_reversible() -> None:
    g = NavGraph(corridor({"a": (0, 0), "b": (5, 0)}, [("ab", "a", "b", 2)]))
    g.degrade(["ab"], 0.5, True)
    assert g.speed_factor == {"ab": 0.5}
    g.degrade(["ab"], 0.5, False)
    assert g.speed_factor == {}
    assert g.edges_for("at_a") == ["ab"]


@pytest.mark.parametrize("one_way", [False, True])
def test_FR_FAC_06_generated_layouts_are_valid(one_way: bool) -> None:
    m = corridor_layout(corridors=5, slots_per_corridor=6, one_way=one_way, end_locations=3)
    assert validate_facility(m) == []
    g = NavGraph(m)
    for loc in m.locations:
        g.route("end_01", loc.id)
        g.route(loc.id, "end_03")
    slots = [loc for loc in m.locations if loc.kind == "slot"]
    assert len(slots) == 30 and all("occupies_edge" in s.attributes for s in slots)


def test_FR_FAC_05_multiple_facilities_by_config() -> None:
    a = load_spec(FacilityManifest, EXAMPLES / "facilities/two_aisle.yaml")
    b = corridor_layout(facility_id="other")
    assert validate_facility(a) == [] and validate_facility(b) == []
    assert NavGraph(a).manifest.id != NavGraph(b).manifest.id

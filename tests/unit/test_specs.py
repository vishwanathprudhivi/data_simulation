"""M0 exit gate: schemas validate sample configs; spec contracts behave (BRD 7.1, 7.7)."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from pydantic import TypeAdapter, ValidationError

from simcore.core.config import _resolve_extends, load_document
from simcore.core.units import UnitError, to_si
from simcore.specs import (
    SPECS,
    ChartSpec,
    DomainDefinition,
    ExperimentSpec,
    FacilityManifest,
    Findings,
    QuerySpec,
    ScenarioSpec,
    schema_for,
)
from simcore.specs.distributions import DistributionSpec
from simcore.specs.domain import derive_catalogs
from tests.conftest import EXAMPLES, ROOT

SCHEMA_DIR = ROOT / "schemas"

SAMPLES: list[tuple[str, Path]] = [
    ("scenario", EXAMPLES / "scenarios/toy_queue/mmc_baseline.yaml"),
    ("scenario", EXAMPLES / "scenarios/toy_queue/mmc_four_servers.yaml"),
    *[
        ("scenario", p)
        for p in sorted((EXAMPLES / "scenarios/warehouse").glob("*.yaml"))
        if p.name != "behaviors.yaml"
    ],
    ("behaviors", EXAMPLES / "scenarios/toy_queue/behaviors.yaml"),
    ("behaviors", EXAMPLES / "scenarios/warehouse/behaviors.yaml"),
    *[("facility", p) for p in sorted((EXAMPLES / "facilities").glob("*.yaml"))],
    *[("experiment", p) for p in sorted((EXAMPLES / "experiments").glob("*.yaml"))],
    ("domain_definition", ROOT / "domains/toy_queue/domain.yaml"),
]


def test_M0_checked_in_schemas_match_models() -> None:
    """schemas/ is generated; regenerate with `simcore schemas export` if this fails."""
    for name in SPECS:
        on_disk = json.loads((SCHEMA_DIR / f"{name}.schema.json").read_text())
        assert on_disk == schema_for(name), f"schemas/{name}.schema.json is stale"


@pytest.mark.parametrize(("kind", "path"), SAMPLES, ids=[p.name for _, p in SAMPLES])
def test_M0_sample_configs_validate_against_json_schema(kind: str, path: Path) -> None:
    doc = _resolve_extends(path) if kind == "scenario" else load_document(path)
    schema = json.loads((SCHEMA_DIR / f"{kind}.schema.json").read_text())
    jsonschema.Draft202012Validator(schema).validate(doc)
    SPECS[kind].model_validate(doc)


def test_M0_schemas_are_versioned() -> None:
    for name in SPECS:
        schema = schema_for(name)
        assert schema["$id"] == f"urn:simcore:schema:{name}:1.0"
        assert schema["x-schema-version"] == "1.0"


@pytest.mark.parametrize(
    ("value", "dim", "expected"),
    [
        ("30 min", "time", 1800.0),
        ("2 h", "time", 7200.0),
        (5, "time", 5.0),
        ("1.2 km/h", "speed", 1200 / 3600),
        ("240 cm", "length", 2.4),
        ("3 ft", "length", 0.9144),
    ],
)
def test_units_convert_to_si(value: object, dim: str, expected: float) -> None:
    assert to_si(value, dim) == pytest.approx(expected)  # type: ignore[arg-type]


def test_units_reject_wrong_dimension_with_hint() -> None:
    with pytest.raises(UnitError, match="not a time unit; accepted"):
        to_si("3 m", "time")
    with pytest.raises(UnitError, match="cannot parse"):
        to_si("soon", "time")


def test_distribution_units_converted_at_load() -> None:
    ta: TypeAdapter[object] = TypeAdapter(DistributionSpec)
    d = ta.validate_python({"kind": "gamma", "shape": 2, "scale": 1.5, "unit": "min"})
    assert d.scale == pytest.approx(90.0) and d.unit is None  # type: ignore[attr-defined]


def test_scenario_rejects_unknown_fields_and_bad_warmup() -> None:
    base = {"id": "s", "domain": "d", "duration": 10}
    with pytest.raises(ValidationError, match="Extra inputs"):
        ScenarioSpec.model_validate({**base, "durration": 5})
    with pytest.raises(ValidationError, match="warmup"):
        ScenarioSpec.model_validate({**base, "warmup": 10})


def test_disruption_needs_exactly_one_of_at_or_trigger() -> None:
    base = {"id": "s", "domain": "d", "duration": 10}
    with pytest.raises(ValidationError, match="exactly one"):
        ScenarioSpec.model_validate({**base, "disruptions": [{"id": "x", "type": "demand_surge"}]})


def test_experiment_reserved_baseline_id() -> None:
    with pytest.raises(ValidationError, match="reserved"):
        ExperimentSpec.model_validate(
            {"id": "e", "base_scenario": "s.yaml", "variants": [{"id": "baseline"}]}
        )


def test_facility_duplicate_ids_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate id"):
        FacilityManifest.model_validate(
            {"id": "f", "nodes": [{"id": "a", "position": [0, 0]}, {"id": "a", "position": [1, 0]}]}
        )


def test_FR_VIZ_01_chart_spec_cannot_inline_data() -> None:
    q = {"metrics": ["wait_time_mean"], "dimensions": ["variant"]}
    ChartSpec.model_validate({"template": "bar_ci", "query": q})
    for key in ("data", "values", "datasets"):
        with pytest.raises(ValidationError, match="Extra inputs"):
            ChartSpec.model_validate({"template": "bar_ci", "query": q, key: [{"y": 1.0}]})
    with pytest.raises(ValidationError):
        ChartSpec.model_validate({"template": "pie", "query": q})


def test_query_spec_requires_metrics() -> None:
    with pytest.raises(ValidationError):
        QuerySpec.model_validate({"metrics": []})


def test_findings_example_from_brd_validates() -> None:
    Findings.model_validate(
        {
            "experiment_id": "exp_0042",
            "baseline": "scn_base",
            "fidelity": {"screen": "logical", "promoted": ["var_b"], "promoted_mode": "kinematic"},
            "n_replications": 20,
            "confidence": 0.95,
            "comparisons": [
                {
                    "variant": "var_b",
                    "metric": "dock_on_time_rate",
                    "paired_diff": {"mean": 0.031, "ci": [0.012, 0.050]},
                    "ci_excludes_zero": True,
                    "significant": True,
                    "ref_id": "q_9f3a",
                }
            ],
            "drivers": [
                {"metric": "bay_colocation_minutes", "share_of_delta": 0.46, "ref_id": "q_a71c"}
            ],
            "caveats": ["var_c fleet size outside calibrated envelope"],
        }
    )


def test_FR_DOM_03_catalogs_derived_from_domain_definition() -> None:
    d = DomainDefinition.model_validate(load_document(ROOT / "domains/toy_queue/domain.yaml"))
    levers, metrics, dims = derive_catalogs(d)
    assert [lv.id for lv in levers.levers] == ["servers", "mean_interarrival"]
    assert metrics.domain == "toy_queue" and metrics.domain_version == "0.1.0"
    assert {m.id for m in metrics.metrics} == {"wait_time_mean", "utilization"}
    assert {x.id for x in dims.dimensions} == {"variant", "replication"}


def test_domain_definition_cross_reference_errors() -> None:
    base = {"id": "d", "name": "D", "version": "1", "tier": 0}
    with pytest.raises(ValidationError, match="undeclared dimensions"):
        DomainDefinition.model_validate(
            {
                **base,
                "metrics": [
                    {
                        "id": "m",
                        "display_name": "M",
                        "unit": "s",
                        "formula": "m",
                        "allowed_dimensions": ["zone"],
                    }
                ],
            }
        )
    with pytest.raises(ValidationError, match="must name a 'plugin'"):
        DomainDefinition.model_validate({**base, "tier": 1})

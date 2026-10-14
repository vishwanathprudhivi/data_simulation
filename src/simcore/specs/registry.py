"""Registry of spec models and JSON Schema export (BRD 7.7, FR-API-08)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from simcore.specs.behaviors import BehaviorsSpec
from simcore.specs.chart import ChartSpec
from simcore.specs.common import SCHEMA_VERSION
from simcore.specs.domain import (
    CapabilityManifest,
    DimensionCatalog,
    DomainDefinition,
    LeverCatalog,
    MetricCatalog,
)
from simcore.specs.edit import ScenarioEdit
from simcore.specs.experiment import ExperimentSpec
from simcore.specs.facility import FacilityManifest
from simcore.specs.findings import Findings
from simcore.specs.plan import PlanSpec
from simcore.specs.query import QuerySpec
from simcore.specs.scenario import ScenarioSpec

__all__ = ["SPECS", "export_schemas", "schema_for"]

#: name -> model. The four config documents first, then the contract specs.
SPECS: dict[str, type[BaseModel]] = {
    "facility": FacilityManifest,
    "behaviors": BehaviorsSpec,
    "scenario": ScenarioSpec,
    "experiment": ExperimentSpec,
    "domain_definition": DomainDefinition,
    "lever_catalog": LeverCatalog,
    "metric_catalog": MetricCatalog,
    "dimension_catalog": DimensionCatalog,
    "capability_manifest": CapabilityManifest,
    "query_spec": QuerySpec,
    "chart_spec": ChartSpec,
    "findings": Findings,
    "plan": PlanSpec,
    "scenario_edit": ScenarioEdit,
}


def schema_for(name: str) -> dict[str, Any]:
    """Return the JSON Schema (draft 2020-12) for spec ``name``."""
    try:
        model = SPECS[name]
    except KeyError:
        raise KeyError(f"unknown spec {name!r}; known: {sorted(SPECS)}") from None
    schema = model.model_json_schema(by_alias=True, mode="validation")
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"urn:simcore:schema:{name}:{SCHEMA_VERSION}",
        "x-schema-version": SCHEMA_VERSION,
        **schema,
    }


def export_schemas(directory: Path) -> list[Path]:
    """Write every spec schema to ``directory/<name>.schema.json``; return the paths."""
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name in SPECS:
        path = directory / f"{name}.schema.json"
        path.write_text(json.dumps(schema_for(name), indent=2, sort_keys=True) + "\n")
        written.append(path)
    return written

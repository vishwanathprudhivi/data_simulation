"""Versioned spec contracts: the narrow waist (BRD 7.1, 7.7).

Every document exchanged between engine, services, frontend and LLM tools is one
of these Pydantic v2 models. JSON Schemas in ``schemas/`` are generated from
them (``simcore schemas export``); frontend types and LLM tool schemas are
generated from those, never hand-copied.
"""

from simcore.specs.behaviors import AgentTypeSpec, BehaviorsSpec
from simcore.specs.chart import ChartSpec
from simcore.specs.common import SCHEMA_VERSION, SpecModel
from simcore.specs.distributions import DistributionSpec
from simcore.specs.domain import (
    DimensionCatalog,
    DomainDefinition,
    LeverCatalog,
    MetricCatalog,
)
from simcore.specs.experiment import ExperimentSpec
from simcore.specs.facility import FacilityManifest
from simcore.specs.findings import Findings
from simcore.specs.plan import PlanSpec
from simcore.specs.query import QuerySpec
from simcore.specs.registry import SPECS, export_schemas, schema_for
from simcore.specs.scenario import ScenarioSpec

__all__ = [
    "SCHEMA_VERSION",
    "SPECS",
    "AgentTypeSpec",
    "BehaviorsSpec",
    "ChartSpec",
    "DimensionCatalog",
    "DistributionSpec",
    "DomainDefinition",
    "ExperimentSpec",
    "FacilityManifest",
    "Findings",
    "LeverCatalog",
    "MetricCatalog",
    "PlanSpec",
    "QuerySpec",
    "ScenarioSpec",
    "SpecModel",
    "export_schemas",
    "schema_for",
]

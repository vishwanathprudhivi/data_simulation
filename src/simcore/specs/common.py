"""Shared building blocks for spec models."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, WithJsonSchema

from simcore.core.units import to_si

__all__ = [
    "ID_PATTERN",
    "SCHEMA_VERSION",
    "Duration",
    "Identifier",
    "Length",
    "SchemaVersion",
    "SpecModel",
    "Speed",
]

#: Current schema version for every spec. Bump per BRD 7.1 with a migration hook.
SCHEMA_VERSION = "1.0"

ID_PATTERN = r"^[A-Za-z_][A-Za-z0-9_.\-]*$"

Identifier = Annotated[str, Field(pattern=ID_PATTERN, min_length=1, max_length=128)]
SchemaVersion = Literal["1.0"]


class SpecModel(BaseModel):
    """Base for all spec models: unknown fields are errors, not silently ignored."""

    model_config = ConfigDict(extra="forbid", validate_default=True, populate_by_name=True)


def _quantity_schema(dimension: str, example: str) -> dict[str, Any]:
    return {
        "anyOf": [
            {"type": "number"},
            {
                "type": "string",
                "pattern": r"^\s*[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?\s*[A-Za-z/0-9]*\s*$",
            },
        ],
        "description": f"{dimension} quantity: SI number or string with unit, e.g. '{example}'",
    }


def _converter(dimension: str) -> BeforeValidator:
    def convert(value: Any) -> Any:
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            return to_si(value, dimension)
        return value

    return BeforeValidator(convert)


Duration = Annotated[
    float, _converter("time"), Field(ge=0), WithJsonSchema(_quantity_schema("time", "30 min"))
]
Length = Annotated[
    float, _converter("length"), Field(ge=0), WithJsonSchema(_quantity_schema("length", "2.4 m"))
]
Speed = Annotated[
    float, _converter("speed"), Field(gt=0), WithJsonSchema(_quantity_schema("speed", "1.2 m/s"))
]

"""Serialized probability distributions (FR-AGT-04, FR-EXP-08, FR-SCN-02).

All parameters are SI after load. A ``unit`` may be given to declare the unit of
location/scale parameters; it is converted at load and then cleared.
"""

from __future__ import annotations

import math
from typing import Annotated, Literal

from pydantic import Field, model_validator

from simcore.core.units import DIMENSIONS, UnitError
from simcore.specs.common import SpecModel

__all__ = [
    "ConstantDist",
    "DistributionSpec",
    "EmpiricalDist",
    "ExponentialDist",
    "GammaDist",
    "LognormalDist",
    "NormalDist",
    "TriangularDist",
    "UniformDist",
    "WeibullDist",
    "unit_factor",
]


def unit_factor(unit: str) -> float:
    """SI factor for ``unit`` searched across all dimensions."""
    for factors in DIMENSIONS.values():
        if unit in factors:
            return factors[unit]
    raise UnitError(f"unknown unit {unit!r}")


class _Dist(SpecModel):
    unit: str | None = Field(
        default=None,
        description="Unit of location/scale parameters; converted to SI at load, then cleared.",
    )

    def _scaled(self, factor: float) -> dict[str, object]:  # pragma: no cover - overridden
        raise NotImplementedError

    @model_validator(mode="after")
    def _to_si(self) -> _Dist:
        if self.unit is None:
            return self
        factor = unit_factor(self.unit)
        data = self._scaled(factor)
        data["unit"] = None
        return type(self).model_validate(data)


class ConstantDist(_Dist):
    kind: Literal["constant"] = "constant"
    value: float

    def _scaled(self, factor: float) -> dict[str, object]:
        return {"kind": self.kind, "value": self.value * factor}


class ExponentialDist(_Dist):
    kind: Literal["exponential"] = "exponential"
    mean: float = Field(gt=0)

    def _scaled(self, factor: float) -> dict[str, object]:
        return {"kind": self.kind, "mean": self.mean * factor}


class LognormalDist(_Dist):
    """Lognormal with ``mu``/``sigma`` of the underlying normal (natural log)."""

    kind: Literal["lognormal"] = "lognormal"
    mu: float
    sigma: float = Field(gt=0)

    def _scaled(self, factor: float) -> dict[str, object]:
        return {"kind": self.kind, "mu": self.mu + math.log(factor), "sigma": self.sigma}


class GammaDist(_Dist):
    kind: Literal["gamma"] = "gamma"
    shape: float = Field(gt=0)
    scale: float = Field(gt=0)

    def _scaled(self, factor: float) -> dict[str, object]:
        return {"kind": self.kind, "shape": self.shape, "scale": self.scale * factor}


class WeibullDist(_Dist):
    kind: Literal["weibull"] = "weibull"
    shape: float = Field(gt=0)
    scale: float = Field(gt=0)

    def _scaled(self, factor: float) -> dict[str, object]:
        return {"kind": self.kind, "shape": self.shape, "scale": self.scale * factor}


class NormalDist(_Dist):
    """Normal, optionally truncated below at ``min`` by resampling."""

    kind: Literal["normal"] = "normal"
    mean: float
    sd: float = Field(gt=0)
    min: float | None = None

    def _scaled(self, factor: float) -> dict[str, object]:
        return {
            "kind": self.kind,
            "mean": self.mean * factor,
            "sd": self.sd * factor,
            "min": None if self.min is None else self.min * factor,
        }


class UniformDist(_Dist):
    kind: Literal["uniform"] = "uniform"
    low: float
    high: float

    @model_validator(mode="after")
    def _check(self) -> UniformDist:
        if self.high < self.low:
            raise ValueError(f"uniform: high ({self.high}) must be >= low ({self.low})")
        return self

    def _scaled(self, factor: float) -> dict[str, object]:
        return {"kind": self.kind, "low": self.low * factor, "high": self.high * factor}


class TriangularDist(_Dist):
    kind: Literal["triangular"] = "triangular"
    low: float
    mode: float
    high: float

    @model_validator(mode="after")
    def _check(self) -> TriangularDist:
        if not self.low <= self.mode <= self.high or self.low == self.high:
            raise ValueError("triangular: require low <= mode <= high and low < high")
        return self

    def _scaled(self, factor: float) -> dict[str, object]:
        return {
            "kind": self.kind,
            "low": self.low * factor,
            "mode": self.mode * factor,
            "high": self.high * factor,
        }


class EmpiricalDist(_Dist):
    """Resample from observed values (optionally weighted)."""

    kind: Literal["empirical"] = "empirical"
    values: list[float] = Field(min_length=1)
    weights: list[float] | None = None

    @model_validator(mode="after")
    def _check(self) -> EmpiricalDist:
        if self.weights is not None:
            if len(self.weights) != len(self.values):
                raise ValueError("empirical: weights must have the same length as values")
            if any(w < 0 for w in self.weights) or sum(self.weights) <= 0:
                raise ValueError("empirical: weights must be non-negative and not all zero")
        return self

    def _scaled(self, factor: float) -> dict[str, object]:
        return {
            "kind": self.kind,
            "values": [v * factor for v in self.values],
            "weights": self.weights,
        }


DistributionSpec = Annotated[
    ConstantDist
    | ExponentialDist
    | LognormalDist
    | GammaDist
    | WeibullDist
    | NormalDist
    | UniformDist
    | TriangularDist
    | EmpiricalDist,
    Field(discriminator="kind"),
]

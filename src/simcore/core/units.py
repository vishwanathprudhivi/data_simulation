"""Unit parsing and SI conversion (BRD 7.1).

Internally every quantity is SI (seconds, metres, kilograms, metres/second).
Config fields may carry a unit suffix, e.g. ``"30 min"`` or ``"1.2 km/h"``; these
are converted once, at load time.
"""

from __future__ import annotations

import re
from typing import Final

__all__ = ["DIMENSIONS", "UnitError", "to_si"]


class UnitError(ValueError):
    """Raised when a quantity string cannot be parsed or has the wrong dimension."""


# factor to SI for each accepted unit symbol, grouped by physical dimension.
DIMENSIONS: Final[dict[str, dict[str, float]]] = {
    "time": {
        "s": 1.0,
        "sec": 1.0,
        "second": 1.0,
        "seconds": 1.0,
        "ms": 1e-3,
        "min": 60.0,
        "minute": 60.0,
        "minutes": 60.0,
        "h": 3600.0,
        "hr": 3600.0,
        "hour": 3600.0,
        "hours": 3600.0,
        "d": 86400.0,
        "day": 86400.0,
        "days": 86400.0,
    },
    "length": {
        "m": 1.0,
        "cm": 0.01,
        "mm": 0.001,
        "km": 1000.0,
        "ft": 0.3048,
        "in": 0.0254,
        "yd": 0.9144,
    },
    "speed": {
        "m/s": 1.0,
        "km/h": 1000.0 / 3600.0,
        "mph": 0.44704,
        "ft/s": 0.3048,
        "m/min": 1.0 / 60.0,
    },
    "rate": {
        "1/s": 1.0,
        "/s": 1.0,
        "1/min": 1.0 / 60.0,
        "/min": 1.0 / 60.0,
        "1/h": 1.0 / 3600.0,
        "/h": 1.0 / 3600.0,
    },
    "mass": {"kg": 1.0, "g": 1e-3, "t": 1000.0, "lb": 0.45359237},
}

_QUANTITY = re.compile(r"^\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*([A-Za-z/0-9]*)\s*$")


def to_si(value: float | int | str, dimension: str) -> float:
    """Convert ``value`` to SI for ``dimension``.

    Bare numbers are assumed to already be SI. Strings may carry a unit suffix.

    >>> to_si("30 min", "time")
    1800.0
    >>> to_si(2.5, "speed")
    2.5
    """
    if dimension not in DIMENSIONS:
        raise UnitError(f"unknown dimension {dimension!r}; known: {sorted(DIMENSIONS)}")
    if isinstance(value, bool):
        raise UnitError(f"expected a {dimension} quantity, got boolean {value!r}")
    if isinstance(value, (int, float)):
        return float(value)
    match = _QUANTITY.match(value)
    if match is None:
        raise UnitError(
            f"cannot parse {value!r} as a {dimension} quantity; "
            f"use a number (SI) or '<number> <unit>', e.g. '30 min'"
        )
    number, unit = float(match.group(1)), match.group(2)
    if unit == "":
        return number
    factors = DIMENSIONS[dimension]
    if unit not in factors:
        raise UnitError(
            f"unit {unit!r} is not a {dimension} unit; accepted: {', '.join(sorted(factors))}"
        )
    return number * factors[unit]

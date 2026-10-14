"""Governed metric formulas -> SQL fragments (FR-SEM-01/03).

KPI metrics (``source: kpi``) read one key from ``results``. Event metrics
(``source: event_log``) use a closed grammar, never free SQL:

* ``count(<event.type>)``
* ``sum|mean|max|min(<event.type>.<data field>)`` (``@dwell`` = time - data.entry)
* ``state_duration`` (time between consecutive agent.state records, by state)
* ``state_intervals`` (raw intervals for timelines)
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["EVENT_DIMENSIONS", "FormulaError", "ParsedFormula", "parse_formula"]

_AGG = re.compile(r"^(count|sum|mean|max|min)\(([a-z_]+(?:\.[a-z_]+)*?)(?:\.(@?[a-z_]+))?\)$")
_TYPE = re.compile(r"^[a-z_]+(\.[a-z_]+)+$")

#: dimension id -> SQL expression over the events table
EVENT_DIMENSIONS = {
    "variant": "variant",
    "replication": "replication",
    "agent": "agent",
    "agent_type": "regexp_replace(agent, '-[0-9]+$', '')",
    "location": "location",
    "state": "json_extract_string(data, '$.state')",
}


class FormulaError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedFormula:
    kind: str  # agg | state_duration | state_intervals
    agg: str = ""
    event_type: str = ""
    field: str | None = None

    def value_sql(self) -> str:
        if self.agg == "count":
            return "COUNT(*)"
        expr = (
            "(time - CAST(json_extract(data, '$.entry') AS DOUBLE))"
            if self.field == "@dwell"
            else f"CAST(json_extract(data, '$.{self.field}') AS DOUBLE)"
        )
        fn = {"sum": "SUM", "mean": "AVG", "max": "MAX", "min": "MIN"}[self.agg]
        return f"{fn}({expr})"


def parse_formula(formula: str) -> ParsedFormula:
    f = formula.strip()
    if f in ("state_duration", "state_intervals"):
        return ParsedFormula(kind=f)
    m = _AGG.match(f)
    if m is None:
        raise FormulaError(
            f"unsupported event formula {formula!r}; use count(type), sum|mean|max|min("
            "type.field), state_duration or state_intervals"
        )
    agg, path, field_ = m.group(1), m.group(2), m.group(3)
    if agg == "count":
        event_type = f"{path}.{field_}" if field_ else path
        if not _TYPE.match(event_type):
            raise FormulaError(f"bad event type in {formula!r}")
        return ParsedFormula("agg", "count", event_type)
    if field_ is None:
        raise FormulaError(f"{agg}() needs <event type>.<field> in {formula!r}")
    if not _TYPE.match(path):
        raise FormulaError(f"bad event type in {formula!r}")
    return ParsedFormula("agg", agg, path, field_)

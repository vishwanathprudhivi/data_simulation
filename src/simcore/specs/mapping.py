"""``DataMappingSpec`` / ``mapping.yaml``: source data -> engine inputs (FR-DOM-04/05/06).

Declarative: each source is a CSV with a role (``kind``) and a column map from the
engine's field names to the file's headers, plus time format, time zone and units.
The ``backtest`` section says how historical periods become scenarios (lever values
and arrival rates per period) and which observed KPIs to compare.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from simcore.specs.common import SchemaVersion, SpecModel

__all__ = ["BacktestSpec", "DataMappingSpec", "SourceSpec", "Tolerance"]

SourceKind = Literal["station_events", "arrivals", "downtime", "observed_kpis", "samples"]

#: Required engine fields per source kind (the column map must name them).
REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "station_events": ("station", "start", "end"),
    "arrivals": ("time",),
    "downtime": ("station", "start", "end"),
    "observed_kpis": ("period", "start", "end"),
    "samples": ("value",),
}


class SourceSpec(SpecModel):
    path: str = Field(description="CSV path, relative to the mapping file.")
    kind: SourceKind
    columns: dict[str, str] = Field(
        description="engine field -> CSV header, e.g. {station: 'Station', start: 'Start'}."
    )
    time_format: str | None = Field(default=None, description="strptime format; default ISO 8601.")
    timezone: str | None = Field(default=None, description="IANA zone of naive timestamps.")
    units: dict[str, str] = Field(
        default_factory=dict, description="Units of numeric duration columns, e.g. {setup: s}."
    )
    group: str | None = Field(
        default=None, description="samples: what the values describe (e.g. a station id)."
    )

    @model_validator(mode="after")
    def _check(self) -> SourceSpec:
        missing = [f for f in REQUIRED_FIELDS[self.kind] if f not in self.columns]
        if missing:
            raise ValueError(f"{self.kind} sources need columns for {missing}")
        return self


class Tolerance(SpecModel):
    """Backtest gate: mean absolute relative error and coverage thresholds."""

    pass_error: float = Field(default=0.10, gt=0)
    warn_error: float = Field(default=0.20, gt=0)
    pass_coverage: float = Field(default=0.8, ge=0, le=1)
    warn_coverage: float = Field(default=0.5, ge=0, le=1)


class BacktestSpec(SpecModel):
    base_scenario: str = Field(description="Scenario each period starts from (relative path).")
    periods: str = Field(description="Id of the observed_kpis source with one row per period.")
    levers: dict[str, str] = Field(
        default_factory=dict, description="lever id -> column of the periods source."
    )
    arrival_paths: dict[str, str] = Field(
        default_factory=dict,
        description="work-item type -> scenario path of its mean interarrival time.",
    )
    kpis: dict[str, str] = Field(description="metric id -> observed column of the periods source.")
    replications: int = Field(default=5, ge=2)
    tolerance: Tolerance = Field(default_factory=Tolerance)


class DataMappingSpec(SpecModel):
    schema_version: SchemaVersion = "1.0"
    domain: str
    timezone: str = Field(default="UTC", description="Default zone of naive timestamps.")
    sources: dict[str, SourceSpec]
    backtest: BacktestSpec | None = None

    @model_validator(mode="after")
    def _check(self) -> DataMappingSpec:
        if self.backtest is not None:
            src = self.sources.get(self.backtest.periods)
            if src is None or src.kind != "observed_kpis":
                raise ValueError(
                    f"backtest.periods must name an observed_kpis source; got "
                    f"{self.backtest.periods!r}"
                )
            cols = set(src.columns)
            for lever, col in self.backtest.levers.items():
                if col not in cols:
                    raise ValueError(f"backtest lever {lever!r}: no column {col!r} in {src.path}")
            for kpi, col in self.backtest.kpis.items():
                if col not in cols:
                    raise ValueError(f"backtest kpi {kpi!r}: no column {col!r} in {src.path}")
        return self

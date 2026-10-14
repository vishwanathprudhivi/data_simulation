"""Load mapped source data and report its quality (FR-DOM-04).

Files are untrusted data: they are parsed as CSV values only, never evaluated.
The data-quality report covers missing columns and values, unparseable or
ambiguous timestamps, time-zone handling, negative or zero durations, duplicates,
outliers and unit plausibility against the domain's own defaults.
"""

from __future__ import annotations

import csv
import math
import statistics
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from simcore.core.units import UnitError, to_si
from simcore.specs.domain import DomainDefinition
from simcore.specs.mapping import DataMappingSpec, SourceSpec

__all__ = ["DQIssue", "DQReport", "MappedData", "Period", "load_mapping", "map_data"]

MAX_ROWS = 2_000_000
MAX_BYTES = 200 * 1024 * 1024


@dataclass(frozen=True)
class DQIssue:
    severity: str  # "error" | "warn" | "info"
    source: str
    check: str
    message: str
    rows: tuple[int, ...] = ()
    hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "source": self.source,
            "check": self.check,
            "message": self.message,
            "rows": list(self.rows[:20]),
            "n_rows": len(self.rows),
            "hint": self.hint,
        }


@dataclass
class DQReport:
    issues: list[DQIssue] = field(default_factory=list)
    stats: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not any(i.severity == "error" for i in self.issues)

    def add(self, *args: Any, **kw: Any) -> None:
        self.issues.append(DQIssue(*args, **kw))

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "issues": [i.to_dict() for i in self.issues], "stats": self.stats}

    def markdown(self) -> str:
        lines = ["## Data quality", ""]
        for name, st in sorted(self.stats.items()):
            lines.append(f"- **{name}**: {st.get('rows', 0)} rows, {st.get('used', 0)} used")
        lines.append("")
        if not self.issues:
            lines.append("No data-quality issues found.")
        for i in self.issues:
            rows = f" (rows {', '.join(map(str, i.rows[:8]))}{'…' if len(i.rows) > 8 else ''})"
            lines.append(
                f"- [{i.severity.upper()}] {i.source} / {i.check}: {i.message}"
                + (rows if i.rows else "")
                + (f" — fix: {i.hint}" if i.hint else "")
            )
        return "\n".join(lines) + "\n"


@dataclass
class Period:
    id: str
    start: float  # epoch seconds (UTC)
    end: float
    values: dict[str, float]

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class MappedData:
    """Engine-ready inputs extracted from the sources (all times in SI seconds)."""

    service_times: dict[str, list[float]] = field(default_factory=dict)  # station -> samples
    servers: dict[str, set[str]] = field(default_factory=dict)  # station -> server ids seen
    batch: dict[str, list[float]] = field(default_factory=dict)  # station -> items per service
    busy: dict[str, list[tuple[float, float]]] = field(default_factory=dict)  # station -> spans
    arrivals: dict[str, list[float]] = field(default_factory=dict)  # work-item type -> times
    repairs: dict[str, list[float]] = field(default_factory=dict)  # station -> MTTR samples
    uptimes: dict[str, list[float]] = field(default_factory=dict)  # station -> MTBF samples
    samples: dict[str, list[float]] = field(default_factory=dict)  # group -> values
    periods: list[Period] = field(default_factory=list)


def load_mapping(path: Path) -> DataMappingSpec:
    import yaml

    from simcore.core.config import validate_spec

    return validate_spec(DataMappingSpec, yaml.safe_load(path.read_text()) or {}, str(path))


def _read_csv(path: Path, report: DQReport, name: str) -> tuple[list[str], list[dict[str, str]]]:
    if not path.exists():
        report.add("error", name, "file", f"{path} does not exist", hint="check 'path'")
        return [], []
    if path.stat().st_size > MAX_BYTES:
        report.add("error", name, "file", f"{path.name} is larger than {MAX_BYTES >> 20} MB")
        return [], []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        rows = []
        for i, row in enumerate(reader):
            if i >= MAX_ROWS:
                report.add("warn", name, "file", f"only the first {MAX_ROWS} rows were read")
                break
            rows.append(row)
        return list(reader.fieldnames or []), rows


def _parse_time(text: str, src: SourceSpec, default_tz: str) -> float:
    text = text.strip()
    if not text:
        raise ValueError("empty")
    dt = (
        datetime.strptime(text, src.time_format)
        if src.time_format
        else datetime.fromisoformat(text.replace("Z", "+00:00"))
    )
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(src.timezone or default_tz))
    return dt.astimezone(timezone.utc).timestamp()


def _number(text: str, unit: str | None) -> float:
    v = float(text.strip())
    return to_si(f"{v} {unit}", "time") if unit else v


def _outliers(values: Sequence[float], k: float = 3.0) -> list[int]:
    if len(values) < 8:
        return []
    q = statistics.quantiles(values, n=4)
    iqr = q[2] - q[0]
    lo, hi = q[0] - k * iqr, q[2] + k * iqr
    return [i for i, v in enumerate(values) if v < lo or v > hi]


def _default_means(defn: DomainDefinition | None) -> dict[str, float]:
    """Mean of each station's default service time (for unit plausibility checks)."""
    out: dict[str, float] = {}
    if defn is None or defn.process is None:
        return out
    from simcore.core.rng import RNGManager
    from simcore.core.sampling import make_sampler

    rng = RNGManager(0).stream("dq")
    for st in defn.process.stations:
        if st.service_time is None:
            continue
        sampler = make_sampler(st.service_time)
        out[st.id] = statistics.fmean(sampler(rng) for _ in range(400))
    return out


def map_data(
    spec: DataMappingSpec, base_dir: Path, definition: DomainDefinition | None = None
) -> tuple[MappedData, DQReport]:
    """Read every source, convert to engine inputs, and collect data-quality issues."""
    report = DQReport()
    data = MappedData()
    try:
        ZoneInfo(spec.timezone)
    except ZoneInfoNotFoundError:
        report.add("error", "mapping", "timezone", f"unknown time zone {spec.timezone!r}")
        return data, report
    stations = (
        {s.id for s in definition.process.stations}
        if definition is not None and definition.process is not None
        else None
    )
    for name, src in sorted(spec.sources.items()):
        header, rows = _read_csv(base_dir / src.path, report, name)
        st = report.stats.setdefault(name, {"rows": len(rows), "used": 0, "kind": src.kind})
        if not rows:
            if header or (base_dir / src.path).exists():
                report.add("error", name, "file", "no data rows")
            continue
        missing_cols = sorted(c for c in src.columns.values() if c not in header)
        if missing_cols:
            report.add(
                "error",
                name,
                "columns",
                f"columns {missing_cols} not in the file",
                hint=f"available columns: {header}",
            )
            continue
        if src.timezone:
            try:
                ZoneInfo(src.timezone)
            except ZoneInfoNotFoundError:
                report.add("error", name, "timezone", f"unknown time zone {src.timezone!r}")
                continue
        empty: dict[str, list[int]] = defaultdict(list)
        bad_time: list[int] = []
        bad_num: list[int] = []
        negative: list[int] = []
        unknown: dict[str, list[int]] = defaultdict(list)
        seen: dict[tuple[str, ...], int] = {}
        dupes: list[int] = []
        parsed: list[tuple[int, dict[str, Any]]] = []
        time_fields = [f for f in ("start", "end", "time") if f in src.columns]
        for i, row in enumerate(rows, start=2):  # row 1 is the header
            rec: dict[str, Any] = {}
            ok = True
            key = tuple(row.values())  # whole-row duplicates (exports repeated)
            if key in seen:
                dupes.append(i)
                continue
            seen[key] = i
            for fld, col in src.columns.items():
                raw = (row.get(col) or "").strip()
                if not raw:
                    empty[col].append(i)
                    if fld in time_fields or fld in ("station", "period", "value"):
                        ok = False
                    continue
                if fld in time_fields:
                    try:
                        rec[fld] = _parse_time(raw, src, spec.timezone)
                    except (ValueError, ZoneInfoNotFoundError):
                        if not bad_time or bad_time[-1] != i:
                            bad_time.append(i)
                        ok = False
                elif fld in ("station", "period", "server", "sku", "item"):
                    rec[fld] = raw
                else:
                    try:
                        rec[fld] = _number(raw, src.units.get(fld))
                    except (ValueError, UnitError):
                        bad_num.append(i)
                        ok = False
            if not ok:
                continue
            if "start" in rec and "end" in rec and rec["end"] <= rec["start"]:
                negative.append(i)
                continue
            if stations is not None and "station" in rec and rec["station"] not in stations:
                unknown[rec["station"]].append(i)
                continue
            parsed.append((i, rec))
        for col, idx in sorted(empty.items()):
            report.add(
                "warn",
                name,
                "missing_values",
                f"{len(idx)} empty values in {col!r}",
                tuple(idx),
                "rows with an empty required field are skipped",
            )
        if bad_time:
            fmt = src.time_format or "ISO 8601"
            report.add(
                "error" if len(bad_time) > len(rows) / 2 else "warn",
                name,
                "timestamps",
                f"{len(bad_time)} timestamps do not match {fmt}",
                tuple(bad_time),
                "set time_format in mapping.yaml",
            )
        if bad_num:
            report.add(
                "warn", name, "numbers", f"{len(bad_num)} non-numeric values", tuple(bad_num)
            )
        if negative:
            report.add(
                "warn",
                name,
                "durations",
                f"{len(negative)} rows end before they start",
                tuple(negative),
                "check the column mapping of start/end and time zones",
            )
        for station, idx in sorted(unknown.items()):
            report.add(
                "error",
                name,
                "unknown_station",
                f"station {station!r} is not in the domain definition",
                tuple(idx),
                f"known stations: {sorted(stations or [])}",
            )
        if dupes:
            report.add(
                "info", name, "duplicates", f"{len(dupes)} duplicate rows ignored", tuple(dupes)
            )
        if not src.timezone and any(_naive(r, src) for r in rows[:50]):
            report.add("info", name, "timezone", f"naive timestamps interpreted in {spec.timezone}")
        st["used"] = len(parsed)
        if not parsed:
            report.add(
                "error",
                name,
                "no_usable_rows",
                f"none of the {len(rows)} rows could be used",
                hint="check the column map and time_format against the file",
            )
        _collect(src, name, parsed, data, report)
    if definition is not None:
        _plausibility(data, definition, report)
    return data, report


def _naive(row: Mapping[str, str], src: SourceSpec) -> bool:
    for f in ("start", "time"):
        col = src.columns.get(f)
        if col and row.get(col):
            v = row[col].strip()
            return not (v.endswith("Z") or "+" in v[10:] or v[10:].count("-") > 0)
    return False


def _collect(
    src: SourceSpec,
    name: str,
    parsed: list[tuple[int, dict[str, Any]]],
    data: MappedData,
    report: DQReport,
) -> None:
    if src.kind == "station_events":
        by: dict[str, list[tuple[int, float]]] = defaultdict(list)
        for i, r in parsed:
            d = r["end"] - r["start"] - float(r.get("setup", 0.0) or 0.0)
            if d > 0:
                by[r["station"]].append((i, d))
            data.servers.setdefault(r["station"], set()).add(str(r.get("server", "0")))
            if "items" in r:
                data.batch.setdefault(r["station"], []).append(float(r["items"]))
            data.busy.setdefault(r["station"], []).append((r["start"], r["end"]))
        for station, vals in sorted(by.items()):
            values = [v for _, v in vals]
            out = _outliers(values)
            if out:
                report.add(
                    "warn",
                    name,
                    "outliers",
                    f"{len(out)} service times at {station} are > 3 IQR from the quartiles",
                    tuple(vals[j][0] for j in out),
                    "outliers are kept; check for unlogged stoppages",
                )
            data.service_times[station] = values
    elif src.kind == "arrivals":
        for _, r in parsed:
            data.arrivals.setdefault(str(r.get("sku", "*")), []).append(r["time"])
        for k in data.arrivals:
            data.arrivals[k].sort()
    elif src.kind == "downtime":
        spans: dict[str, list[tuple[float, float]]] = defaultdict(list)
        for _, r in parsed:
            spans[r["station"]].append((r["start"], r["end"]))
        for station, ss in spans.items():
            ss.sort()
            data.repairs[station] = [b - a for a, b in ss]
            data.uptimes[station] = [
                ss[j + 1][0] - ss[j][1] for j in range(len(ss) - 1) if ss[j + 1][0] > ss[j][1]
            ]
    elif src.kind == "observed_kpis":
        for _, r in parsed:
            kpi_vals = {
                k: float(v)
                for k, v in r.items()
                if k not in ("period", "start", "end") and isinstance(v, (int, float))
            }
            data.periods.append(Period(str(r["period"]), r["start"], r["end"], kpi_vals))
        data.periods.sort(key=lambda p: p.start)
    elif src.kind == "samples":
        data.samples.setdefault(src.group or name, []).extend(r["value"] for _, r in parsed)


def _plausibility(data: MappedData, defn: DomainDefinition, report: DQReport) -> None:
    """Flag service times whose scale is far from the domain's defaults (unit mistakes)."""
    means = _default_means(defn)
    for station, values in sorted(data.service_times.items()):
        if station not in means or not values or means[station] <= 0:
            continue
        ratio = statistics.median(values) / means[station]
        if ratio > 20 or ratio < 0.05:
            factor = 60 if 30 < ratio < 120 or 1 / 120 < ratio < 1 / 30 else None
            hint = (
                "this looks like a minutes/seconds mix-up; check 'units' and the time columns"
                if factor
                else "check units and the start/end mapping"
            )
            report.add(
                "warn",
                "station_events",
                "units",
                f"median service time at {station} is {statistics.median(values):.4g} s, "
                f"{ratio:.3g}x the domain default mean of {means[station]:.4g} s",
                hint=hint,
            )
    if data.periods:
        for p in data.periods:
            if p.duration <= 0 or math.isnan(p.duration):
                report.add("error", "periods", "durations", f"period {p.id} has no length")

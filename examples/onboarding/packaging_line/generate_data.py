"""Generate the synthetic packaging-line sample data (BRD 9.1; CLAUDE.md: synthetic only).

The "historical" data is produced by the reference model itself
(``domains/packaging_line/domain.yaml``) for four 8-hour shifts with different
staffing and demand, then exported the way a line MES might: local timestamps
(Europe/Berlin, day.month.year), spaces in headers, one duplicate row and one row
with a missing end time. Re-run with ``python generate_data.py``; output is
deterministic.
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from simcore.api import run_scenario
from simcore.core.config import load_scenario
from simcore.core.obs.eventlog import EventType

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
TZ = ZoneInfo("Europe/Berlin")
FMT = "%d.%m.%Y %H:%M:%S"
# (operators, mean seconds between trays) per shift
SHIFTS = [(2, 15.0), (3, 13.0), (4, 14.0), (5, 12.5)]


def local(t0: datetime, seconds: float) -> datetime:
    return (t0 + timedelta(seconds=seconds)).astimezone(TZ)


def stamp(t0: datetime, seconds: float) -> str:
    """MES-style local time with milliseconds."""
    dt = local(t0, seconds)
    return f"{dt.strftime(FMT)}.{dt.microsecond // 1000:03d}"


def main() -> None:
    base = load_scenario(ROOT / "examples/scenarios/packaging_line/baseline.yaml")
    out = HERE / "data"
    out.mkdir(exist_ok=True)
    station_rows, releases, downtime, shifts = [], [], [], []
    for i, (ops, gap) in enumerate(SHIFTS):
        start = datetime(2026, 3, 2 + i, 6, 0, tzinfo=TZ).astimezone(timezone.utc)
        result = run_scenario(
            base,
            seed=100 + i,
            overrides={
                "fleet.operator.count": ops,
                "demand.demand.interarrival.mean": gap,
                "demand.demand_b.interarrival.mean": gap,
                "warmup": 0,
            },
        )
        log = result.events
        open_service: dict[tuple[str, int], tuple[float, float, int]] = {}
        for r in log.of_type(EventType.SERVICE_START, EventType.SERVICE_END):
            key = (str(r.location), int(r.data.get("server", 0)))
            if r.type == EventType.SERVICE_START:
                open_service[key] = (r.time, float(r.data.get("changeover", 0.0)),
                                     len(r.data.get("items", [])))
            elif key in open_service:
                t0, setup, n = open_service.pop(key)
                station_rows.append([key[0], f"{key[0].upper()}-{key[1] + 1}",
                                     stamp(start, t0), stamp(start, r.time), f"{setup:.0f}", n])
        for r in log.of_type(EventType.TASK_RELEASED):
            releases.append([local(start, r.time).isoformat(timespec="milliseconds"),
                             "A" if str(r.data.get("task_type", "")).endswith("a") else "B"])
        down_open: dict[str, float] = {}
        for r in log.of_type(EventType.DISRUPTION_START, EventType.DISRUPTION_END):
            if r.data.get("type") != "breakdown":
                continue
            if r.type == EventType.DISRUPTION_START:
                down_open[str(r.location)] = r.time
            elif str(r.location) in down_open:
                t0 = down_open.pop(str(r.location))
                downtime.append([r.location, stamp(start, t0), stamp(start, r.time)])
        end = local(start, base.scenario.duration)
        shifts.append([f"S{i + 1:02d}", stamp(start, 0), f"{end.strftime(FMT)}.000", ops,
                       int(result.kpis["tasks_completed"])])
    # MES quirks the data-quality report should find
    station_rows.insert(10, list(station_rows[9]))
    station_rows[25][3] = ""
    with (out / "station_log.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Station", "Line Server", "Start Time", "End Time", "Setup (s)", "Trays"])
        w.writerows(station_rows)
    with (out / "tray_releases.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Released At", "SKU"])
        w.writerows(releases)
    with (out / "downtime.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Station", "Down From", "Down Until"])
        w.writerows(downtime)
    with (out / "shifts.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Shift", "Start", "End", "Operators", "Trays packed"])
        w.writerows(shifts)
    print(f"wrote {len(station_rows)} station events, {len(releases)} releases, "
          f"{len(downtime)} breakdowns, {len(shifts)} shifts to {out}")


if __name__ == "__main__":
    main()

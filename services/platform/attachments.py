"""Data attachments (FR-SCG-10): uploaded CSVs mapped to scenario inputs.

Uploads are untrusted data: size- and type-limited, parsed as CSV only, and never
interpreted as instructions. A mapping is *proposed* from the headers (for the user
or the assistant to confirm) and *validated* through the onboarding data-mapping
layer before any value reaches a scenario.
"""

from __future__ import annotations

import csv
import io
import itertools
import re
import statistics
import tempfile
from pathlib import Path
from typing import Any

from simcore.experiment.fitting import fit_distributions
from simcore.onboarding.mapping import map_data
from simcore.specs.mapping import DataMappingSpec, SourceSpec

__all__ = ["MAX_ATTACHMENT_BYTES", "apply_attachment", "inspect_csv", "propose_mapping"]

MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
_TIME = re.compile(r"(time|date|released|arriv|start|created|timestamp)", re.I)
_TYPE = re.compile(r"(sku|type|product|class|category|item)", re.I)
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


def inspect_csv(content: str) -> dict[str, Any]:
    if len(content.encode()) > MAX_ATTACHMENT_BYTES:
        raise ValueError(f"attachment is larger than {MAX_ATTACHMENT_BYTES >> 20} MB")
    reader = csv.DictReader(io.StringIO(content))
    header = list(reader.fieldnames or [])
    if not header or len(header) > 200:
        raise ValueError("expected a CSV file with a header row (at most 200 columns)")
    rows = list(reader)
    if not rows:
        raise ValueError("the CSV has no data rows")
    return {"columns": header, "rows": len(rows), "preview": rows[:5]}


def propose_mapping(info: dict[str, Any]) -> dict[str, Any]:
    """Heuristic proposal from headers and sample values; to be confirmed, never auto-applied."""
    cols, preview = info["columns"], info["preview"]

    def values(c: str) -> list[str]:
        return [str(r.get(c, "")) for r in preview]

    times = [c for c in cols if _TIME.search(c) and any(_ISO.match(v) for v in values(c))]
    types = [c for c in cols if _TYPE.search(c)]
    numeric = []
    for c in cols:
        try:
            [float(v) for v in values(c) if v]
            numeric.append(c)
        except ValueError:
            pass
    if times:
        columns = {"time": times[0], **({"sku": types[0]} if types else {})}
        return {"kind": "arrivals", "columns": columns,
                "why": f"'{times[0]}' holds timestamps" + (f", '{types[0]}' the work-item type"
                                                            if types else "")}  # fmt: skip
    if numeric:
        return {"kind": "samples", "columns": {"value": numeric[0]},
                "why": f"'{numeric[0]}' is the first numeric column"}  # fmt: skip
    return {"kind": None, "columns": {}, "why": "no timestamp or numeric column recognized"}


def apply_attachment(
    content: str,
    source: SourceSpec,
    *,
    domain: str,
    arrival_paths: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Validate the mapping on the data; return DQ report plus overrides / fitted values."""
    with tempfile.TemporaryDirectory(prefix="simcore-att-") as tmp:
        path = Path(tmp) / "attachment.csv"
        path.write_text(content)
        src = source.model_copy(update={"path": "attachment.csv"})
        spec = DataMappingSpec(domain=domain, sources={"attachment": src})
        data, dq = map_data(spec, Path(tmp))
    out: dict[str, Any] = {"data_quality": dq.to_dict(), "overrides": {}, "fitted": None}
    if not dq.ok:
        return out
    if source.kind == "arrivals":
        rates = {}
        for sku, times in sorted(data.arrivals.items()):
            gaps = [b - a for a, b in itertools.pairwise(times) if b > a]
            if len(gaps) >= 2:
                rates[sku] = statistics.fmean(gaps)
        out["interarrival_means"] = rates
        for sku, path_ in (arrival_paths or {}).items():
            if sku in rates:
                out["overrides"][path_] = rates[sku]
            elif sku == "*" and rates:
                all_times = sorted(t for ts in data.arrivals.values() for t in ts)
                gaps = [b - a for a, b in itertools.pairwise(all_times) if b > a]
                out["overrides"][path_] = statistics.fmean(gaps)
    elif source.kind == "samples":
        values = next(iter(data.samples.values()), [])
        rep = fit_distributions(values)
        out["fitted"] = {"best": rep.best.to_dict(), "summary": rep.summary,
                         "spec": dict(rep.best.spec)}  # fmt: skip
    return out

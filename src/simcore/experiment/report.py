"""Experiment reports in Markdown and HTML (FR-IFC-06, BRD Q3).

Both formats are rendered from the same ``summary.json`` structure, so a report
can be regenerated from disk (``simcore report <experiment dir>``) without re-running.
"""

from __future__ import annotations

import html
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

__all__ = ["render_html", "render_markdown", "warmup_svg", "write_reports"]


def _fmt(x: Any) -> str:
    if x is None:
        return "n/a"
    if isinstance(x, float):
        return f"{x:.4g}"
    return str(x)


def _ci(entry: dict[str, Any]) -> str:
    low, high = entry["ci"]
    return f"[{_fmt(low)}, {_fmt(high)}]"


def _tables(summary: dict[str, Any]) -> list[tuple[str, list[str], list[list[str]]]]:
    """(title, header, rows) for every table in the report."""
    tables: list[tuple[str, list[str], list[list[str]]]] = []
    conf = round(summary["confidence"] * 100)
    for metric in summary["metrics"]:
        rows = [
            [
                s["variant"],
                _fmt(s["mean"]),
                _fmt(s["half_width"]),
                _ci(s),
                str(s["n"]),
                "; ".join(s["warnings"]),
            ]
            for s in summary["summaries"]
            if s["metric"] == metric
        ]
        tables.append(
            (f"KPI `{metric}`", ["variant", "mean", f"± ({conf}% CI)", "CI", "n", "notes"], rows)
        )
    comps = summary["comparisons"]
    if comps:
        rows = []
        for c in comps:
            d = c["diff"]
            unpaired = c.get("unpaired_half_width")
            ratio = ""
            if c["paired"] and unpaired and d["half_width"]:
                ratio = f"{unpaired / d['half_width']:.2f}x narrower"
            rows.append(
                [
                    c["variant"],
                    c["metric"],
                    _fmt(d["mean"]),
                    _ci(d),
                    "paired (CRN)" if c["paired"] else "independent (Welch)",
                    _fmt(unpaired) if c["paired"] else "",
                    ratio,
                    "yes" if c["ci_excludes_zero"] else "no (CI includes 0)",
                ]
            )
        tables.append(
            (
                f"Comparisons vs `{summary['reference']}` (variant - reference)",
                [
                    "variant",
                    "metric",
                    "mean diff",
                    f"{conf}% CI",
                    "method",
                    "unpaired ± (same data)",
                    "CRN effect",
                    "CI excludes 0",
                ],
                rows,
            )
        )
    return tables


def _intro(summary: dict[str, Any]) -> list[tuple[str, str]]:
    prov = summary["provenance"]
    reps = summary["replications_ok"]
    return [
        ("Experiment", summary["experiment_id"]),
        ("Domain", str(prov["domain"])),
        ("Base scenario", str(prov["base_scenario"])),
        ("Fidelity mode", summary["mode"]),
        ("Common random numbers", "yes" if summary["crn"] else "no"),
        ("Confidence level", f"{summary['confidence']:.0%}"),
        (
            "Replications (ok / planned)",
            ", ".join(f"{k}: {v}/{summary['replications_planned']}" for k, v in reps.items()),
        ),
        ("Engine", f"{prov['engine_version']} (git {prov['git_sha'] or 'unknown'})"),
    ]


def render_markdown(summary: dict[str, Any]) -> str:
    out = [f"# Experiment report: {summary['experiment_id']}", ""]
    out += [f"- **{k}:** {v}" for k, v in _intro(summary)]
    out += ["", "## Alternatives", "", "| id | overrides |", "|---|---|"]
    for v in summary["variants"]:
        out.append(f"| {v['id']} | `{json.dumps(v['overrides'], sort_keys=True)}` |")
    for title, header, rows in _tables(summary):
        out += ["", f"## {title}", "", "| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
        out += ["| " + " | ".join(r) + " |" for r in rows]
    warmup = summary.get("warmup")
    if warmup:
        out += [
            "",
            "## Warm-up diagnostic (Welch)",
            "",
            f"Signal `{warmup['signal']}`, bucket {warmup['bucket']} s, window "
            f"{warmup['window']}, configured warm-up {warmup['configured']} s, suggested "
            f"{_fmt(warmup['suggested'])} s. See report.html for the plot.",
        ]
    if summary["warnings"]:
        out += ["", "## Warnings", ""] + [f"- {w}" for w in summary["warnings"]]
    if summary["failures"]:
        out += ["", "## Failed replications", ""]
        out += [
            f"- {f['variant']} rep {f['replication']} ({f['attempts']} attempt(s)): {f['error']}"
            for f in summary["failures"]
        ]
    out += [
        "",
        "## Method",
        "",
        "Each KPI is averaged per replication over the post-warm-up window; intervals are "
        "Student-t on replication means. With CRN, alternatives share seeds per "
        "replication and differences use the paired-t interval; the 'unpaired' column "
        "shows the Welch interval on the same data for comparison. A difference is only "
        "called significant when its interval excludes zero.",
        "",
    ]
    return "\n".join(out)


def warmup_svg(
    series: Sequence[float | None],
    bucket: float,
    warmup: float,
    suggested: float | None,
    width: int = 640,
    height: int = 220,
) -> str:
    """Tiny dependency-free line chart for the Welch moving average."""
    pts = [(i * bucket, v) for i, v in enumerate(series) if v is not None]
    if not pts:
        return "<p>no data</p>"
    xmax = max(p[0] for p in pts) or 1.0
    ymax = max(p[1] for p in pts) or 1.0
    pad = 30

    def sx(x: float) -> float:
        return pad + (width - 2 * pad) * x / xmax

    def sy(y: float) -> float:
        return height - pad - (height - 2 * pad) * y / ymax

    path = " ".join(
        f"{'M' if i == 0 else 'L'}{sx(x):.1f},{sy(y):.1f}" for i, (x, y) in enumerate(pts)
    )
    marks = [
        f'<line x1="{sx(warmup):.1f}" x2="{sx(warmup):.1f}" y1="{pad}" '
        f'y2="{height - pad}" stroke="#888" stroke-dasharray="4"/>'
    ]
    if suggested is not None:
        marks.append(
            f'<line x1="{sx(suggested):.1f}" x2="{sx(suggested):.1f}" y1="{pad}" '
            f'y2="{height - pad}" stroke="#c33"/>'
        )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'role="img" aria-label="Welch moving average">'
        f'<line x1="{pad}" y1="{height - pad}" x2="{width - pad}" y2="{height - pad}" '
        f'stroke="#333"/><line x1="{pad}" y1="{pad}" x2="{pad}" y2="{height - pad}" '
        f'stroke="#333"/><path d="{path}" fill="none" stroke="#2a6fdb" stroke-width="1.5"/>'
        + "".join(marks)
        + f'<text x="{pad}" y="{pad - 8}" font-size="11">max {ymax:.4g}</text>'
        f'<text x="{width - pad}" y="{height - 8}" font-size="11" text-anchor="end">'
        f"sim-time {xmax:.0f} s</text></svg>"
    )


def render_html(summary: dict[str, Any]) -> str:
    e = html.escape
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        f"<title>Experiment report: {e(summary['experiment_id'])}</title>",
        "<style>body{font-family:system-ui,sans-serif;max-width:1100px;margin:2em auto;"
        "padding:0 1em}table{border-collapse:collapse;margin:1em 0}td,th{border:1px solid "
        "#ccc;padding:4px 8px;text-align:left}th{background:#f4f4f4}.warn{color:#a40}</style>",
        "</head><body>",
        f"<h1>Experiment report: {e(summary['experiment_id'])}</h1><ul>",
    ]
    parts += [f"<li><b>{e(k)}:</b> {e(v)}</li>" for k, v in _intro(summary)]
    parts.append("</ul><h2>Alternatives</h2><table><tr><th>id</th><th>overrides</th></tr>")
    for v in summary["variants"]:
        parts.append(
            f"<tr><td>{e(v['id'])}</td><td><code>"
            f"{e(json.dumps(v['overrides'], sort_keys=True))}</code></td></tr>"
        )
    parts.append("</table>")
    for title, header, rows in _tables(summary):
        parts.append(f"<h2>{e(title).replace('`', '')}</h2><table><tr>")
        parts += [f"<th>{e(h)}</th>" for h in header]
        parts.append("</tr>")
        for r in rows:
            parts.append("<tr>" + "".join(f"<td>{e(c)}</td>" for c in r) + "</tr>")
        parts.append("</table>")
    warmup = summary.get("warmup")
    if warmup:
        parts.append("<h2>Warm-up diagnostic (Welch)</h2>")
        parts.append(
            f"<p>Signal <code>{e(warmup['signal'])}</code>; dashed line: configured "
            f"warm-up ({e(_fmt(warmup['configured']))} s); red line: suggested "
            f"({e(_fmt(warmup['suggested']))} s).</p>"
        )
        parts.append(
            warmup_svg(
                warmup["moving_average"],
                warmup["bucket"],
                warmup["configured"],
                warmup["suggested"],
            )
        )
    if summary["warnings"]:
        parts.append("<h2>Warnings</h2><ul>")
        parts += [f"<li class='warn'>{e(w)}</li>" for w in summary["warnings"]]
        parts.append("</ul>")
    if summary["failures"]:
        parts.append("<h2>Failed replications</h2><ul>")
        parts += [
            f"<li>{e(f['variant'])} rep {f['replication']}: {e(str(f['error']))}</li>"
            for f in summary["failures"]
        ]
        parts.append("</ul>")
    parts.append("</body></html>")
    return "\n".join(parts)


def write_reports(summary: dict[str, Any], directory: Path) -> dict[str, Path]:
    md, page = directory / "report.md", directory / "report.html"
    md.write_text(render_markdown(summary) + "\n", encoding="utf-8")
    page.write_text(render_html(summary) + "\n", encoding="utf-8")
    return {"report_md": md, "report_html": page}

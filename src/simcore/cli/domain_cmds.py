"""``simcore domain ...``: the onboarding workflow (FR-DOM-07/08, AC-DOM-01/02).

A domain workspace is a directory with ``domain.yaml``, ``mapping.yaml`` and the
data it names. ``init`` scaffolds one; ``validate`` checks definitions and data;
``backtest`` fits inputs, replays history and writes the Validation Report;
``publish`` copies an approved definition onto the domain search path. Nothing is
ever published automatically.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

import click
import yaml

__all__ = ["domain"]

TEMPLATE_T0 = """\
# Tier-0 domain (config only): edit from your SOP / process walk. See docs/guides/onboarding.md.
schema_version: "1.0"
id: {id}
name: {name}
version: 0.1.0
tier: 0
description: >-
  TODO - one paragraph on what flows through the process and what limits it.
resource_types:
  - {{id: operator, description: "TODO: who staffs the stations"}}
work_item_types:
  - {{id: unit, description: "TODO: the thing that flows (tray, order, patient, ...)"}}
process:
  stations:
    - id: step_1
      description: "TODO: first step"
      service_time: {{kind: triangular, low: 8, mode: 10, high: 15}}  # seconds; refit from data
    - id: step_2
      resource_type: operator
      service_time: {{kind: triangular, low: 10, mode: 12, high: 20}}
      buffer_capacity: 10
  routings:
    - {{work_item_type: unit, steps: [step_1, step_2]}}
metrics:
  - {{id: output, display_name: Units completed, unit: "1", formula: tasks_completed,
     direction: higher_is_better, allowed_dimensions: [variant, replication],
     format: {{kind: integer, decimals: 0}}}}
levers:
  - {{id: operators, display_name: Operators, path: fleet.operator.count, type: int,
     min: 1, max: 20, default: 2}}
"""

TEMPLATE_PLUGIN = '''"""Tier-{tier} hooks for {id} (FR-DOM-01).

Register the class through the ``simcore.domains`` entry point.
"""

from __future__ import annotations

from simcore.core.plugins import DomainPlugin


class {cls}(DomainPlugin):
    name = "{id}"
    version = "0.1.0"
    description = "TODO"
    # Tier 1: override policies()/kpi_calculators() for custom rules and KPIs.
    # Tier 2: implement build_model() with custom entity types (see domains/warehouse).
'''

TEMPLATE_MAPPING = """\
# Data mapping (FR-DOM-04): engine field -> column in your export. Edit paths and headers.
schema_version: "1.0"
domain: {id}
timezone: UTC
sources:
  events:
    path: data/station_events.csv
    kind: station_events
    columns: {{station: Station, start: Start, end: End}}
  arrivals:
    path: data/arrivals.csv
    kind: arrivals
    columns: {{time: Released}}
  periods:
    path: data/periods.csv
    kind: observed_kpis
    columns: {{period: Period, start: Start, end: End, operators: Operators, output: Output}}
backtest:
  base_scenario: scenarios/baseline.yaml
  periods: periods
  levers: {{operators: operators}}
  arrival_paths: {{"*": demand.arrivals.interarrival.mean}}
  kpis: {{output: output}}
"""

TEMPLATE_BEHAVIORS = """\
schema_version: "1.0"
agent_types:
  - id: operator
    description: Station operator
"""

TEMPLATE_SCENARIO = """\
schema_version: "1.0"
id: {id}_baseline
name: {name} baseline
domain: {id}
behaviors: behaviors.yaml
duration: 8 h
fleet:
  - {{agent_type: operator, count: 2}}
demand:
  - {{kind: stochastic, stream: arrivals, task_type: unit,
     interarrival: {{kind: exponential, mean: 15}}}}
"""

CHECKLIST = """\
# {name} onboarding checklist

1. Describe the process in `domain.yaml` (stations, routings, resources, levers, metrics).
2. Export data into `data/` and map it in `mapping.yaml`.
3. `simcore domain validate .` until it reports no errors.
4. `simcore domain backtest . --out report/` and read `report/validation_report.md`.
5. Fix what the report flags; repeat 3-4 until the rating is PASS (or an accepted WARN).
6. Review `report/domain.fitted.yaml`, then `simcore domain publish . --fitted --to <path>`.
"""


@click.group()
def domain() -> None:
    """Domain onboarding: init, validate, backtest, publish (FR-DOM-08)."""


def _fail(msg: str) -> None:
    click.echo(click.style("error: ", fg="red", bold=True) + msg, err=True)
    sys.exit(1)


def _load_definition(path: Path) -> Any:
    from simcore.core.config import ConfigError, validate_spec
    from simcore.specs.domain import DomainDefinition

    try:
        return validate_spec(DomainDefinition, yaml.safe_load(path.read_text()) or {}, str(path))
    except (ConfigError, OSError) as exc:
        _fail(str(exc))


@domain.command("init")
@click.argument("name")
@click.option("--tier", type=click.Choice(["0", "1", "2"]), default="0")
@click.option("--dir", "parent", type=click.Path(path_type=Path), default=Path("."))
def init(name: str, tier: str, parent: Path) -> None:
    """Scaffold a domain workspace NAME (domain.yaml, mapping, scenario, checklist)."""
    import re

    did = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")
    root = parent / did
    if root.exists():
        _fail(f"{root} already exists")
    (root / "data").mkdir(parents=True)
    (root / "scenarios").mkdir()
    fmt = {
        "id": did,
        "name": name,
        "tier": tier,
        "cls": "".join(p.title() for p in did.split("_")) + "Plugin",
    }
    doc = TEMPLATE_T0.format(**fmt)
    if tier != "0":
        doc = doc.replace("tier: 0", f"tier: {tier}\nplugin: {did}")
        (root / "__init__.py").write_text(TEMPLATE_PLUGIN.format(**fmt))
    (root / "domain.yaml").write_text(doc)
    (root / "mapping.yaml").write_text(TEMPLATE_MAPPING.format(**fmt))
    (root / "scenarios" / "baseline.yaml").write_text(TEMPLATE_SCENARIO.format(**fmt))
    (root / "scenarios" / "behaviors.yaml").write_text(TEMPLATE_BEHAVIORS)
    (root / "CHECKLIST.md").write_text(CHECKLIST.format(**fmt))
    (root / "data" / "README.md").write_text(
        "Put exports here. Use synthetic or approved data only; never commit customer data.\n"
    )
    click.echo(
        f"created {root} (tier {tier}); next: edit domain.yaml, then "
        f"`simcore domain validate {root}`"
    )


def _workspace(path: Path, definition: Path | None) -> tuple[Path, Path, Path]:
    root = path if path.is_dir() else path.parent
    d = definition or root / "domain.yaml"
    m = root / "mapping.yaml"
    for f in (d, m):
        if not f.exists():
            _fail(f"{f} not found")
    return root, d, m


@domain.command("validate")
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@click.option("--definition", type=click.Path(exists=True, path_type=Path), default=None)
@click.option("--json", "as_json", is_flag=True)
def validate(path: Path, definition: Path | None, as_json: bool) -> None:
    """Validate domain.yaml, mapping.yaml, the data (DQ report) and the base scenario."""
    from simcore.core.config import ConfigError
    from simcore.onboarding.mapping import load_mapping, map_data

    root, dpath, mpath = _workspace(path, definition)
    defn = _load_definition(dpath)
    try:
        spec = load_mapping(mpath)
    except ConfigError as exc:
        _fail(str(exc))
    if spec.domain != defn.id:
        _fail(f"mapping.yaml is for domain {spec.domain!r} but domain.yaml defines {defn.id!r}")
    _, dq = map_data(spec, root, defn)
    problems = []
    if spec.backtest is not None:
        from simcore.core.config import load_scenario

        try:
            base = load_scenario(root / spec.backtest.base_scenario)
            if base.scenario.domain != defn.id:
                problems.append(f"base scenario is for domain {base.scenario.domain!r}")
        except ConfigError as exc:
            problems.append(str(exc))
    if as_json:
        click.echo(
            json.dumps(
                {
                    "domain": defn.id,
                    "version": defn.version,
                    "data_quality": dq.to_dict(),
                    "problems": problems,
                },
                indent=2,
            )
        )
    else:
        click.echo(f"domain {defn.id} {defn.version} (tier {defn.tier}): definition OK")
        click.echo(dq.markdown())
        for p in problems:
            click.echo(click.style("error: ", fg="red") + p)
    sys.exit(0 if dq.ok and not problems else 1)


@domain.command("backtest")
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@click.option("--definition", type=click.Path(exists=True, path_type=Path), default=None)
@click.option(
    "--out",
    type=click.Path(path_type=Path),
    default=None,
    help="Report directory (default <workspace>/report).",
)
@click.option("--replications", type=int, default=None)
@click.option(
    "--no-fit", is_flag=True, help="Backtest domain.yaml as written (skip input fitting)."
)
def backtest_cmd(
    path: Path, definition: Path | None, out: Path | None, replications: int | None, no_fit: bool
) -> None:
    """Fit inputs from data, replay the historical periods and write the Validation Report."""
    from simcore.onboarding import apply_inputs, backtest, fit_inputs, load_mapping, map_data

    root, dpath, mpath = _workspace(path, definition)
    defn = _load_definition(dpath)
    spec = load_mapping(mpath)
    if spec.backtest is None:
        _fail("mapping.yaml has no backtest section")
    data, dq = map_data(spec, root, defn)
    inputs = None if no_fit else fit_inputs(data, defn)
    fitted = defn if inputs is None else apply_inputs(defn, inputs)
    report = backtest(fitted, spec, root, data, dq, inputs=inputs, replications=replications)
    files = report.write(out or root / "report", fitted if inputs is not None else None)
    color = {"pass": "green", "warn": "yellow", "fail": "red"}[report.rating]
    click.echo(f"Validation Report: {click.style(report.rating.upper(), fg=color, bold=True)}")
    for k in report.kpis:
        click.echo(f"  {k.metric}: error {k.mape:.1%}, coverage {k.coverage:.0%} -> {k.rating}")
    for d in report.diagnostics:
        click.echo(f"  [{d['severity']}] {d['title']}")
        if d.get("action"):
            click.echo(f"      action: {d['action']}")
    for kind, f in files.items():
        click.echo(f"wrote {kind}: {f}")
    sys.exit(0 if report.passed else 1)


@domain.command("publish")
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--to",
    "dest",
    type=click.Path(path_type=Path),
    required=True,
    help="A directory on SIMCORE_DOMAIN_PATH.",
)
@click.option("--fitted", is_flag=True, help="Publish report/domain.fitted.yaml.")
@click.option("--report", "report_dir", type=click.Path(path_type=Path), default=None)
@click.option("--allow-warn/--no-allow-warn", default=True)
def publish(
    path: Path, dest: Path, fitted: bool, report_dir: Path | None, allow_warn: bool
) -> None:
    """Publish a definition whose latest Validation Report passed (human-run, FR-DOM-09/10)."""
    root = path if path.is_dir() else path.parent
    rdir = report_dir or root / "report"
    rfile = rdir / "validation_report.json"
    if not rfile.exists():
        _fail(f"no Validation Report at {rfile}; run `simcore domain backtest` first")
    report = json.loads(rfile.read_text())
    if report["rating"] == "fail" or (report["rating"] == "warn" and not allow_warn):
        _fail(
            f"the latest Validation Report is {report['rating'].upper()}; fix it before publishing"
        )
    src = rdir / "domain.fitted.yaml" if fitted else root / "domain.yaml"
    defn = _load_definition(src)
    if defn.version != report["domain_version"] and not fitted:
        _fail(
            f"domain.yaml is version {defn.version} but the report validated "
            f"{report['domain_version']}; re-run the backtest"
        )
    target = dest / defn.id
    versioned = target / "versions" / defn.version / "domain.yaml"
    if versioned.exists():
        _fail(f"{defn.id} {defn.version} is already published; bump the version")
    versioned.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, versioned)
    shutil.copyfile(rfile, versioned.parent / "validation_report.json")
    shutil.copyfile(src, target / "domain.yaml")
    click.echo(f"published {defn.id} {defn.version} to {target} (report: {report['rating']})")


@domain.command("ingest-facility")
@click.argument("directory", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--id", "facility_id", required=True)
@click.option("--out", type=click.Path(path_type=Path), required=True, help="facility.yaml")
@click.option("--usd", type=click.Path(path_type=Path), default=None, help="Also write .usda")
def ingest_facility(directory: Path, facility_id: str, out: Path, usd: Path | None) -> None:
    """Build a facility manifest (and optionally a minimal USD scene) from layout CSVs."""
    from simcore.core.config import ConfigError
    from simcore.onboarding.facility_csv import facility_from_csv, facility_usda

    try:
        m = facility_from_csv(directory, facility_id)
    except ConfigError as exc:
        _fail(str(exc))
    doc = m.model_dump(mode="json", by_alias=True, exclude_none=True, exclude_defaults=True)
    doc = {"schema_version": "1.0", **doc}
    if usd is not None:
        usd.write_text(facility_usda(m))
        doc["usd_stage"] = str(usd)
    out.write_text(yaml.safe_dump(doc, sort_keys=False))
    click.echo(
        f"wrote {out} ({len(m.nodes)} nodes, {len(m.edges)} edges)" + (f" and {usd}" if usd else "")
    )

"""``simcore`` CLI: validate, run, experiment, fit, report, replay, schemas, domains (FR-IFC-01)."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

import click

from simcore.core.config import ConfigError, load_document, load_scenario, validate_spec
from simcore.specs.registry import SPECS, export_schemas

log = logging.getLogger("simcore.cli")


def _plugins(extra: tuple[str, ...]) -> Any:
    from simcore.api import default_plugins

    return default_plugins(extra)


def _infer_kind(doc: dict[str, Any]) -> str:
    if "agent_types" in doc:
        return "behaviors"
    if "base_scenario" in doc:
        return "experiment"
    if "tier" in doc:
        return "domain_definition"
    if "nodes" in doc or "edges" in doc or "zones" in doc or "locations" in doc:
        return "facility"
    if "domain" in doc and ("duration" in doc or "extends" in doc):
        return "scenario"
    raise click.UsageError("cannot infer document kind; pass --kind")


def _fail(exc: Exception) -> None:
    click.echo(click.style("error: ", fg="red", bold=True) + str(exc), err=True)
    sys.exit(1)


@click.group()
@click.option("-v", "--verbose", count=True, help="Increase log verbosity.")
@click.version_option(package_name="simcore")
def cli(verbose: int) -> None:
    """simcore: reusable simulation engine."""
    logging.basicConfig(
        level=logging.WARNING - 10 * verbose,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


@cli.command()
@click.argument("path", type=click.Path(path_type=Path))
@click.option("--kind", type=click.Choice(sorted(SPECS)), default=None)
@click.option("--plugin", "plugins", multiple=True, help="Extra plug-in 'module:attr'.")
def validate(path: Path, kind: str | None, plugins: tuple[str, ...]) -> None:
    """Validate a config document (scenario, facility, behaviors, experiment, ...)."""
    try:
        doc = load_document(path)
        kind = kind or _infer_kind(doc)
        if kind == "scenario":
            bundle = load_scenario(path)
            _plugins(plugins).get(bundle.scenario.domain).validate_scenario(bundle)
        elif kind == "experiment":
            from simcore.core.config import load_experiment

            _, bundle = load_experiment(path)
            _plugins(plugins).get(bundle.scenario.domain).validate_scenario(bundle)
        else:
            validate_spec(SPECS[kind], doc, str(path))
    except (ConfigError, Exception) as exc:
        _fail(exc)
    click.echo(f"OK: {path} is a valid {kind}")


@cli.command()
@click.argument("scenario", type=click.Path(path_type=Path))
@click.option("--seed", type=int, default=None, help="Master seed (default: scenario.seed).")
@click.option("--replication", type=int, default=0)
@click.option("--mode", type=click.Choice(["logical", "kinematic", "physics"]), default="logical")
@click.option("--out", type=click.Path(path_type=Path), default=Path("runs"))
@click.option("--experiment-id", default="adhoc")
@click.option("--jsonl", is_flag=True, help="Also write events.jsonl for debugging.")
@click.option("--set", "sets", multiple=True, help="Override 'path=value' (YAML value).")
@click.option("--plugin", "plugins", multiple=True)
def run(
    scenario: Path,
    seed: int | None,
    replication: int,
    mode: str,
    out: Path,
    experiment_id: str,
    jsonl: bool,
    sets: tuple[str, ...],
    plugins: tuple[str, ...],
) -> None:
    """Run one replication of SCENARIO and write runs/<exp>/<scenario>/<rep>/ (AC-ENG-01)."""
    import yaml

    from simcore.api import run_scenario

    overrides = {}
    for item in sets:
        key, sep, value = item.partition("=")
        if not sep:
            raise click.UsageError(f"--set expects path=value, got {item!r}")
        overrides[key] = yaml.safe_load(value)
    try:
        bundle = load_scenario(scenario, overrides)
        rep_dir = out / experiment_id / bundle.scenario.id / f"rep_{replication:03d}"
        result = run_scenario(
            bundle,
            seed=seed,
            replication=replication,
            mode=mode,
            out=rep_dir,
            plugins=_plugins(plugins),
            jsonl=jsonl,
        )
    except Exception as exc:
        _fail(exc)
    click.echo(f"wrote {rep_dir}")
    for name, kpi_value in result.kpis.items():
        click.echo(f"  {name:28s} {kpi_value:.6g}")


@cli.command()
@click.argument("events", type=click.Path(exists=True, path_type=Path))
@click.option("--at", "times", type=float, multiple=True, help="Sim-times to report state at.")
def replay(events: Path, times: tuple[float, ...]) -> None:
    """Summarize a run's event log without re-simulating (FR-OBS-04; viewport replay in M4/M11)."""
    from simcore.core.obs.eventlog import EventLog, EventType

    log_ = EventLog.from_parquet(events)
    counts: dict[str, int] = {}
    for r in log_:
        counts[r.type] = counts.get(r.type, 0) + 1
    click.echo(
        json.dumps({"records": len(log_), "by_type": dict(sorted(counts.items()))}, indent=2)
    )
    for at in times:
        states: dict[str, str] = {}
        for r in log_.of_type(EventType.AGENT_STATE):
            if r.time > at:
                break
            if r.agent:
                states[r.agent] = str(r.data.get("state"))
        click.echo(f"t={at}: " + json.dumps(states, sort_keys=True))


@cli.group()
def schemas() -> None:
    """JSON Schemas for the spec contracts."""


@schemas.command("export")
@click.option("--out", type=click.Path(path_type=Path), default=Path("schemas"))
def schemas_export(out: Path) -> None:
    """Write schemas/<spec>.schema.json from the Pydantic models."""
    for p in export_schemas(out):
        click.echo(f"wrote {p}")


@cli.group()
def domains() -> None:
    """Installed domain plug-ins."""


@domains.command("list")
@click.option("--plugin", "plugins", multiple=True)
@click.option("--json", "as_json", is_flag=True)
def domains_list(plugins: tuple[str, ...], as_json: bool) -> None:
    registry = _plugins(plugins)
    manifests = [registry.get(n).manifest().to_dict() for n in registry.names]
    if as_json:
        click.echo(json.dumps(manifests, indent=2, default=str))
        return
    for m in manifests:
        click.echo(f"{m['name']} {m['version']}: {m['description']}")


def main() -> None:  # pragma: no cover
    cli()

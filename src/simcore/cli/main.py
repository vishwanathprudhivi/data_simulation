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
        elif kind == "facility":
            from simcore.core.facility.validate import validate_facility

            manifest = validate_spec(SPECS[kind], doc, str(path))
            found = validate_facility(manifest)  # type: ignore[arg-type]
            for issue in found:
                click.echo(str(issue), err=True)
            if any(i.severity == "error" for i in found):
                sys.exit(1)
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
@click.option(
    "--step",
    type=float,
    default=None,
    help="Fixed step (s); default event-driven (logical) or 0.5 s (kinematic).",
)
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
    step: float | None,
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
            step=step,
        )
    except Exception as exc:
        _fail(exc)
    click.echo(f"wrote {rep_dir}")
    for name, kpi_value in result.kpis.items():
        click.echo(f"  {name:28s} {kpi_value:.6g}")


@cli.command()
@click.argument("experiment", type=click.Path(path_type=Path))
@click.option("--out", type=click.Path(path_type=Path), default=Path("runs"))
@click.option(
    "--parallel",
    "parallelism",
    type=int,
    default=None,
    help="Worker processes (default: experiment.parallelism).",
)
@click.option("--retries", type=int, default=0, help="Retries per failed replication.")
@click.option("--plugin", "plugins", multiple=True)
def experiment(
    experiment: Path,
    out: Path,
    parallelism: int | None,
    retries: int,
    plugins: tuple[str, ...],
) -> None:
    """Run EXPERIMENT (baseline + variants x replications) and write a report (FR-EXP-*)."""

    def show(ev: Any) -> None:
        click.echo(
            f"  [{ev.completed}/{ev.planned}] {ev.variant_id} rep {ev.replication}: {ev.status}",
            err=True,
        )

    try:
        from simcore.core.config import load_experiment
        from simcore.experiment.runner import ExperimentRunner

        spec, base = load_experiment(experiment)
        result = ExperimentRunner(
            spec,
            base,
            _plugins(plugins),
            out_root=out,
            parallelism=parallelism,
            retries=retries,
            progress=show,
        ).run()
    except Exception as exc:
        _fail(exc)
    click.echo(f"wrote {out / spec.id}")
    for c in result.comparisons:
        d = c.diff
        flag = "excludes 0" if c.ci_excludes_zero else "includes 0"
        click.echo(
            f"  {c.variant} vs {c.reference} {c.metric}: {d.mean:+.4g} "
            f"[{d.low:.4g}, {d.high:.4g}] ({flag})"
        )
    for w in result.warnings:
        click.echo(click.style("warning: ", fg="yellow") + w)


@cli.command()
@click.argument("data", type=click.Path(exists=True, path_type=Path))
@click.option("--column", default=None, help="CSV column holding the samples.")
@click.option("--unit", default=None, help="Unit of the samples, e.g. 'min' (converted to SI).")
@click.option(
    "--out",
    type=click.Path(path_type=Path),
    default=None,
    help="Write the best distribution spec as YAML here.",
)
@click.option("--json", "as_json", is_flag=True, help="Print the full fit report as JSON.")
def fit(data: Path, column: str | None, unit: str | None, out: Path | None, as_json: bool) -> None:
    """Fit candidate distributions to samples and rank them (FR-EXP-08)."""
    import yaml

    from simcore.experiment.fitting import fit_distributions, read_samples

    try:
        report = fit_distributions(read_samples(data, column), unit=unit)
    except Exception as exc:
        _fail(exc)
    if as_json:
        click.echo(json.dumps(report.to_dict(), indent=2))
    else:
        click.echo(f"n = {report.n}; ranked by AIC (lower is better)")
        click.echo(
            f"  {'distribution':12s} {'AIC':>12s} {'KS':>8s} {'KS p':>8s} {'AD':>8s}  params"
        )
        for c in report.candidates:
            params = ", ".join(f"{k}={v:.4g}" for k, v in c.params.items())
            click.echo(
                f"  {c.name:12s} {c.aic:12.1f} {c.ks_stat:8.4f} {c.ks_pvalue:8.3f} "
                f"{c.ad_stat:8.3f}  {params}"
            )
        for name, why in report.skipped.items():
            click.echo(f"  {name:12s} skipped: {why}")
    if out is not None:
        out.write_text(yaml.safe_dump(report.best.spec, sort_keys=False))
        click.echo(f"wrote {out} ({report.best.name})")


@cli.command()
@click.argument("experiment_dir", type=click.Path(exists=True, path_type=Path))
def report(experiment_dir: Path) -> None:
    """Regenerate report.md / report.html from an experiment's summary.json (FR-IFC-06)."""
    from simcore.experiment.report import write_reports

    summary_path = experiment_dir / "summary.json"
    if not summary_path.exists():
        _fail(FileNotFoundError(f"{summary_path} not found; run 'simcore experiment' first"))
    files = write_reports(json.loads(summary_path.read_text()), experiment_dir)
    for p in files.values():
        click.echo(f"wrote {p}")


@cli.command()
@click.argument("scenario", type=click.Path(path_type=Path))
@click.option("--replications", type=int, default=5)
@click.option("--seed", type=int, default=0)
@click.option(
    "--signal",
    type=click.Choice(["queue_length", "cycle_time", "wait_time", "throughput"]),
    default="queue_length",
)
@click.option("--bucket", type=float, default=60.0, help="Bucket width in seconds.")
@click.option("--window", type=int, default=5, help="Welch moving-average half-width.")
@click.option(
    "--experiment-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Attach the diagnostic to this experiment's summary and report.",
)
@click.option("--plugin", "plugins", multiple=True)
def warmup(
    scenario: Path,
    replications: int,
    seed: int,
    signal: str,
    bucket: float,
    window: int,
    experiment_dir: Path | None,
    plugins: tuple[str, ...],
) -> None:
    """Welch's-method warm-up diagnostic for SCENARIO (FR-EXP-06)."""
    from simcore.experiment.report import write_reports
    from simcore.experiment.warmup import welch_diagnostic

    try:
        diag = welch_diagnostic(
            load_scenario(scenario),
            _plugins(plugins),
            replications=replications,
            master_seed=seed,
            signal=signal,
            bucket=bucket,
            window=window,
        )
    except Exception as exc:
        _fail(exc)
    click.echo(
        f"configured warm-up: {diag.configured:g} s; suggested: "
        f"{'n/a' if diag.suggested is None else f'{diag.suggested:g} s'}"
    )
    if experiment_dir is not None:
        path = experiment_dir / "summary.json"
        summary = json.loads(path.read_text())
        summary["warmup"] = diag.to_dict()
        path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
        write_reports(summary, experiment_dir)
        click.echo(f"updated {experiment_dir}/report.html")
    else:
        click.echo(json.dumps(diag.to_dict()))


@cli.command()
@click.argument("events", type=click.Path(exists=True, path_type=Path))
@click.option("--at", "times", type=float, multiple=True, help="Sim-times to report state at.")
@click.option(
    "--scenario",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Scenario of the run; adds agent positions from its facility.",
)
def replay(events: Path, times: tuple[float, ...], scenario: Path | None) -> None:
    """Replay a run from its event log without re-simulating (FR-OBS-04)."""
    from simcore.core.obs.eventlog import EventLog
    from simcore.core.obs.replay import agent_states_at, snapshot

    log_ = EventLog.from_parquet(events)
    counts: dict[str, int] = {}
    for r in log_:
        counts[r.type] = counts.get(r.type, 0) + 1
    click.echo(
        json.dumps({"records": len(log_), "by_type": dict(sorted(counts.items()))}, indent=2)
    )
    nav = None
    if scenario is not None:
        from simcore.core.facility.graph import NavGraph

        fac = load_scenario(scenario).facility
        nav = NavGraph(fac) if fac is not None else None
    for at in times:
        if nav is not None:
            snap = snapshot(log_, nav, at)
            click.echo(f"t={at}: " + json.dumps(snap, sort_keys=True, default=list))
        else:
            states = {a: v["state"] for a, v in agent_states_at(log_, at).items()}
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

"""Experiment runner (FR-EXP-02..07/09/10, FR-IFC-04, NFR-08).

Runs every alternative x replication, in-process or across a bounded pool of
worker processes, each replication guarded by a wall-clock timeout. A failed
replication is recorded (with retries) and never aborts the experiment.

Output layout (BRD 7.5)::

    <out>/<experiment_id>/
        experiment.json   spec + variants + provenance
        results.parquet   one row per (variant, replication, metric)
        runs.parquet      one row per (variant, replication): status, seeds, hashes
        summary.json      CIs per variant/metric and comparisons vs the reference
        report.md / report.html
        <variant>/<rep_id>/{events.parquet,kpis.json,meta.json}
"""

from __future__ import annotations

import json
import logging
import math
import multiprocessing
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from simcore.adapters import get_backend
from simcore.core.config import ScenarioBundle
from simcore.core.engine import Engine
from simcore.core.plugins import PluginRegistry
from simcore.core.provenance import engine_version, git_sha, library_versions
from simcore.experiment.design import BASELINE_ID, Variant, build_variants, seed_for
from simcore.experiment.stats import CIEstimate, mean_ci, paired_ci, welch_ci
from simcore.specs.experiment import ExperimentSpec

__all__ = [
    "ComparisonResult",
    "ExperimentCancelled",
    "ExperimentResult",
    "ExperimentRunner",
    "MetricSummary",
    "ProgressEvent",
    "ReplicationJob",
    "ReplicationOutcome",
    "run_job",
]

log = logging.getLogger(__name__)


# ----------------------------------------------------------------------------- jobs


class ExperimentCancelled(RuntimeError):
    """The experiment was cancelled before all replications finished."""


@dataclass(frozen=True)
class ReplicationJob:
    experiment_id: str
    variant_id: str
    bundle: ScenarioBundle
    master_seed: int
    replication: int
    mode: str
    plugins: PluginRegistry
    out_dir: Path | None = None
    timeout_s: float | None = None
    retries: int = 0
    jsonl: bool = False


@dataclass
class ReplicationOutcome:
    variant_id: str
    replication: int
    master_seed: int
    status: str  # "ok" | "failed"
    kpis: dict[str, float] = field(default_factory=dict)
    error: str | None = None
    attempts: int = 1
    wall_time_s: float = 0.0
    event_log_sha256: str | None = None
    path: str | None = None
    config_hash: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def run_job(job: ReplicationJob) -> ReplicationOutcome:
    """Run one replication with retries. Module-level so worker processes can import it."""
    error = None
    for attempt in range(1, job.retries + 2):
        engine = Engine(job.plugins, get_backend, mode=job.mode, timeout_s=job.timeout_s)
        try:
            engine.load_config(job.bundle).build_world()
            result = engine.run_replication(job.master_seed, job.replication)
            if job.out_dir is not None:
                result.write(job.out_dir, jsonl=job.jsonl)
            return ReplicationOutcome(
                variant_id=job.variant_id,
                replication=job.replication,
                master_seed=job.master_seed,
                status="ok",
                kpis=result.kpis,
                attempts=attempt,
                wall_time_s=float(result.meta["wall_time_s"]),
                event_log_sha256=result.meta.get("event_log_sha256"),
                path=str(job.out_dir) if job.out_dir is not None else None,
                config_hash=job.bundle.config_hash,
            )
        except Exception as exc:  # NFR-08: record, don't abort the experiment
            error = f"{type(exc).__name__}: {exc}"
            log.warning(
                "replication failed",
                extra={
                    "experiment": job.experiment_id,
                    "variant": job.variant_id,
                    "replication": job.replication,
                    "attempt": attempt,
                    "error": error,
                },
            )
        finally:
            engine.teardown()
    return ReplicationOutcome(
        variant_id=job.variant_id,
        replication=job.replication,
        master_seed=job.master_seed,
        status="failed",
        error=error,
        attempts=job.retries + 1,
        config_hash=job.bundle.config_hash,
    )


@dataclass(frozen=True)
class ProgressEvent:
    """Emitted after each replication (consumed by the API's SSE stream in M7)."""

    experiment_id: str
    variant_id: str
    replication: int
    status: str
    completed: int
    planned: int


# ----------------------------------------------------------------------------- results


@dataclass(frozen=True)
class MetricSummary:
    variant: str
    metric: str
    ci: CIEstimate


@dataclass(frozen=True)
class ComparisonResult:
    variant: str
    reference: str
    metric: str
    diff: CIEstimate  # variant - reference
    paired: bool
    unpaired_half_width: float | None  # same data, pairing ignored (shows CRN's effect)

    @property
    def ci_excludes_zero(self) -> bool:
        return self.diff.excludes_zero


def _ci_dict(ci: CIEstimate) -> dict[str, Any]:
    def num(x: float) -> float | None:
        return None if math.isnan(x) or math.isinf(x) else x

    return {
        "mean": num(ci.mean),
        "half_width": num(ci.half_width),
        "ci": [num(ci.low), num(ci.high)],
        "n": ci.n,
        "sd": num(ci.sd),
        "confidence": ci.confidence,
        "warnings": list(ci.warnings),
    }


@dataclass
class ExperimentResult:
    spec: ExperimentSpec
    variants: list[Variant]
    outcomes: list[ReplicationOutcome]
    mode: str
    metrics: list[str]
    summaries: list[MetricSummary]
    comparisons: list[ComparisonResult]
    reference: str
    warnings: list[str]
    files: dict[str, Path] = field(default_factory=dict)

    # -- lookups -------------------------------------------------------------

    def summary(self, variant: str, metric: str) -> CIEstimate:
        for s in self.summaries:
            if s.variant == variant and s.metric == metric:
                return s.ci
        raise KeyError(f"no summary for variant {variant!r}, metric {metric!r}")

    def comparison(self, variant: str, metric: str) -> ComparisonResult:
        for c in self.comparisons:
            if c.variant == variant and c.metric == metric:
                return c
        raise KeyError(f"no comparison for variant {variant!r}, metric {metric!r}")

    def values(self, variant: str, metric: str) -> list[float]:
        return [
            o.kpis.get(metric, math.nan) for o in self.outcomes if o.variant_id == variant and o.ok
        ]

    @property
    def failed(self) -> list[ReplicationOutcome]:
        return [o for o in self.outcomes if not o.ok]

    # -- serialization -----------------------------------------------------------

    def provenance(self) -> dict[str, Any]:
        base = self.variants[0].bundle
        assert base is not None
        return {
            "engine_version": engine_version(),
            "git_sha": git_sha(),
            "versions": library_versions(),
            "mode": self.mode,
            "domain": base.scenario.domain,
            "base_scenario": base.source,
        }

    def summary_dict(self) -> dict[str, Any]:
        return {
            "experiment_id": self.spec.id,
            "reference": self.reference,
            "mode": self.mode,
            "crn": self.spec.crn,
            "confidence": self.spec.confidence,
            "replications_planned": self.spec.replications,
            "replications_ok": {
                v.id: sum(1 for o in self.outcomes if o.variant_id == v.id and o.ok)
                for v in self.variants
            },
            "metrics": self.metrics,
            "variants": [
                {
                    "id": v.id,
                    "name": v.name,
                    "overrides": dict(v.overrides),
                    "factors": dict(v.factors),
                    "config_hash": v.bundle.config_hash if v.bundle else None,
                }
                for v in self.variants
            ],
            "summaries": [
                {"variant": s.variant, "metric": s.metric, **_ci_dict(s.ci)} for s in self.summaries
            ],
            "comparisons": [
                {
                    "variant": c.variant,
                    "reference": c.reference,
                    "metric": c.metric,
                    "paired": c.paired,
                    "diff": _ci_dict(c.diff),
                    "ci_excludes_zero": c.ci_excludes_zero,
                    "unpaired_half_width": c.unpaired_half_width,
                }
                for c in self.comparisons
            ],
            "failures": [
                {
                    "variant": o.variant_id,
                    "replication": o.replication,
                    "error": o.error,
                    "attempts": o.attempts,
                }
                for o in self.failed
            ],
            "warnings": self.warnings,
            "provenance": self.provenance(),
        }

    def write(self, directory: Path) -> dict[str, Path]:
        from simcore.experiment.report import write_reports

        directory.mkdir(parents=True, exist_ok=True)
        prov = self.provenance()
        by_variant = {v.id: v for v in self.variants}
        rows: dict[str, list[Any]] = {
            k: []
            for k in (
                "experiment_id",
                "variant",
                "replication",
                "master_seed",
                "metric",
                "value",
                "config_hash",
                "domain",
                "engine_version",
                "git_sha",
                "mode",
            )
        }
        runs: dict[str, list[Any]] = {
            k: []
            for k in (
                "experiment_id",
                "variant",
                "replication",
                "master_seed",
                "status",
                "error",
                "attempts",
                "wall_time_s",
                "event_log_sha256",
                "path",
                "config_hash",
            )
        }
        for o in self.outcomes:
            runs["experiment_id"].append(self.spec.id)
            runs["variant"].append(o.variant_id)
            runs["replication"].append(o.replication)
            runs["master_seed"].append(o.master_seed)
            runs["status"].append(o.status)
            runs["error"].append(o.error)
            runs["attempts"].append(o.attempts)
            runs["wall_time_s"].append(o.wall_time_s)
            runs["event_log_sha256"].append(o.event_log_sha256)
            runs["path"].append(o.path)
            runs["config_hash"].append(o.config_hash)
            for metric, value in sorted(o.kpis.items()):
                rows["experiment_id"].append(self.spec.id)
                rows["variant"].append(o.variant_id)
                rows["replication"].append(o.replication)
                rows["master_seed"].append(o.master_seed)
                rows["metric"].append(metric)
                rows["value"].append(value)
                rows["config_hash"].append(o.config_hash)
                rows["domain"].append(prov["domain"])
                rows["engine_version"].append(prov["engine_version"])
                rows["git_sha"].append(prov["git_sha"])
                rows["mode"].append(self.mode)
        files = {
            "results": directory / "results.parquet",
            "runs": directory / "runs.parquet",
            "summary": directory / "summary.json",
            "experiment": directory / "experiment.json",
        }
        pq.write_table(pa.table(rows), files["results"], compression="zstd")
        pq.write_table(pa.table(runs), files["runs"], compression="zstd")
        files["summary"].write_text(
            json.dumps(self.summary_dict(), indent=2, sort_keys=True, default=str) + "\n"
        )
        files["experiment"].write_text(
            json.dumps(
                {
                    "spec": self.spec.model_dump(mode="json"),
                    "variants": {vid: dict(v.overrides) for vid, v in by_variant.items()},
                    "provenance": prov,
                },
                indent=2,
                sort_keys=True,
                default=str,
            )
            + "\n"
        )
        files.update(write_reports(self.summary_dict(), directory))
        self.files = files
        return files


# ----------------------------------------------------------------------------- runner

Progress = Callable[[ProgressEvent], None]


class ExperimentRunner:
    """Execute an :class:`ExperimentSpec` against a base scenario.

    ``out_root=None`` keeps everything in memory (no files), which is what the
    evaluator and tests use.
    """

    def __init__(
        self,
        spec: ExperimentSpec,
        base: ScenarioBundle,
        plugins: PluginRegistry | None = None,
        *,
        out_root: Path | None = None,
        mode: str | None = None,
        parallelism: int | None = None,
        retries: int = 0,
        jsonl: bool = False,
        progress: Progress | None = None,
        cancel: Any = None,
    ) -> None:
        if plugins is None:
            from simcore.api.engine import default_plugins

            plugins = default_plugins()
        self.spec = spec
        self.base = base
        self.plugins = plugins
        self.mode = mode or spec.fidelity.screen
        self.parallelism = parallelism or spec.parallelism
        self.retries = retries
        self.out_root = out_root
        self.jsonl = jsonl
        self.progress = progress
        self.cancel = cancel  # anything with is_set(), e.g. threading.Event
        self.variants = build_variants(spec, base)
        plugin = plugins.get(base.scenario.domain)
        for v in self.variants:  # fail fast before running anything (FR-ENG-08)
            assert v.bundle is not None
            plugin.validate_scenario(v.bundle)
        self._completed = 0
        self._planned = 0

    @property
    def experiment_dir(self) -> Path | None:
        return None if self.out_root is None else self.out_root / self.spec.id

    def _jobs(self, replications: Sequence[int]) -> list[ReplicationJob]:
        jobs = []
        for v in self.variants:
            assert v.bundle is not None
            for rep in replications:
                master, rep_id = seed_for(self.spec, v.id, rep)
                out = None
                if self.experiment_dir is not None:
                    out = self.experiment_dir / v.id / f"rep_{rep:03d}"
                jobs.append(
                    ReplicationJob(
                        experiment_id=self.spec.id,
                        variant_id=v.id,
                        bundle=v.bundle,
                        master_seed=master,
                        replication=rep_id,
                        mode=self.mode,
                        plugins=self.plugins,
                        out_dir=out,
                        timeout_s=self.spec.timeout_s,
                        retries=self.retries,
                        jsonl=self.jsonl,
                    )
                )
        return jobs

    def _execute(self, jobs: list[ReplicationJob]) -> list[ReplicationOutcome]:
        self._planned += len(jobs)
        outcomes: list[ReplicationOutcome] = []

        def done(o: ReplicationOutcome) -> None:
            self._completed += 1
            outcomes.append(o)
            if self.progress is not None:
                self.progress(
                    ProgressEvent(
                        self.spec.id,
                        o.variant_id,
                        o.replication,
                        o.status,
                        self._completed,
                        self._planned,
                    )
                )

        def cancelled() -> bool:
            return self.cancel is not None and bool(self.cancel.is_set())

        if self.parallelism <= 1 or len(jobs) <= 1:
            for job in jobs:
                if cancelled():
                    raise ExperimentCancelled(f"experiment {self.spec.id} cancelled")
                done(run_job(job))
        else:
            ctx = multiprocessing.get_context("spawn")
            workers = min(self.parallelism, len(jobs))
            with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as pool:
                futures = [pool.submit(run_job, job) for job in jobs]
                for job, fut in zip(jobs, futures):  # submission order: deterministic output
                    if cancelled():
                        for f in futures:
                            f.cancel()
                        raise ExperimentCancelled(f"experiment {self.spec.id} cancelled")
                    try:
                        done(fut.result())
                    except Exception as exc:  # worker crashed (e.g. killed)
                        done(
                            ReplicationOutcome(
                                job.variant_id,
                                job.replication,
                                job.master_seed,
                                "failed",
                                error=f"worker error: {exc!r}",
                                config_hash=job.bundle.config_hash,
                            )
                        )
        return outcomes

    def _adaptive_satisfied(self, outcomes: list[ReplicationOutcome]) -> bool:
        ad = self.spec.adaptive
        assert ad is not None
        for v in self.variants:
            values = [
                o.kpis.get(ad.metric, math.nan) for o in outcomes if o.variant_id == v.id and o.ok
            ]
            if len(values) < 2:
                return False
            import warnings

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                ci = mean_ci(values, self.spec.confidence)
            width = ci.relative_half_width if ad.relative else ci.half_width
            if not width <= ad.target_half_width:
                return False
        return True

    def run(self) -> ExperimentResult:
        n = self.spec.replications
        outcomes = self._execute(self._jobs(range(n)))
        warnings_: list[str] = []
        ad = self.spec.adaptive
        if ad is not None:  # FR-EXP-07 sequential replication
            step = max(1, self.parallelism)
            while not self._adaptive_satisfied(outcomes) and n < ad.max_replications:
                more = range(n, min(n + step, ad.max_replications))
                outcomes += self._execute(self._jobs(more))
                n = more.stop
            if not self._adaptive_satisfied(outcomes):
                warnings_.append(
                    f"adaptive target half-width {ad.target_half_width} on {ad.metric!r} not "
                    f"reached within max_replications={ad.max_replications}"
                )
            else:
                warnings_.append(f"adaptive replication stopped at n={n} per alternative")
        order = {v.id: i for i, v in enumerate(self.variants)}
        outcomes.sort(key=lambda o: (order[o.variant_id], o.replication))
        result = summarize(self.spec, self.variants, outcomes, self.mode, warnings_)
        if self.experiment_dir is not None:
            result.write(self.experiment_dir)
        return result


def summarize(
    spec: ExperimentSpec,
    variants: list[Variant],
    outcomes: list[ReplicationOutcome],
    mode: str,
    extra_warnings: Sequence[str] = (),
) -> ExperimentResult:
    """Replication CIs per alternative and comparisons vs the reference (FR-EXP-04/05/10)."""
    import warnings

    warn_list = list(extra_warnings)
    seen = sorted({k for o in outcomes if o.ok for k in o.kpis})
    metrics = list(spec.kpis) if spec.kpis else seen
    unknown = [m for m in metrics if m not in seen]
    if unknown:
        warn_list.append(f"requested KPIs never produced: {unknown}")
        metrics = [m for m in metrics if m in seen]
    failed = [o for o in outcomes if not o.ok]
    if failed:
        warn_list.append(f"{len(failed)} replication(s) failed; see 'failures'")
    if spec.fidelity.promote_top_k:
        warn_list.append(
            "fidelity promotion requested but kinematic/physics backends arrive in M4; "
            "all results are from the screening mode"
        )
    reference = BASELINE_ID if spec.include_baseline else variants[0].id

    def values(vid: str, metric: str) -> dict[int, float]:
        return {
            o.replication: o.kpis.get(metric, math.nan)
            for o in outcomes
            if o.variant_id == vid and o.ok
        }

    summaries: list[MetricSummary] = []
    comparisons: list[ComparisonResult] = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # notes are kept on each CIEstimate instead
        for v in variants:
            for m in metrics:
                summaries.append(
                    MetricSummary(v.id, m, mean_ci(list(values(v.id, m).values()), spec.confidence))
                )
        for v in variants:
            if v.id == reference:
                continue
            for m in metrics:
                ref, var = values(reference, m), values(v.id, m)
                unpaired = welch_ci(list(ref.values()), list(var.values()), spec.confidence)
                if spec.crn:
                    common = sorted(set(ref) & set(var))
                    diff = paired_ci(
                        [ref[r] for r in common], [var[r] for r in common], spec.confidence
                    )
                    comparisons.append(
                        ComparisonResult(v.id, reference, m, diff, True, unpaired.half_width)
                    )
                else:
                    comparisons.append(ComparisonResult(v.id, reference, m, unpaired, False, None))
    low = sorted({s.ci.n for s in summaries if s.ci.n < 5})
    if low:
        warn_list.append(
            f"some alternatives have fewer than 5 successful replications (n={low}); "
            "confidence intervals are not meaningful"
        )
    return ExperimentResult(
        spec, variants, outcomes, mode, metrics, summaries, comparisons, reference, warn_list
    )

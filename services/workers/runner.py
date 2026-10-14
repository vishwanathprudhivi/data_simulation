"""Execute one experiment job: run, record progress, store artifacts and benchmarks."""

from __future__ import annotations

import logging
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

from services.platform.artifacts import ArtifactStore
from services.platform.metadata import MetadataStore, now
from services.platform.settings import Settings
from simcore.core.config import ConfigError
from simcore.core.plugins import PluginRegistry
from simcore.experiment.runner import ExperimentCancelled, ExperimentRunner, ProgressEvent

__all__ = ["execute_job"]

log = logging.getLogger(__name__)


def execute_job(
    job_id: str,
    store: MetadataStore,
    artifacts: ArtifactStore,
    plugins: PluginRegistry,
    settings: Settings,
    cancel: threading.Event,
    notify: Callable[[str], None] | None = None,
) -> None:
    from services.platform.jobs import JobService

    def ping() -> None:
        if notify is not None:
            notify(job_id)

    job = store.job(job_id)
    if job is None or job["status"] in ("completed", "failed", "cancelled"):
        return
    if cancel.is_set():
        store.update_job(job_id, status="cancelled", finished_at=now())
        ping()
        return
    store.update_job(job_id, status="running", started_at=now())
    ping()
    timed_out = threading.Event()

    def on_timeout() -> None:
        timed_out.set()
        cancel.set()

    timer = threading.Timer(settings.job_timeout_s, on_timeout)
    timer.daemon = True
    timer.start()
    prefix = f"jobs/{job_id}"
    tmp: tempfile.TemporaryDirectory[str] | None = None
    try:
        spec, bundle, mode = JobService.load(job)
        out_root = artifacts.local_dir(prefix)
        if out_root is None:
            tmp = tempfile.TemporaryDirectory(prefix="simcore-job-")
            out_root = Path(tmp.name)

        def progress(ev: ProgressEvent) -> None:
            store.update_job(job_id, progress_completed=ev.completed, progress_planned=ev.planned)
            ping()

        runner = ExperimentRunner(
            spec,
            bundle,
            plugins,
            out_root=out_root,
            mode=mode,
            parallelism=settings.replication_parallelism,
            progress=progress,
            cancel=cancel,
        )
        result = runner.run()
        if artifacts.local_dir(prefix) is None:
            artifacts.put_tree(prefix, out_root)
        plugin = plugins.get(bundle.scenario.domain)
        agents = sum(f.count for f in bundle.scenario.fleet)
        for o in result.outcomes:
            if o.ok and o.wall_time_s > 0:
                store.add_benchmark(
                    plugin.name, mode, bundle.scenario.duration, o.wall_time_s, agents
                )
        status = "completed"
        error = "; ".join(result.warnings) if result.failed else None
        store.update_job(
            job_id,
            status=status,
            finished_at=now(),
            result_prefix=f"{prefix}/{spec.id}",
            error=error,
        )
        store.audit(
            "experiment.finish",
            user=job["submitted_by"],
            workspace=job["workspace"],
            target=job_id,
            spec_hash=job["cache_key"],
            outcome=status,
            detail={"failed_replications": len(result.failed)},
        )
    except ExperimentCancelled:
        reason = f"timed out after {settings.job_timeout_s:g} s" if timed_out.is_set() else None
        store.update_job(
            job_id, status="failed" if reason else "cancelled", finished_at=now(), error=reason
        )
        store.audit(
            "experiment.finish",
            user=job["submitted_by"],
            workspace=job["workspace"],
            target=job_id,
            outcome="timeout" if reason else "cancelled",
        )
    except (ConfigError, Exception) as exc:  # record, never crash the worker
        log.exception("job %s failed", job_id)
        store.update_job(job_id, status="failed", finished_at=now(), error=str(exc))
        store.audit(
            "experiment.finish",
            user=job["submitted_by"],
            workspace=job["workspace"],
            target=job_id,
            outcome="failed",
            detail={"error": str(exc)},
        )
    finally:
        timer.cancel()
        if tmp is not None:
            tmp.cleanup()
        ping()

"""Job model, submission, result cache, quotas and brokers (FR-API-02/03/05, FR-SCG-07).

Lifecycle: ``submitted -> queued -> running -> completed | failed | cancelled``.
Submissions are idempotent: an identical request (same cache key) returns the
running job, or a new job marked completed from the cached result without
re-running (FR-API-05). Brokers hand queued jobs to CPU (logical) or GPU
(kinematic/physics) worker pools:

* ``InProcessBroker`` (dev / tests): worker threads inside the API process.
* ``RedisBroker``: Redis sorted sets per pool; ``python -m services.workers``
  processes pop jobs and share the metadata store.
"""

from __future__ import annotations

import hashlib
import heapq
import hmac
import itertools
import json
import logging
import os
import secrets
import threading
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from services.platform.artifacts import ArtifactStore
from services.platform.bundles import bundle_from_json, bundle_to_json
from services.platform.estimator import Estimate, estimate
from services.platform.metadata import MetadataStore, now
from services.platform.settings import Settings
from simcore.core.config import ScenarioBundle
from simcore.core.hashing import content_hash
from simcore.core.plugins import PluginRegistry
from simcore.core.provenance import engine_version
from simcore.experiment.design import build_variants
from simcore.specs.experiment import ExperimentSpec

__all__ = [
    "TERMINAL",
    "Broker",
    "InProcessBroker",
    "JobService",
    "QuotaExceeded",
    "RedisBroker",
    "SubmissionRejected",
]

log = logging.getLogger(__name__)

TERMINAL = ("completed", "failed", "cancelled")
GPU_MODES = ("kinematic", "physics")


class SubmissionRejected(ValueError):
    """The request needs confirmation or is otherwise not acceptable."""


class QuotaExceeded(SubmissionRejected):
    pass


@dataclass(frozen=True)
class Prepared:
    spec: ExperimentSpec
    bundle: ScenarioBundle
    mode: str
    cache_key: str
    estimate: Estimate

    @property
    def pool(self) -> str:
        return "gpu" if self.mode in GPU_MODES else "cpu"


# ----------------------------------------------------------------------------- brokers


class Broker(ABC):
    @abstractmethod
    def start(self, run: Callable[[str, threading.Event], None]) -> None: ...

    @abstractmethod
    def enqueue(self, job_id: str, pool: str, priority: int) -> None: ...

    @abstractmethod
    def cancel(self, job_id: str) -> bool:
        """Signal a queued or running job to stop; True if it was known."""

    @abstractmethod
    def shutdown(self) -> None: ...


class InProcessBroker(Broker):
    def __init__(self, cpu_workers: int = 2, gpu_workers: int = 0) -> None:
        self.workers = {"cpu": cpu_workers, "gpu": gpu_workers}
        self._queues: dict[str, list[tuple[int, int, str]]] = {"cpu": [], "gpu": []}
        self._cv = threading.Condition()
        self._seq = itertools.count()
        self._cancel: dict[str, threading.Event] = {}
        self._threads: list[threading.Thread] = []
        self._stop = False

    def start(self, run: Callable[[str, threading.Event], None]) -> None:
        for pool, n in self.workers.items():
            for i in range(n):
                t = threading.Thread(
                    target=self._loop, args=(pool, run), daemon=True, name=f"simcore-{pool}-{i}"
                )
                t.start()
                self._threads.append(t)

    def _loop(self, pool: str, run: Callable[[str, threading.Event], None]) -> None:
        while True:
            with self._cv:
                while not self._queues[pool] and not self._stop:
                    self._cv.wait()
                if self._stop:
                    return
                _, _, job_id = heapq.heappop(self._queues[pool])
                event = self._cancel.setdefault(job_id, threading.Event())
            try:
                run(job_id, event)
            except Exception:  # pragma: no cover - run() records its own failures
                log.exception("job %s crashed the worker loop", job_id)

    def enqueue(self, job_id: str, pool: str, priority: int) -> None:
        if self.workers.get(pool, 0) <= 0:
            raise SubmissionRejected(
                f"no {pool.upper()} workers are configured; "
                f"set SIMCORE_{pool.upper()}_WORKERS or use mode 'logical'"
            )
        with self._cv:
            self._cancel.setdefault(job_id, threading.Event())
            heapq.heappush(self._queues[pool], (-priority, next(self._seq), job_id))
            self._cv.notify_all()

    def cancel(self, job_id: str) -> bool:
        with self._cv:
            for q in self._queues.values():
                for item in q:
                    if item[2] == job_id:
                        q.remove(item)
                        heapq.heapify(q)
                        self._cancel.setdefault(job_id, threading.Event()).set()
                        return True
            ev = self._cancel.get(job_id)
        if ev is not None:
            ev.set()
            return True
        return False

    def shutdown(self) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify_all()
        for t in self._threads:
            t.join(timeout=5)


class RedisBroker(Broker):
    """Jobs in Redis sorted sets ``<ns>:pool:<pool>``; cancellation flags ``<ns>:cancel:<id>``."""

    def __init__(self, url: str, namespace: str = "simcore:jobs", client: Any = None) -> None:
        if client is None:
            import redis

            client = redis.Redis.from_url(url, decode_responses=True)
        self.r = client
        self.ns = namespace

    def start(self, run: Callable[[str, threading.Event], None]) -> None:
        """The API process does not execute jobs with this broker (workers do)."""

    def enqueue(self, job_id: str, pool: str, priority: int) -> None:
        seq = int(self.r.incr(f"{self.ns}:seq"))
        self.r.zadd(f"{self.ns}:pool:{pool}", {job_id: -priority * 1e12 + seq})

    def pop(self, pool: str, timeout: float = 1.0) -> str | None:
        item = self.r.bzpopmin(f"{self.ns}:pool:{pool}", timeout=timeout)
        return None if item is None else str(item[1])

    def cancel(self, job_id: str) -> bool:
        for pool in ("cpu", "gpu"):
            self.r.zrem(f"{self.ns}:pool:{pool}", job_id)
        self.r.set(f"{self.ns}:cancel:{job_id}", "1", ex=86400)  # running workers poll this
        return True

    def cancelled(self, job_id: str) -> bool:
        return bool(self.r.exists(f"{self.ns}:cancel:{job_id}"))

    def shutdown(self) -> None:
        return None


# ----------------------------------------------------------------------------- service


class JobService:
    def __init__(
        self,
        settings: Settings,
        store: MetadataStore,
        artifacts: ArtifactStore,
        plugins: PluginRegistry,
        broker: Broker,
    ) -> None:
        self.settings, self.store, self.artifacts = settings, store, artifacts
        self.plugins, self.broker = plugins, broker
        self._secret = (os.environ.get("SIMCORE_SECRET") or secrets.token_hex(32)).encode()
        self.listeners: list[Callable[[str], None]] = []

    # ------------------------------------------------------------------ preparing

    def prepare(
        self, spec: ExperimentSpec, bundle: ScenarioBundle, mode: str = "logical"
    ) -> Prepared:
        """Validate, expand variants and compute the cache key and estimate."""
        plugin = self.plugins.get(bundle.scenario.domain)
        variants = build_variants(spec, bundle)
        for v in variants:
            assert v.bundle is not None
            plugin.validate_scenario(v.bundle)
        result_relevant = spec.model_dump(
            mode="json",
            exclude={"id", "name", "description", "parallelism", "timeout_s", "base_scenario"},
        )
        key = content_hash(
            {
                "spec": result_relevant,
                "variants": [(v.id, v.bundle.config_hash) for v in variants if v.bundle],
                "engine_version": engine_version(),
                "domain": [plugin.name, plugin.version],
                "mode": mode,
            },
            length=32,
        )
        reps = spec.adaptive.max_replications if spec.adaptive else spec.replications
        duration = variants[0].bundle.scenario.duration if variants[0].bundle else 0.0
        est = estimate(
            self.store,
            plugin.name,
            mode,
            len(variants),
            reps,
            duration,
            self.settings.replication_parallelism,
        )
        return Prepared(spec, bundle, mode, key, est)

    def needs_confirmation(self, p: Prepared) -> bool:
        s = self.settings
        return (
            p.estimate.variants > s.auto_approve_variants
            or p.estimate.replications > s.auto_approve_replications
            or p.estimate.minutes > s.auto_approve_minutes
        )

    def confirmation_token(self, p: Prepared, workspace: str) -> str:
        msg = f"{workspace}:{p.cache_key}".encode()
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()[:32]

    # ------------------------------------------------------------------ submitting

    def submit(
        self,
        p: Prepared,
        *,
        workspace: str,
        user: str | None,
        confirmation_token: str | None = None,
        priority: int = 0,
    ) -> dict[str, Any]:
        """Idempotent submission; returns the job record (FR-API-02/05, FR-SCG-07)."""
        existing = self.store.active_job(p.cache_key, workspace)
        if existing is not None:
            return existing
        cached = self.store.cached_job(p.cache_key)
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        base = {
            "id": job_id,
            "workspace": workspace,
            "kind": "experiment",
            "priority": priority,
            "pool": p.pool,
            "cache_key": p.cache_key,
            "created_at": now(),
            "submitted_by": user,
            "estimate_json": json.dumps(p.estimate.to_dict()),
            "spec_json": json.dumps(
                {
                    "experiment": p.spec.model_dump(mode="json"),
                    "bundle": bundle_to_json(p.bundle),
                    "mode": p.mode,
                },
                sort_keys=True,
            ),
        }
        if cached is not None:  # served from cache, nothing re-runs
            self.store.insert_job(
                {
                    **base,
                    "status": "completed",
                    "started_at": now(),
                    "finished_at": now(),
                    "progress_completed": cached["progress_completed"],
                    "progress_planned": cached["progress_planned"],
                    "result_prefix": cached["result_prefix"],
                    "cached_from": cached["id"],
                }
            )
            self.store.audit(
                "experiment.submit",
                user=user,
                workspace=workspace,
                target=job_id,
                spec_hash=p.cache_key,
                outcome="cache_hit",
            )
            return self.store.job(job_id) or {}
        if self.needs_confirmation(p) and not hmac.compare_digest(
            confirmation_token or "", self.confirmation_token(p, workspace)
        ):
            raise SubmissionRejected(
                f"this experiment needs confirmation ({p.estimate.variants} variants x "
                f"{p.estimate.replications} replications, ~{p.estimate.minutes:.1f} min, "
                f"{p.mode}); call /experiments/estimate and resubmit with its confirmation_token"
            )
        if self.store.count_active(workspace) >= self.settings.max_jobs_per_workspace:
            raise QuotaExceeded(
                f"workspace {workspace!r} already has "
                f"{self.settings.max_jobs_per_workspace} active jobs; wait or cancel one"
            )
        self.store.insert_job({**base, "status": "submitted", "progress_planned": p.estimate.runs})
        self.store.audit(
            "experiment.submit",
            user=user,
            workspace=workspace,
            target=job_id,
            spec_hash=p.cache_key,
            outcome="queued",
        )
        try:
            self.broker.enqueue(job_id, p.pool, priority)
        except SubmissionRejected as exc:
            self.store.update_job(job_id, status="failed", error=str(exc), finished_at=now())
            raise
        self.store.update_job(job_id, status="queued")
        self._notify(job_id)
        return self.store.job(job_id) or {}

    def cancel(self, job_id: str, user: str | None = None) -> dict[str, Any]:
        job = self.store.job(job_id)
        if job is None:
            raise KeyError(job_id)
        if job["status"] not in TERMINAL:
            self.broker.cancel(job_id)
            if job["status"] in ("submitted", "queued"):
                self.store.update_job(job_id, status="cancelled", finished_at=now())
            self.store.audit("job.cancel", user=user, workspace=job["workspace"], target=job_id)
        self._notify(job_id)
        return self.store.job(job_id) or {}

    def _notify(self, job_id: str) -> None:
        for fn in list(self.listeners):
            fn(job_id)

    def run_job(self, job_id: str, cancel: threading.Event) -> None:
        from services.workers.runner import execute_job

        execute_job(
            job_id,
            self.store,
            self.artifacts,
            self.plugins,
            self.settings,
            cancel,
            notify=self._notify,
        )

    @staticmethod
    def load(job: dict[str, Any]) -> tuple[ExperimentSpec, ScenarioBundle, str]:
        payload = json.loads(job["spec_json"])
        return (
            ExperimentSpec.model_validate(payload["experiment"]),
            bundle_from_json(payload["bundle"]),
            str(payload["mode"]),
        )

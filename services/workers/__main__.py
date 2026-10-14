"""Standalone worker: ``python -m services.workers --pool cpu`` (Redis broker).

Pops jobs for one pool, runs them, and polls the cancellation flag. GPU workers
run the same command with ``--pool gpu`` inside the Isaac Sim image.
"""

from __future__ import annotations

import argparse
import logging
import threading

from services.platform.artifacts import open_artifact_store
from services.platform.jobs import RedisBroker
from services.platform.metadata import MetadataStore
from services.platform.settings import Settings
from services.workers.runner import execute_job
from simcore.api import default_plugins


def serve(pool: str, settings: Settings, once: bool = False) -> int:
    if not settings.redis_url:
        raise SystemExit("SIMCORE_REDIS_URL is required for standalone workers")
    broker = RedisBroker(settings.redis_url)
    store = MetadataStore(settings.metadata)
    artifacts = open_artifact_store(settings.artifacts)
    plugins = default_plugins()
    done = 0
    while True:
        job_id = broker.pop(pool, timeout=1.0)
        if job_id is None:
            if once:
                return done
            continue
        cancel = threading.Event()
        stop = threading.Event()

        def watch(
            job: str = job_id, ev: threading.Event = cancel, halt: threading.Event = stop
        ) -> None:
            while not halt.wait(0.5):
                if broker.cancelled(job):
                    ev.set()
                    return

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        try:
            execute_job(job_id, store, artifacts, plugins, settings, cancel)
        finally:
            stop.set()
        done += 1
        if once:
            return done


def main() -> None:  # pragma: no cover - CLI
    parser = argparse.ArgumentParser(description="simcore experiment worker")
    parser.add_argument("--pool", choices=["cpu", "gpu"], default="cpu")
    parser.add_argument("--once", action="store_true", help="process one job then exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    serve(args.pool, Settings(), once=args.once)


if __name__ == "__main__":  # pragma: no cover
    main()

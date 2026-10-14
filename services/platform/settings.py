"""Service settings from environment variables (NFR-09: secrets only via env)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["Settings"]


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.lower() not in ("0", "false", "no", "off")


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(_env("SIMCORE_DATA", "./.simcore") or "."))
    metadata_url: str | None = field(default_factory=lambda: _env("SIMCORE_METADATA_URL"))
    artifact_url: str | None = field(default_factory=lambda: _env("SIMCORE_ARTIFACTS_URL"))
    broker: str = field(default_factory=lambda: _env("SIMCORE_BROKER", "inprocess") or "")
    redis_url: str | None = field(default_factory=lambda: _env("SIMCORE_REDIS_URL"))
    auth_enabled: bool = field(default_factory=lambda: _bool("SIMCORE_AUTH", False))
    cpu_workers: int = field(default_factory=lambda: _int("SIMCORE_CPU_WORKERS", 2))
    gpu_workers: int = field(default_factory=lambda: _int("SIMCORE_GPU_WORKERS", 0))
    replication_parallelism: int = field(default_factory=lambda: _int("SIMCORE_REP_PARALLELISM", 1))
    max_jobs_per_workspace: int = field(
        default_factory=lambda: _int("SIMCORE_MAX_JOBS_PER_WORKSPACE", 4)
    )
    job_timeout_s: float = field(
        default_factory=lambda: float(_env("SIMCORE_JOB_TIMEOUT_S", "3600") or 3600)
    )
    # BRD Q13: auto-approve <= 10 variants, <= 10 replications each, <= 5 est. minutes
    auto_approve_variants: int = 10
    auto_approve_replications: int = 10
    auto_approve_minutes: float = 5.0
    rate_limit_per_minute: int = field(
        default_factory=lambda: int(os.environ.get("SIMCORE_RATE_LIMIT", "600"))
    )
    sql_enabled: bool = field(default_factory=lambda: _bool("SIMCORE_SQL_ENABLED", False))
    examples_dir: Path | None = field(
        default_factory=lambda: Path(p) if (p := os.environ.get("SIMCORE_EXAMPLES")) else None
    )

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    @property
    def metadata(self) -> str:
        return self.metadata_url or f"sqlite:///{self.data_dir / 'metadata.sqlite'}"

    @property
    def artifacts(self) -> str:
        return self.artifact_url or f"file://{(self.data_dir / 'artifacts').resolve()}"

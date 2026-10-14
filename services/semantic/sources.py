"""Load an experiment's outputs into an in-memory DuckDB database.

Tables (all read eagerly so queries never touch the filesystem afterwards):

* ``results(variant, replication, master_seed, metric, value, config_hash, ...)``
* ``runs(variant, replication, master_seed, status, config_hash, ...)``
* ``events(variant, replication, seq, time, type, agent, task, location, data)`` (lazy)
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb

__all__ = ["ExperimentData"]


@dataclass
class ExperimentData:
    root: Path  # .../<experiment_id>/
    con: Any = None
    _events_loaded: bool = False
    summary: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not (self.root / "results.parquet").exists():
            raise FileNotFoundError(f"no experiment results under {self.root}")
        self.con = duckdb.connect(":memory:")
        for table in ("results", "runs"):
            path = (self.root / f"{table}.parquet").as_posix()
            self.con.execute(f"CREATE TABLE {table} AS SELECT * FROM read_parquet(?)", [path])
        self.summary = json.loads((self.root / "summary.json").read_text())

    @property
    def digest(self) -> str:
        h = hashlib.sha256()
        for name in ("results.parquet", "runs.parquet"):
            h.update((self.root / name).read_bytes())
        return h.hexdigest()[:16]

    def ensure_events(self) -> None:
        if self._events_loaded:
            return
        files = sorted(self.root.glob("*/rep_*/events.parquet"))
        if not files:
            raise FileNotFoundError(f"no event logs under {self.root}")
        parts = []
        for f in files:
            variant, rep = f.parent.parent.name, int(f.parent.name.split("_")[1])
            parts.append((variant, rep, f.as_posix()))
        self.con.execute(
            "CREATE TABLE events (variant VARCHAR, replication INTEGER, seq BIGINT, "
            "time DOUBLE, type VARCHAR, agent VARCHAR, task VARCHAR, "
            "location VARCHAR, data VARCHAR)"
        )
        for variant, rep, path in parts:
            self.con.execute(
                "INSERT INTO events SELECT ?, ?, seq, time, type, agent, task, location, data "
                "FROM read_parquet(?)",
                [variant, rep, path],
            )
        self._events_loaded = True

    def lock_down(self) -> None:
        """Forbid file/network access from SQL (advanced read-only mode, FR-SEM-03)."""
        self.ensure_events()
        self.con.execute("SET enable_external_access = false")
        self.con.execute("SET lock_configuration = true")

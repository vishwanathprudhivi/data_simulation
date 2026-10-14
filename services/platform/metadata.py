"""Metadata store: SQLite by default, Postgres adapter (FR-API-04/07/09, FR-SCG-08, FR-DOM-12).

A thin DB-API layer: the same SQL runs on both engines (``?`` placeholders are
translated for psycopg). All access is serialized by a lock so worker threads can
share one connection.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

__all__ = ["MetadataStore", "now"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, password_hash TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tokens (
  token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS workspaces (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS memberships (
  user_id TEXT NOT NULL, workspace_id TEXT NOT NULL, role TEXT NOT NULL,
  PRIMARY KEY (user_id, workspace_id));
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, workspace TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
  priority INTEGER NOT NULL, pool TEXT NOT NULL, spec_json TEXT NOT NULL,
  cache_key TEXT NOT NULL, created_at REAL NOT NULL, started_at REAL, finished_at REAL,
  progress_completed INTEGER NOT NULL DEFAULT 0, progress_planned INTEGER NOT NULL DEFAULT 0,
  error TEXT, result_prefix TEXT, estimate_json TEXT, submitted_by TEXT,
  cached_from TEXT);
CREATE INDEX IF NOT EXISTS jobs_cache ON jobs (cache_key, status);
CREATE TABLE IF NOT EXISTS scenarios (
  workspace TEXT NOT NULL, id TEXT NOT NULL, version INTEGER NOT NULL, name TEXT NOT NULL,
  tags_json TEXT NOT NULL, domain TEXT NOT NULL, domain_version TEXT, bundle_json TEXT NOT NULL,
  parent_id TEXT, parent_version INTEGER, pinned INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL, created_by TEXT, PRIMARY KEY (workspace, id, version));
CREATE TABLE IF NOT EXISTS analyses (
  id TEXT PRIMARY KEY, workspace TEXT NOT NULL, name TEXT NOT NULL, payload_json TEXT NOT NULL,
  created_at REAL NOT NULL, updated_at REAL NOT NULL, created_by TEXT);
CREATE TABLE IF NOT EXISTS chat_sessions (
  id TEXT PRIMARY KEY, workspace TEXT NOT NULL, user_id TEXT, payload_json TEXT NOT NULL,
  created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, user_id TEXT, workspace TEXT,
  action TEXT NOT NULL, target TEXT, spec_hash TEXT, outcome TEXT, detail_json TEXT);
CREATE TABLE IF NOT EXISTS benchmarks (
  domain TEXT NOT NULL, mode TEXT NOT NULL, sim_seconds REAL NOT NULL,
  wall_seconds REAL NOT NULL, agents INTEGER NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS capability_gaps (
  id TEXT PRIMARY KEY, workspace TEXT NOT NULL, domain TEXT NOT NULL, request TEXT NOT NULL,
  detail_json TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL);
"""

JOB_COLUMNS = (
    "id",
    "workspace",
    "kind",
    "status",
    "priority",
    "pool",
    "spec_json",
    "cache_key",
    "created_at",
    "started_at",
    "finished_at",
    "progress_completed",
    "progress_planned",
    "error",
    "result_prefix",
    "estimate_json",
    "submitted_by",
    "cached_from",
)


def now() -> float:
    """Wall-clock timestamp for records (service layer only, never simulation logic)."""
    return time.time()


class MetadataStore:
    def __init__(self, url: str) -> None:
        self.url = url
        self._lock = threading.RLock()
        if url.startswith("sqlite:///"):
            path = url[len("sqlite:///") :]
            if path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._conn: Any = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._pg = False
        elif url.startswith(("postgresql://", "postgres://")):  # pragma: no cover
            import psycopg

            self._conn = psycopg.connect(url, autocommit=True)
            self._pg = True
        else:
            raise ValueError(f"unsupported metadata store {url!r}; use sqlite:/// or postgresql://")
        with self._lock:
            for stmt in SCHEMA.split(";"):
                if stmt.strip():
                    sql = stmt
                    if self._pg:  # pragma: no cover
                        sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
                    self._conn.execute(sql)

    # ------------------------------------------------------------------ plumbing

    def _sql(self, query: str) -> str:
        return query.replace("?", "%s") if self._pg else query

    def execute(self, query: str, params: Sequence[Any] = ()) -> None:
        with self._lock:
            self._conn.execute(self._sql(query), tuple(params))

    def query(self, query: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(self._sql(query), tuple(params))
            cols = [c[0] for c in cur.description] if cur.description else []
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def one(self, query: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        rows = self.query(query, params)
        return rows[0] if rows else None

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ users / workspaces

    def create_user(self, user_id: str, password_hash: str) -> None:
        self.execute(
            "INSERT INTO users (id, password_hash, created_at) VALUES (?, ?, ?)",
            (user_id, password_hash, now()),
        )

    def user(self, user_id: str) -> dict[str, Any] | None:
        return self.one("SELECT * FROM users WHERE id = ?", (user_id,))

    def add_token(self, token_hash: str, user_id: str) -> None:
        self.execute(
            "INSERT INTO tokens (token_hash, user_id, created_at) VALUES (?, ?, ?)",
            (token_hash, user_id, now()),
        )

    def token_user(self, token_hash: str) -> str | None:
        row = self.one("SELECT user_id FROM tokens WHERE token_hash = ?", (token_hash,))
        return None if row is None else str(row["user_id"])

    def ensure_workspace(self, workspace_id: str, name: str | None = None) -> None:
        if self.one("SELECT id FROM workspaces WHERE id = ?", (workspace_id,)) is None:
            self.execute(
                "INSERT INTO workspaces (id, name, created_at) VALUES (?, ?, ?)",
                (workspace_id, name or workspace_id, now()),
            )

    def add_member(self, user_id: str, workspace_id: str, role: str = "member") -> None:
        self.ensure_workspace(workspace_id)
        if (
            self.one(
                "SELECT 1 AS x FROM memberships WHERE user_id = ? AND workspace_id = ?",
                (user_id, workspace_id),
            )
            is None
        ):
            self.execute(
                "INSERT INTO memberships (user_id, workspace_id, role) VALUES (?, ?, ?)",
                (user_id, workspace_id, role),
            )

    def workspaces_of(self, user_id: str) -> list[str]:
        rows = self.query(
            "SELECT workspace_id FROM memberships WHERE user_id = ? ORDER BY workspace_id",
            (user_id,),
        )
        return [str(r["workspace_id"]) for r in rows]

    # ------------------------------------------------------------------ jobs

    def insert_job(self, job: dict[str, Any]) -> None:
        cols = [c for c in JOB_COLUMNS if c in job]
        self.execute(
            f"INSERT INTO jobs ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
            [job[c] for c in cols],
        )

    def update_job(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        bad = set(fields) - set(JOB_COLUMNS)
        if bad:
            raise KeyError(f"unknown job fields {sorted(bad)}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE jobs SET {sets} WHERE id = ?", [*fields.values(), job_id])

    def job(self, job_id: str) -> dict[str, Any] | None:
        return self.one("SELECT * FROM jobs WHERE id = ?", (job_id,))

    def jobs(
        self, workspace: str | None = None, status: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        where, params = [], []
        if workspace is not None:
            where.append("workspace = ?")
            params.append(workspace)
        if status is not None:
            where.append("status = ?")
            params.append(status)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        return self.query(
            f"SELECT * FROM jobs {clause} ORDER BY created_at DESC, id LIMIT ?", [*params, limit]
        )

    def cached_job(self, cache_key: str) -> dict[str, Any] | None:
        return self.one(
            "SELECT * FROM jobs WHERE cache_key = ? AND status = 'completed' "
            "AND cached_from IS NULL ORDER BY finished_at LIMIT 1",
            (cache_key,),
        )

    def active_job(self, cache_key: str, workspace: str) -> dict[str, Any] | None:
        return self.one(
            "SELECT * FROM jobs WHERE cache_key = ? AND workspace = ? AND status IN "
            "('submitted', 'queued', 'running') ORDER BY created_at LIMIT 1",
            (cache_key, workspace),
        )

    def count_active(self, workspace: str) -> int:
        row = self.one(
            "SELECT COUNT(*) AS n FROM jobs WHERE workspace = ? AND status IN "
            "('submitted', 'queued', 'running')",
            (workspace,),
        )
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------------ scenario library

    def save_scenario(
        self,
        workspace: str,
        scenario_id: str,
        name: str,
        domain: str,
        domain_version: str | None,
        bundle: dict[str, Any],
        tags: list[str],
        parent: tuple[str, int] | None = None,
        pinned: bool = False,
        user: str | None = None,
    ) -> int:
        with self.transaction():
            row = self.one(
                "SELECT MAX(version) AS v FROM scenarios WHERE workspace = ? AND id = ?",
                (workspace, scenario_id),
            )
            version = int(row["v"]) + 1 if row and row["v"] is not None else 1
            if pinned:  # one pinned baseline per domain and workspace
                self.execute(
                    "UPDATE scenarios SET pinned = 0 WHERE workspace = ? AND domain = ?",
                    (workspace, domain),
                )
            self.execute(
                "INSERT INTO scenarios (workspace, id, version, name, tags_json, domain, "
                "domain_version, bundle_json, parent_id, parent_version, pinned, created_at, "
                "created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    workspace,
                    scenario_id,
                    version,
                    name,
                    json.dumps(sorted(set(tags))),
                    domain,
                    domain_version,
                    json.dumps(bundle, sort_keys=True),
                    parent[0] if parent else None,
                    parent[1] if parent else None,
                    int(pinned),
                    now(),
                    user,
                ),
            )
        return version

    def scenario(
        self, workspace: str, scenario_id: str, version: int | None = None
    ) -> dict[str, Any] | None:
        if version is None:
            return self.one(
                "SELECT * FROM scenarios WHERE workspace = ? AND id = ? "
                "ORDER BY version DESC LIMIT 1",
                (workspace, scenario_id),
            )
        return self.one(
            "SELECT * FROM scenarios WHERE workspace = ? AND id = ? AND version = ?",
            (workspace, scenario_id, version),
        )

    def scenarios(self, workspace: str, domain: str | None = None) -> list[dict[str, Any]]:
        q = (
            "SELECT s.* FROM scenarios s JOIN (SELECT id, MAX(version) AS v FROM scenarios "
            "WHERE workspace = ? GROUP BY id) m ON s.id = m.id AND s.version = m.v "
            "WHERE s.workspace = ?"
        )
        params: list[Any] = [workspace, workspace]
        if domain is not None:
            q += " AND s.domain = ?"
            params.append(domain)
        return self.query(q + " ORDER BY s.id", params)

    def pinned_baseline(self, workspace: str, domain: str) -> dict[str, Any] | None:
        return self.one(
            "SELECT * FROM scenarios WHERE workspace = ? AND domain = ? AND "
            "pinned = 1 ORDER BY version DESC LIMIT 1",
            (workspace, domain),
        )

    # ------------------------------------------------------------------ analyses / chat

    def save_analysis(
        self,
        workspace: str,
        name: str,
        payload: dict[str, Any],
        analysis_id: str | None = None,
        user: str | None = None,
    ) -> str:
        analysis_id = analysis_id or f"an_{uuid.uuid4().hex[:10]}"
        t = now()
        if self.one("SELECT id FROM analyses WHERE id = ?", (analysis_id,)):
            self.execute(
                "UPDATE analyses SET name = ?, payload_json = ?, updated_at = ? "
                "WHERE id = ? AND workspace = ?",
                (name, json.dumps(payload, sort_keys=True), t, analysis_id, workspace),
            )
        else:
            self.execute(
                "INSERT INTO analyses (id, workspace, name, payload_json, created_at, "
                "updated_at, created_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (analysis_id, workspace, name, json.dumps(payload, sort_keys=True), t, t, user),
            )
        return analysis_id

    def analysis(self, workspace: str, analysis_id: str) -> dict[str, Any] | None:
        return self.one(
            "SELECT * FROM analyses WHERE id = ? AND workspace = ?", (analysis_id, workspace)
        )

    def analyses(self, workspace: str) -> list[dict[str, Any]]:
        return self.query(
            "SELECT id, name, created_at, updated_at, created_by FROM analyses "
            "WHERE workspace = ? ORDER BY updated_at DESC",
            (workspace,),
        )

    def save_chat(
        self, session_id: str, workspace: str, user: str | None, payload: dict[str, Any]
    ) -> None:
        t = now()
        if self.one("SELECT id FROM chat_sessions WHERE id = ?", (session_id,)):
            self.execute(
                "UPDATE chat_sessions SET payload_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(payload, sort_keys=True, default=str), t, session_id),
            )
        else:
            self.execute(
                "INSERT INTO chat_sessions (id, workspace, user_id, payload_json, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    workspace,
                    user,
                    json.dumps(payload, sort_keys=True, default=str),
                    t,
                    t,
                ),
            )

    def chat(self, session_id: str, workspace: str) -> dict[str, Any] | None:
        return self.one(
            "SELECT * FROM chat_sessions WHERE id = ? AND workspace = ?", (session_id, workspace)
        )

    # ------------------------------------------------------------------ audit / benchmarks / gaps

    def audit(
        self,
        action: str,
        *,
        user: str | None = None,
        workspace: str | None = None,
        target: str | None = None,
        spec_hash: str | None = None,
        outcome: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.execute(
            "INSERT INTO audit (ts, user_id, workspace, action, target, spec_hash, "
            "outcome, detail_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                now(),
                user,
                workspace,
                action,
                target,
                spec_hash,
                outcome,
                json.dumps(detail or {}, sort_keys=True, default=str),
            ),
        )

    def audit_log(self, workspace: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        if workspace is None:
            return self.query("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
        return self.query(
            "SELECT * FROM audit WHERE workspace = ? ORDER BY id DESC LIMIT ?", (workspace, limit)
        )

    def add_benchmark(
        self, domain: str, mode: str, sim_seconds: float, wall_seconds: float, agents: int
    ) -> None:
        self.execute(
            "INSERT INTO benchmarks (domain, mode, sim_seconds, wall_seconds, agents, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (domain, mode, sim_seconds, wall_seconds, agents, now()),
        )

    def benchmarks(self, domain: str, mode: str, limit: int = 50) -> list[dict[str, Any]]:
        return self.query(
            "SELECT * FROM benchmarks WHERE domain = ? AND mode = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (domain, mode, limit),
        )

    def add_gap(self, workspace: str, domain: str, request: str, detail: dict[str, Any]) -> str:
        gap_id = f"gap_{uuid.uuid4().hex[:10]}"
        self.execute(
            "INSERT INTO capability_gaps (id, workspace, domain, request, detail_json, "
            "status, created_at) VALUES (?, ?, ?, ?, ?, 'open', ?)",
            (gap_id, workspace, domain, request, json.dumps(detail, sort_keys=True), now()),
        )
        return gap_id

    def gaps(self, domain: str | None = None) -> list[dict[str, Any]]:
        if domain is None:
            return self.query("SELECT * FROM capability_gaps ORDER BY created_at DESC")
        return self.query(
            "SELECT * FROM capability_gaps WHERE domain = ? ORDER BY created_at DESC", (domain,)
        )

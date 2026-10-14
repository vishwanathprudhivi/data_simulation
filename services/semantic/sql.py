"""Advanced mode: read-only SQL under an allowlist with row and time limits (FR-SEM-03).

Disabled unless ``SIMCORE_SQL_ENABLED=1``. The database is in-memory with external
access disabled and the configuration locked, so a query can read only the
``results``, ``runs`` and ``events`` tables of one experiment.
"""

from __future__ import annotations

import re
import threading
from typing import Any

from services.semantic.sources import ExperimentData

__all__ = ["SqlRejected", "run_readonly_sql"]

_DENY = re.compile(
    r"\b(attach|detach|copy|install|load|pragma|set|reset|create|insert|update|delete|drop|"
    r"alter|export|import|call|checkpoint|vacuum|read_csv\w*|read_parquet|read_json\w*|"
    r"read_text|read_blob|glob|httpfs|getenv)\b",
    re.IGNORECASE,
)


class SqlRejected(ValueError):
    pass


def run_readonly_sql(
    data: ExperimentData, sql: str, max_rows: int = 5000, timeout_s: float = 10.0
) -> dict[str, Any]:
    text = sql.strip().rstrip(";").strip()
    if ";" in text:
        raise SqlRejected("only a single statement is allowed")
    if not re.match(r"^(select|with)\b", text, re.IGNORECASE):
        raise SqlRejected("only SELECT / WITH queries are allowed")
    bad = _DENY.search(text)
    if bad:
        raise SqlRejected(f"keyword or function {bad.group(0)!r} is not allowed")
    data.lock_down()
    timer = threading.Timer(timeout_s, data.con.interrupt)
    timer.start()
    try:
        cur = data.con.execute(f"SELECT * FROM ({text}) AS q LIMIT {int(max_rows) + 1}")
        cols = [c[0] for c in cur.description]
        rows = cur.fetchall()
    except Exception as exc:
        if "interrupt" in str(exc).lower():
            raise SqlRejected(f"query exceeded {timeout_s:g} s") from exc
        raise SqlRejected(str(exc)) from exc
    finally:
        timer.cancel()
    truncated = len(rows) > max_rows
    return {
        "columns": cols,
        "rows": [dict(zip(cols, r)) for r in rows[:max_rows]],
        "truncated": truncated,
    }

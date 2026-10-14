"""Stable, content-based hashing (FR-FAC-07, FR-EXP-09, FR-API-05).

Never use Python's built-in ``hash()`` for anything that affects outcomes or
identity: it is salted per process.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

__all__ = ["canonical_json", "content_hash", "stable_int"]


def canonical_json(obj: Any) -> str:
    """Serialize ``obj`` to JSON with sorted keys and no insignificant whitespace."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def content_hash(obj: Any, length: int = 16) -> str:
    """Return a hex SHA-256 of the canonical JSON of ``obj`` truncated to ``length`` chars."""
    digest = hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()
    return digest[:length]


def stable_int(text: str, bits: int = 64) -> int:
    """Map ``text`` to a process-independent non-negative integer."""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return int.from_bytes(digest[: bits // 8], "big")

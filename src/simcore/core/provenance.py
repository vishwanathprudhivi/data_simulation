"""Run provenance: versions, git SHA, host info (FR-OBS-06, FR-EXP-09).

Values here describe the environment; none of them feed simulation logic.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from functools import lru_cache
from importlib import metadata
from pathlib import Path
from typing import Any

__all__ = ["engine_version", "git_sha", "host_info", "library_versions"]


def engine_version() -> str:
    from simcore import __version__

    return __version__


@lru_cache(maxsize=1)
def git_sha() -> str | None:
    """Commit of the simcore source tree, or ``None`` outside a git checkout."""
    if "SIMCORE_GIT_SHA" in os.environ:
        return os.environ["SIMCORE_GIT_SHA"]
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def library_versions() -> dict[str, str]:
    """Versions of libraries whose behavior can change outputs."""
    out = {"python": platform.python_version()}
    for name in ("numpy", "scipy", "pyarrow", "pydantic"):
        try:
            out[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            out[name] = "missing"
    return out


def host_info() -> dict[str, Any]:
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "python_executable": sys.executable,
    }

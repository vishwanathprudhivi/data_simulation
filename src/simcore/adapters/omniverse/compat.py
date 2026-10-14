"""Isaac Sim compatibility shim (ADR-001). All version-specific imports go here."""

from __future__ import annotations

import importlib.util

#: Pinned target version; see docs/decisions/ADR-001.md.
ISAAC_SIM_VERSION = "5.0.0"


def isaac_available() -> bool:
    return importlib.util.find_spec("isaacsim") is not None


def require_isaac() -> None:
    if not isaac_available():
        raise ImportError(
            f"Isaac Sim {ISAAC_SIM_VERSION} is not installed in this Python environment; "
            "kinematic/physics modes need it (logical mode does not)"
        )

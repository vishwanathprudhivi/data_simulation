"""Omniverse / Isaac Sim backend (BRD 7.4). The ONLY package allowed to import
``omni.*``, ``isaacsim.*`` or ``pxr`` (AC-ENG-04); those imports live in
:mod:`simcore.adapters.omniverse.compat` and run only when a stage is created.
"""

from __future__ import annotations

import os

from simcore.core.backend import WorldBackend

__all__ = ["create_backend"]


def create_backend(mode: str) -> WorldBackend:
    """Kinematic/physics backend on a headless Isaac Sim stage.

    Environment: ``SIMCORE_ISAAC_HEADLESS`` (default 1), ``SIMCORE_EXPORT_USD``
    (write the final stage to this path on teardown).
    """
    from simcore.adapters.omniverse.backend import KinematicBackend
    from simcore.adapters.omniverse.compat import IsaacStage, require_isaac

    require_isaac()
    headless = os.environ.get("SIMCORE_ISAAC_HEADLESS", "1") != "0"
    return KinematicBackend(
        lambda: IsaacStage(headless=headless, physics=mode == "physics"),
        mode=mode,
        export_usd=os.environ.get("SIMCORE_EXPORT_USD"),
    )

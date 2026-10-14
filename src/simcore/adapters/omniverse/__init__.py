"""Omniverse / Isaac Sim backend (BRD 7.4). The ONLY package allowed to import
``omni.*``, ``isaacsim.*`` or ``pxr`` (AC-ENG-04). Implemented in M4.

Version-specific calls are confined to :mod:`simcore.adapters.omniverse.compat`.
"""

from __future__ import annotations

from simcore.core.backend import WorldBackend

__all__ = ["create_backend"]


def create_backend(mode: str) -> WorldBackend:
    from simcore.adapters.omniverse import compat

    compat.require_isaac()
    raise NotImplementedError(f"the {mode!r} Isaac Sim backend arrives in milestone M4")

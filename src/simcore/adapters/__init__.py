"""World backends (FR-ENG-09). ``get_backend`` maps a fidelity mode to an implementation."""

from __future__ import annotations

from simcore.core.backend import WorldBackend

__all__ = ["BackendUnavailableError", "get_backend"]

MODES = ("logical", "kinematic", "physics")


class BackendUnavailableError(RuntimeError):
    pass


def get_backend(mode: str) -> WorldBackend:
    """Instantiate the backend for ``mode``. Isaac Sim is imported only for non-logical modes."""
    if mode == "logical":
        from simcore.adapters.logical import LogicalBackend

        return LogicalBackend()
    if mode in ("kinematic", "physics"):
        try:
            from simcore.adapters.omniverse import create_backend
        except ImportError as exc:  # pragma: no cover - depends on Isaac install
            raise BackendUnavailableError(
                f"mode {mode!r} needs Isaac Sim (see docs/decisions/ADR-001.md): {exc}"
            ) from exc
        return create_backend(mode)
    raise ValueError(f"unknown mode {mode!r}; choose one of {MODES}")

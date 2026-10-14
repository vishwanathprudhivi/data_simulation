"""Centralized RNG manager with named, independent streams (FR-ENG-06, FR-EXP-01/02).

Each stream is seeded from ``(master_seed, replication_id, stream_name)`` using
NumPy's ``SeedSequence``; streams are therefore statistically independent and
adding draws on one stream never perturbs another (AC-EXP-02). Using the same
``(master_seed, replication_id)`` for two alternatives gives common random
numbers (CRN) for free.
"""

from __future__ import annotations

import numpy as np

from simcore.core.hashing import stable_int

__all__ = ["RNGManager", "derive_seed_sequence"]


def derive_seed_sequence(
    master_seed: int, replication_id: int, stream_name: str
) -> np.random.SeedSequence:
    """Return the ``SeedSequence`` for one named stream of one replication."""
    if master_seed < 0 or replication_id < 0:
        raise ValueError("master_seed and replication_id must be non-negative")
    return np.random.SeedSequence(
        entropy=master_seed, spawn_key=(replication_id, stable_int(stream_name))
    )


class RNGManager:
    """Hands out one ``numpy.random.Generator`` per stream name.

    Streams are created lazily; the order in which streams are first requested
    does not affect their draws.
    """

    def __init__(self, master_seed: int, replication_id: int = 0) -> None:
        self.master_seed = master_seed
        self.replication_id = replication_id
        self._streams: dict[str, np.random.Generator] = {}

    def stream(self, name: str) -> np.random.Generator:
        """Return the generator for stream ``name`` (created on first use)."""
        gen = self._streams.get(name)
        if gen is None:
            seq = derive_seed_sequence(self.master_seed, self.replication_id, name)
            gen = np.random.Generator(np.random.PCG64(seq))
            self._streams[name] = gen
        return gen

    @property
    def stream_names(self) -> tuple[str, ...]:
        """Names of streams requested so far, sorted."""
        return tuple(sorted(self._streams))

    def describe(self) -> dict[str, object]:
        """Seed provenance for run metadata (FR-OBS-06)."""
        return {
            "master_seed": self.master_seed,
            "replication_id": self.replication_id,
            "streams": list(self.stream_names),
            "derivation": "SeedSequence(entropy=master_seed, "
            "spawn_key=(replication_id, sha256(stream_name)[:8]))",
        }

"""Samplers (FR-AGT-04) and the reasoning client default (AC-POL-01, FR-CHAT-09)."""

from __future__ import annotations

import socket

import numpy as np
import pytest
from pydantic import TypeAdapter

from simcore.core.policy import (
    DisabledReasoningClient,
    MockReasoningClient,
    ReasoningDisabledError,
    ReasoningRequest,
    ReasoningResponse,
)
from simcore.core.sampling import make_sampler, mean_of
from simcore.specs.distributions import DistributionSpec

DISTS = [
    {"kind": "constant", "value": 2.0},
    {"kind": "exponential", "mean": 3.0},
    {"kind": "lognormal", "mu": 0.5, "sigma": 0.4},
    {"kind": "gamma", "shape": 2.0, "scale": 1.5},
    {"kind": "weibull", "shape": 1.7, "scale": 2.0},
    {"kind": "normal", "mean": 5.0, "sd": 1.0},
    {"kind": "uniform", "low": 1.0, "high": 3.0},
    {"kind": "triangular", "low": 0.0, "mode": 1.0, "high": 4.0},
    {"kind": "empirical", "values": [1.0, 2.0, 6.0], "weights": [1, 1, 2]},
]


@pytest.mark.parametrize("raw", DISTS, ids=[d["kind"] for d in DISTS])
def test_FR_AGT_04_sampler_means(raw: dict[str, object]) -> None:
    spec = TypeAdapter(DistributionSpec).validate_python(raw)
    sample = make_sampler(spec)
    g = np.random.default_rng(0)
    draws = np.array([sample(g) for _ in range(20_000)])
    # 4 standard errors (fixed seed); tolerance documented per CLAUDE.md testing rules.
    se = draws.std() / np.sqrt(len(draws)) + 1e-12
    assert abs(draws.mean() - mean_of(spec)) < 4 * se + 1e-9


def test_truncated_normal_respects_min() -> None:
    spec = TypeAdapter(DistributionSpec).validate_python(
        {"kind": "normal", "mean": 0.0, "sd": 1.0, "min": 0.5}
    )
    g = np.random.default_rng(1)
    assert min(make_sampler(spec)(g) for _ in range(2000)) >= 0.5


def test_AC_POL_01_reasoning_disabled_by_default_and_no_network() -> None:
    client = DisabledReasoningClient()
    assert not client.enabled
    with pytest.raises(ReasoningDisabledError):
        client.complete(ReasoningRequest(system="", messages=[]))
    with pytest.raises(RuntimeError, match="network connection"):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("127.0.0.1", 9))
    with pytest.raises(RuntimeError, match="DNS lookup"):
        socket.getaddrinfo("example.com", 80)


def test_AC_POL_01_engine_default_client_is_disabled() -> None:
    from simcore.api import default_engine

    assert not default_engine().reasoning.enabled


def test_mock_reasoning_client_is_deterministic() -> None:
    mock = MockReasoningClient([ReasoningResponse(text="a"), ReasoningResponse(text="b")])
    req = ReasoningRequest(system="s", messages=[{"role": "user", "content": "x"}])
    assert [mock.complete(req).text, mock.complete(req).text] == ["a", "b"]
    assert len(mock.requests) == 2

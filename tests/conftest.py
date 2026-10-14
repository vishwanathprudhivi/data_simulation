"""Shared fixtures. Network access is blocked for every test (AC-POL-01, CLAUDE.md)."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
MMC = EXAMPLES / "scenarios" / "toy_queue" / "mmc_baseline.yaml"
WAREHOUSE_STUB = EXAMPLES / "scenarios" / "warehouse_stub" / "shift.yaml"


class NetworkBlockedError(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def _no_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if request.node.get_closest_marker("redis") or request.node.get_closest_marker("live_llm"):
        return
    real_connect = socket.socket.connect

    def guarded(self: socket.socket, address: object) -> None:
        if self.family == socket.AF_UNIX:
            return real_connect(self, address)  # type: ignore[arg-type]
        raise NetworkBlockedError(f"test attempted a network connection to {address!r}")

    def no_dns(*args: object, **kwargs: object) -> None:
        raise NetworkBlockedError(f"test attempted a DNS lookup for {args[:2]!r}")

    monkeypatch.setattr(socket.socket, "connect", guarded)
    monkeypatch.setattr(socket, "getaddrinfo", no_dns)


@pytest.fixture
def mmc_path() -> Path:
    return MMC


@pytest.fixture
def warehouse_stub_path() -> Path:
    return WAREHOUSE_STUB


@pytest.fixture
def plugins():  # type: ignore[no-untyped-def]
    from simcore.api import default_plugins

    return default_plugins()


def _redis_available() -> bool:
    try:
        import redis  # noqa: F401
    except ImportError:
        return False
    import os

    url = os.environ.get("SIMCORE_REDIS_URL")
    return url is not None


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip Isaac/Redis/live-LLM tests when their dependency is absent (CLAUDE.md testing rules)."""
    import importlib.util

    isaac = importlib.util.find_spec("isaacsim") is not None
    redis_ok = _redis_available()
    for item in items:
        if "isaac" in item.keywords and not isaac:
            item.add_marker(pytest.mark.skip(reason="Isaac Sim not installed"))
        if "redis" in item.keywords and not redis_ok:
            item.add_marker(pytest.mark.skip(reason="SIMCORE_REDIS_URL not set"))
        if "live_llm" in item.keywords:
            item.add_marker(pytest.mark.skip(reason="live LLM tests never run automatically"))

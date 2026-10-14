"""``ReasoningClient``: the only door to an LLM (BRD 7.6, FR-CHAT-09, AC-POL-01).

The engine default is :class:`DisabledReasoningClient`; simulation results never
depend on a live model call. Tests use :class:`MockReasoningClient`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "DisabledReasoningClient",
    "MockReasoningClient",
    "ReasoningClient",
    "ReasoningDisabledError",
    "ReasoningRequest",
    "ReasoningResponse",
]


class ReasoningDisabledError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReasoningRequest:
    system: str
    messages: Sequence[Mapping[str, Any]]
    tools: Sequence[Mapping[str, Any]] = ()
    response_schema: Mapping[str, Any] | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReasoningResponse:
    text: str = ""
    tool_calls: Sequence[Mapping[str, Any]] = ()
    structured: Mapping[str, Any] | None = None
    model: str = ""
    usage: Mapping[str, int] = field(default_factory=dict)


class ReasoningClient(ABC):
    @abstractmethod
    def complete(self, request: ReasoningRequest) -> ReasoningResponse: ...

    @property
    def enabled(self) -> bool:
        return True


class DisabledReasoningClient(ReasoningClient):
    """Default client: refuses every request and makes no network calls."""

    def complete(self, request: ReasoningRequest) -> ReasoningResponse:
        raise ReasoningDisabledError(
            "the reasoning client is disabled (engine default); configure a provider to enable it"
        )

    @property
    def enabled(self) -> bool:
        return False


class MockReasoningClient(ReasoningClient):
    """Deterministic client returning scripted responses (or a function of the request)."""

    def __init__(
        self,
        responses: Sequence[ReasoningResponse] = (),
        responder: Callable[[ReasoningRequest], ReasoningResponse] | None = None,
    ) -> None:
        self._responses = list(responses)
        self._responder = responder
        self.requests: list[ReasoningRequest] = []

    def complete(self, request: ReasoningRequest) -> ReasoningResponse:
        self.requests.append(request)
        if self._responder is not None:
            return self._responder(request)
        if not self._responses:
            raise RuntimeError("MockReasoningClient has no scripted responses left")
        return self._responses.pop(0)

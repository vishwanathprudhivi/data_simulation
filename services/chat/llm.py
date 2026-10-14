"""Reasoning clients for the service layer (FR-CHAT-09, NFR-SEC-05).

* :class:`AnthropicReasoningClient` - live calls via the official SDK.
* :class:`RecordingReasoningClient` - wraps a client and stores each exchange.
* :class:`ReplayReasoningClient` - serves stored exchanges; tests use it (or the
  mock / heuristic clients) so the suite never needs network access.

``make_client`` picks one from settings: ``disabled`` (default), ``heuristic``,
``anthropic``, ``replay:<dir>`` or ``record:<dir>``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from simcore.core.hashing import content_hash
from simcore.core.policy.reasoning import (
    DisabledReasoningClient,
    ReasoningClient,
    ReasoningRequest,
    ReasoningResponse,
)

__all__ = [
    "DEFAULT_MODEL",
    "AnthropicReasoningClient",
    "ReasoningRefusal",
    "RecordingReasoningClient",
    "ReplayReasoningClient",
    "make_client",
    "request_key",
]

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ReasoningRefusal(RuntimeError):
    """The model declined the request (``stop_reason == "refusal"``)."""


def request_key(request: ReasoningRequest) -> str:
    return content_hash(
        {
            "system": request.system,
            "messages": list(request.messages),
            "tools": list(request.tools),
            "schema": request.response_schema,
        },
        length=24,
    )


def _response_to_json(r: ReasoningResponse) -> dict[str, Any]:
    return {
        "text": r.text,
        "tool_calls": [dict(c) for c in r.tool_calls],
        "structured": dict(r.structured) if r.structured is not None else None,
        "model": r.model,
        "usage": dict(r.usage),
    }


def _response_from_json(d: Mapping[str, Any]) -> ReasoningResponse:
    return ReasoningResponse(
        text=str(d.get("text", "")),
        tool_calls=list(d.get("tool_calls", [])),
        structured=d.get("structured"),
        model=str(d.get("model", "")),
        usage=dict(d.get("usage", {})),
    )


class AnthropicReasoningClient(ReasoningClient):
    """Claude via the Anthropic SDK; requires ``ANTHROPIC_API_KEY`` (never logged).

    Requests use structured outputs when a ``response_schema`` is given, and
    server-side refusal fallbacks. A refusal raises :class:`ReasoningRefusal`
    instead of returning partial content.
    """

    def __init__(
        self, model: str = DEFAULT_MODEL, max_tokens: int = 16000, client: Any = None
    ) -> None:
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self.client = client
        self.model = model
        self.max_tokens = max_tokens

    def complete(self, request: ReasoningRequest) -> ReasoningResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": request.system,
            "messages": list(request.messages),
            "betas": [FALLBACK_BETA],
            "fallbacks": "default",
        }
        if request.tools:
            kwargs["tools"] = list(request.tools)
        if request.response_schema is not None:
            from anthropic import transform_schema

            kwargs["output_config"] = {
                "format": {
                    "type": "json_schema",
                    "schema": transform_schema(dict(request.response_schema)),
                }
            }
        msg = self.client.beta.messages.create(**kwargs)
        if msg.stop_reason == "refusal":
            raise ReasoningRefusal("the model declined this request")
        text = "".join(b.text for b in msg.content if b.type == "text")
        calls = [
            {"id": b.id, "name": b.name, "input": dict(b.input)}
            for b in msg.content
            if b.type == "tool_use"
        ]
        structured = None
        if request.response_schema is not None and text.strip():
            structured = json.loads(text)
        usage = {
            "input_tokens": int(getattr(msg.usage, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(msg.usage, "output_tokens", 0) or 0),
        }
        return ReasoningResponse(
            text=text, tool_calls=calls, structured=structured, model=msg.model, usage=usage
        )


class RecordingReasoningClient(ReasoningClient):
    """Delegates to ``inner`` and stores each exchange as ``<dir>/<request_key>.json``."""

    def __init__(self, inner: ReasoningClient, directory: Path) -> None:
        self.inner, self.directory = inner, Path(directory)

    def complete(self, request: ReasoningRequest) -> ReasoningResponse:
        response = self.inner.complete(request)
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{request_key(request)}.json"
        path.write_text(
            json.dumps(
                {"request_metadata": dict(request.metadata), **_response_to_json(response)},
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        return response


class ReplayReasoningClient(ReasoningClient):
    """Serves recorded exchanges; an unrecorded request is an error (no network)."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def complete(self, request: ReasoningRequest) -> ReasoningResponse:
        path = self.directory / f"{request_key(request)}.json"
        if not path.exists():
            raise KeyError(f"no recording for request {path.stem} in {self.directory}")
        return _response_from_json(json.loads(path.read_text()))


def make_client(spec: str | None = None) -> ReasoningClient:
    """Build the configured client (``SIMCORE_REASONING`` when ``spec`` is None)."""
    spec = spec if spec is not None else os.environ.get("SIMCORE_REASONING", "disabled")
    kind, _, arg = spec.partition(":")
    if kind == "disabled":
        return DisabledReasoningClient()
    if kind == "heuristic":
        from services.chat.heuristic import HeuristicCompilerClient

        return HeuristicCompilerClient()
    if kind == "anthropic":
        return AnthropicReasoningClient(model=arg or DEFAULT_MODEL)
    if kind == "replay":
        return ReplayReasoningClient(Path(arg))
    if kind == "record":
        return RecordingReasoningClient(AnthropicReasoningClient(), Path(arg))
    raise ValueError(
        f"unknown reasoning client {spec!r}; use disabled, heuristic, anthropic[:model], "
        "replay:<dir> or record:<dir>"
    )

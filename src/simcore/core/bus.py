"""Typed, deterministic in-process publish/subscribe (FR-ENG-03).

Event kinds are registered with a payload type; publishing a payload of the
wrong type is an error. Subscribers are called synchronously in subscription
order, then wildcard subscribers in subscription order.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from simcore.core.scheduler import ScheduledEvent

__all__ = ["EventBus", "Handler", "UnknownEventKind"]

Handler = Callable[[ScheduledEvent], None]


class UnknownEventKind(KeyError):
    pass


class EventBus:
    def __init__(self) -> None:
        self._types: dict[str, type[Any] | None] = {}
        self._subs: dict[str, list[Handler]] = {}
        self._wildcard: list[Handler] = []
        self.published = 0

    def register(self, kind: str, payload_type: type[Any] | None = None) -> None:
        """Declare event ``kind`` with an optional payload type. Idempotent if consistent."""
        existing = self._types.get(kind, payload_type)
        if kind in self._types and existing is not payload_type:
            raise ValueError(
                f"event kind {kind!r} already registered with payload {existing!r}, "
                f"not {payload_type!r}"
            )
        self._types[kind] = payload_type

    def is_registered(self, kind: str) -> bool:
        return kind in self._types

    def subscribe(self, kind: str, handler: Handler) -> None:
        if kind == "*":
            self._wildcard.append(handler)
            return
        if kind not in self._types:
            raise UnknownEventKind(f"subscribe to unregistered event kind {kind!r}")
        self._subs.setdefault(kind, []).append(handler)

    def check(self, kind: str, payload: object) -> None:
        if kind not in self._types:
            raise UnknownEventKind(
                f"event kind {kind!r} is not registered; call bus.register({kind!r}, ...)"
            )
        expected = self._types[kind]
        if expected is not None and not isinstance(payload, expected):
            raise TypeError(
                f"event {kind!r} expects payload {expected.__name__}, got {type(payload).__name__}"
            )

    def publish(self, event: ScheduledEvent) -> None:
        self.check(event.kind, event.payload)
        self.published += 1
        for handler in self._subs.get(event.kind, ()):
            handler(event)
        for handler in self._wildcard:
            handler(event)

    @property
    def kinds(self) -> tuple[str, ...]:
        return tuple(sorted(self._types))

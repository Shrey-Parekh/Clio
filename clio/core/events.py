"""Async pub/sub. Voice, UI, capabilities, and remote react to the same events
without being wired to each other directly.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from clio.core.logging import get_logger

log = get_logger("clio.events")

Handler = Callable[["Event"], Awaitable[None]]

_WILDCARD = "*"


@dataclass(frozen=True)
class Event:
    name: str
    payload: dict[str, Any] = field(default_factory=dict)
    source: str = "unknown"
    timestamp: float = field(default_factory=time.time)


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, list[Handler]] = {}

    def subscribe(self, event_name: str, handler: Handler) -> Callable[[], None]:
        """Register a handler for `event_name`, or `"*"` for every event.
        Returns a callable that unsubscribes it.
        """
        self._subscribers.setdefault(event_name, []).append(handler)

        def unsubscribe() -> None:
            handlers = self._subscribers.get(event_name)
            if handlers and handler in handlers:
                handlers.remove(handler)

        return unsubscribe

    async def publish(self, event_name: str, payload: dict[str, Any] | None = None, source: str = "unknown") -> None:
        event = Event(name=event_name, payload=payload or {}, source=source)
        handlers = self._subscribers.get(event_name, []) + self._subscribers.get(_WILDCARD, [])

        for handler in handlers:
            try:
                await handler(event)
            except Exception:
                log.error(
                    "Event handler failed",
                    exc_info=True,
                    extra={"extra_fields": {"event": event_name, "handler": getattr(handler, "__qualname__", str(handler))}},
                )

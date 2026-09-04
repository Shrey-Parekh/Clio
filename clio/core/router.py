"""Deterministic intent matching, ahead of the LLM.

Anything matched here is handled without an API call. The LLM is the fallback
for language and judgment, not the default path for every utterance.

An intent is a matcher and a handler. The matcher inspects the transcribed text
and returns a payload if it recognises the request, or None to decline. The
handler receives that payload and returns what to say, or None to say nothing.
Registration order is match order: register narrower intents first.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from clio.core.logging import get_logger

log = get_logger("clio.router")

Matcher = Callable[[str], object | None]
Handler = Callable[[object], Awaitable[str | None]]


@dataclass(frozen=True)
class Intent:
    name: str
    match: Matcher
    handle: Handler


@dataclass(frozen=True)
class Routed:
    """A handled utterance. `reply` is what to speak, or None to stay silent."""

    intent: str
    reply: str | None


class IntentRouter:
    def __init__(self) -> None:
        self._intents: list[Intent] = []

    def register(self, name: str, match: Matcher, handle: Handler) -> None:
        self._intents.append(Intent(name=name, match=match, handle=handle))

    @property
    def names(self) -> list[str]:
        return [i.name for i in self._intents]

    async def route(self, text: str) -> Routed | None:
        """Returns None when nothing matches, meaning the caller should fall
        through to the LLM. A matcher that raises is logged and skipped rather
        than taking down the turn - one bad pattern must not block the rest.
        """
        for intent in self._intents:
            try:
                payload = intent.match(text)
            except Exception:
                log.exception("Intent matcher failed", extra={"extra_fields": {"intent": intent.name}})
                continue

            if payload is None:
                continue

            log.info("Routed without LLM", extra={"extra_fields": {"intent": intent.name}})
            return Routed(intent=intent.name, reply=await intent.handle(payload))

        return None

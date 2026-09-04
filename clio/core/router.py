"""Deterministic intent matching, ahead of the LLM.

Anything matched here is handled without an API call. The LLM is the fallback
for language and judgment, not the default path for every utterance.

An intent is a matcher and a handler. The matcher inspects the transcribed text
and returns a payload if it recognises the request, or None to decline. The
handler receives that payload and returns what to say, or None to say nothing.
Registration order is match order: register narrower intents first.

Matching and running are separate steps. The permission tier sits between them,
so an action needing confirmation is recognised but not performed until the
caller has actually asked.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from clio.core.logging import get_logger
from clio.core.permissions import Permission, PermissionPolicy

log = get_logger("clio.router")

Matcher = Callable[[str], object | None]
Handler = Callable[[object], Awaitable[str | None]]
Describer = Callable[[object], str]


@dataclass(frozen=True)
class Intent:
    name: str
    match: Matcher
    handle: Handler
    describe: Describer | None = None


@dataclass(frozen=True)
class Match:
    """A recognised request, not yet performed."""

    intent: str
    permission: Permission
    description: str
    _handler: Handler
    _payload: object

    async def run(self) -> str | None:
        """Performs the action. Returns what to say, or None to stay silent."""
        return await self._handler(self._payload)


class IntentRouter:
    def __init__(self, policy: PermissionPolicy | None = None) -> None:
        self._intents: list[Intent] = []
        self._policy = policy or PermissionPolicy()

    def register(
        self,
        name: str,
        match: Matcher,
        handle: Handler,
        describe: Describer | None = None,
    ) -> None:
        self._intents.append(Intent(name=name, match=match, handle=handle, describe=describe))

    @property
    def names(self) -> list[str]:
        return [i.name for i in self._intents]

    def match(self, text: str) -> Match | None:
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

            permission = self._policy.tier(intent.name)
            description = intent.describe(payload) if intent.describe else intent.name
            log.info(
                "Matched without LLM",
                extra={"extra_fields": {"intent": intent.name, "permission": permission.value}},
            )
            return Match(
                intent=intent.name,
                permission=permission,
                description=description,
                _handler=intent.handle,
                _payload=payload,
            )

        return None

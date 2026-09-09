"""Deterministic intent matching, ahead of the LLM.

An intent is a matcher (text -> payload, or None to decline) and a handler
(payload -> reply, or None to stay silent). Registration order is match order,
so narrower intents register first. Matching and running are separate steps
with the permission tier between them, so a confirm-tier action is recognised
but not performed until the caller has asked. `capabilities()` lists what
exists without running anything.
"""

from __future__ import annotations

import re
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
    offline: bool = True


@dataclass(frozen=True)
class Capability:
    """One registered capability, for listing rather than running."""

    name: str
    permission: Permission
    offline: bool


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


# Spoken connectors between commands. Capped low: a chain of five is a sentence
# nobody says and can't follow the answer to.
_CONNECTOR = re.compile(
    r"\s+and then\s+|\s+then\s+|\s+and also\s+|\s+and\s+|\s*[;,]\s*", re.IGNORECASE
)
_MAX_STEPS = 4

# Said, but not a request. A vocative or courtesy left as a split part matches
# nothing, and the all-parts-must-match rule would then discard the whole chain.
_FILLER = {
    "clio", "hey", "hi", "hello", "please", "thanks", "thank you", "ok", "okay",
    "also", "and", "so", "well", "um", "uh", "yeah", "right",
}
_PUNCT = re.compile(r"[.!?,;:]+$")


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
        offline: bool = True,
    ) -> None:
        """`offline` is the capability's own claim about needing the network;
        the tier is not, since letting a capability declare its own risk would
        let adding one grant it authority. The tier is resolved now, not at
        first use, so an unclassified capability shows up at startup."""
        self._intents.append(
            Intent(name=name, match=match, handle=handle, describe=describe, offline=offline)
        )
        log.info(
            "Capability registered",
            extra={
                "extra_fields": {
                    "capability": name,
                    "permission": self._policy.tier(name).value,
                    "offline": offline,
                }
            },
        )

    def capabilities(self) -> list[Capability]:
        """Everything registered, in match order."""
        return [
            Capability(name=i.name, permission=self._policy.tier(i.name), offline=i.offline)
            for i in self._intents
        ]

    def plan(self, text: str) -> list[Match]:
        """The steps to run, in order. A split is only accepted when *every*
        part matches an intent by itself; otherwise the whole sentence is one
        request. This keeps "pick between tea and coffee" (one request) intact
        while still splitting "mute Chrome and lock the screen" (two actions).
        """
        parts = [
            part for part in (p.strip() for p in _CONNECTOR.split(text))
            if part and _PUNCT.sub("", part.lower()) not in _FILLER
        ]
        if 2 <= len(parts) <= _MAX_STEPS:
            steps = [self.match(part) for part in parts]
            if all(step is not None for step in steps):
                log.info(
                    "Planned a chain",
                    extra={"extra_fields": {"steps": [s.intent for s in steps]}},  # type: ignore[union-attr]
                )
                return steps  # type: ignore[return-value]

        single = self.match(text)
        return [single] if single is not None else []

    def match(self, text: str) -> Match | None:
        """None means nothing matched and the caller falls through to the LLM.
        A matcher that raises is logged and skipped, so one bad pattern doesn't
        block the rest."""
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

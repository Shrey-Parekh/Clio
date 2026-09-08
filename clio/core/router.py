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

Registering is also how a capability is declared: `capabilities()` lists what
exists, its tier, and whether it needs the network, without running anything.
This is the registry, extracted from four working capabilities rather than
designed ahead of them - matcher, handler, offline claim, and a tier the
policy assigns rather than the capability itself.
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
    """One registered capability, for anything that needs to list them rather
    than run them - what still works with no network, and the settings UI."""

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


# Only what someone actually says to join two commands. Capped low: a spoken
# chain of five things is a sentence he would not say and could not follow the
# answer to.
_CONNECTOR = re.compile(
    r"\s+and then\s+|\s+then\s+|\s+and also\s+|\s+and\s+|\s*[;,]\s*", re.IGNORECASE
)
_MAX_STEPS = 4


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
        """`offline` is the capability's own claim about needing the network -
        a timer does not, weather does. It is declared here because only the
        capability knows; the permission tier is not, because letting one
        declare its own risk would mean adding a capability could quietly grant
        it authority. Registering resolves the tier now rather than at first
        use, so an unclassified capability is visible at startup.
        """
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
        """The steps to run, in the order he said them.

        "Mute Chrome and lock the screen" is two actions, and answering only
        the first is the fire-and-forget failure 2.7 exists to prevent.

        The comma matters as much as the "and": without it, "alpha, beta and
        gamma" split into two parts, and the part reading "alpha, beta"
        matched alpha alone - silently dropping a command, which is the exact
        failure this is here to prevent.

        Splitting on "and" is dangerous on its own - "pick between tea and
        coffee" is one request, "find and summarise" is one verb phrase. What
        makes it safe is that a split is only accepted when *every* part
        matches an intent by itself. Anything less falls back to treating the
        whole sentence as one request, which is what it was before this
        existed, so a bad split cannot make things worse than not splitting.
        """
        parts = [p.strip() for p in _CONNECTOR.split(text) if p.strip()]
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

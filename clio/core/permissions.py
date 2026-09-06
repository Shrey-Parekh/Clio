"""Central Free / Confirm / Blocked classification for everything Clio can do.

One table, read in one place, rather than a permission check scattered through
each capability. A capability declares nothing about its own risk; the policy
decides, so adding a capability cannot quietly grant it new authority.

Unknown actions are CONFIRM, not FREE. A capability added without a rule asks
before acting instead of acting silently.
"""

from __future__ import annotations

import re
from enum import Enum

from clio.core.logging import get_logger

log = get_logger("clio.permissions")


class Permission(Enum):
    FREE = "free"
    CONFIRM = "confirm"
    BLOCKED = "blocked"


# Only actions that exist today. Phase 3 capabilities add their rule here as
# they land - the fail-safe default covers anything not yet classified.
_DEFAULT_RULES: dict[str, Permission] = {
    "timer": Permission.FREE,
    "stop": Permission.FREE,
    "status": Permission.FREE,
    "repeat": Permission.FREE,
    "diagnose": Permission.FREE,
}

_AFFIRMATIVE = {
    "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "confirm", "confirmed",
    "do it", "go ahead", "go for it", "please do", "affirmative", "correct",
    "that's right", "thats right", "right", "fine", "alright",
}

_STRIP = re.compile(r"[.!?,;:]+$")


class PermissionPolicy:
    def __init__(self, rules: dict[str, Permission] | None = None):
        self._rules = dict(_DEFAULT_RULES)
        if rules:
            self._rules.update(rules)

    def tier(self, action: str) -> Permission:
        tier = self._rules.get(action)
        if tier is None:
            log.warning(
                "Unclassified action, defaulting to confirm",
                extra={"extra_fields": {"action": action}},
            )
            return Permission.CONFIRM
        return tier

    def summary(self) -> dict[str, str]:
        """The whole policy, for inspection and for the settings UI in 5.5."""
        return {action: tier.value for action, tier in sorted(self._rules.items())}


def is_affirmative(text: str) -> bool:
    """Only an explicit yes counts. Anything else - silence, a question, an
    unrelated sentence - is a no, because the cost of a false yes is a
    destructive action the user did not ask for.
    """
    return _STRIP.sub("", text.strip().lower()) in _AFFIRMATIVE

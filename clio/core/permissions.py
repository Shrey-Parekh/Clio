"""Central Free / Confirm / Blocked classification, in one table.

A capability declares nothing about its own risk; the policy decides, so adding
one can't quietly grant it authority. Unknown actions fail safe to CONFIRM.
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


# FREE = read-only, answer-only, or reversible in a second. CONFIRM = can end
# work or lose data. Anything unlisted fails safe to CONFIRM.
_DEFAULT_RULES: dict[str, Permission] = {
    "timer": Permission.FREE,
    "remind": Permission.FREE,
    "remind_control": Permission.FREE,
    "stop": Permission.FREE,
    "status": Permission.FREE,
    "repeat": Permission.FREE,
    "diagnose": Permission.FREE,
    "calculate": Permission.FREE,
    "convert": Permission.FREE,
    "currency": Permission.FREE,
    "weather": Permission.FREE,
    "system": Permission.FREE,
    "open": Permission.FREE,      # own Start Menu/shortcuts only, never a spoken path
    "control": Permission.FREE,   # volume, windows, displays, lock
    "power": Permission.CONFIRM,  # sleep ends whatever was running
    "files": Permission.FREE,     # read-only, confined to configured roots
    "clock": Permission.FREE,
    "chance": Permission.FREE,
    "stopwatch": Permission.FREE,
    "timer_control": Permission.FREE,
    "network": Permission.FREE,
    "media": Permission.FREE,
    "help": Permission.FREE,
    "voice": Permission.FREE,
    "close": Permission.CONFIRM,  # can lose unsaved work
    "clipboard": Permission.FREE,  # previous contents kept; "put it back" restores
    "notes": Permission.FREE,      # append-only to a plain text file
    "tasks": Permission.FREE,      # ticking off is undone by saying the task again
    "web": Permission.FREE,        # search and read only, answers out loud
    "email": Permission.FREE,      # reads the mailbox; cannot send, delete or mark read
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
        """The whole policy, for inspection and the settings UI."""
        return {action: tier.value for action, tier in sorted(self._rules.items())}


def is_affirmative(text: str) -> bool:
    """Only an explicit yes counts; anything else is a no, since a false yes
    runs a destructive action the user didn't ask for."""
    return _STRIP.sub("", text.strip().lower()) in _AFFIRMATIVE

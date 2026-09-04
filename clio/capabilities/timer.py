"""Timers: the first real capability, and the deterministic fast path proof.
Parsing never touches an LLM - a regex either finds a clear duration or it
doesn't, and if it doesn't, the utterance falls through to plain conversation
rather than a fuzzy guess. A timer must never cost an API call.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Awaitable, Callable

from clio.core.logging import get_logger

log = get_logger("clio.capabilities.timer")

AnnounceFn = Callable[[str], Awaitable[None]]

_MAX_DURATION_S = 24 * 3600

_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30,
    "forty": 40, "fifty": 50, "sixty": 60,
}

_UNIT_SECONDS = {
    "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600,
    "minute": 60, "minutes": 60, "min": 60, "mins": 60,
    "second": 1, "seconds": 1, "sec": 1, "secs": 1,
}

_DURATION_PATTERN = re.compile(
    r"\b(?P<number>\d+(?:\.\d+)?|" + "|".join(_NUMBER_WORDS) + r")\s+"
    r"(?P<unit>hours?|hrs?|minutes?|mins?|seconds?|secs?)\b"
)


def parse_timer_command(text: str) -> float | None:
    """Returns the requested duration in seconds if `text` clearly asks to set
    a timer for a specific duration, else None (not a timer command, or the
    duration wasn't clear enough to parse deterministically - falls through
    to plain conversation rather than guessing).
    """
    lowered = text.lower()
    if "timer" not in lowered:
        return None

    match = _DURATION_PATTERN.search(lowered)
    if match is None:
        return None

    number_text = match.group("number")
    number = float(number_text) if number_text[0].isdigit() else float(_NUMBER_WORDS[number_text])
    seconds = number * _UNIT_SECONDS[match.group("unit")]

    if seconds <= 0 or seconds > _MAX_DURATION_S:
        return None
    return seconds


def format_duration(duration_s: float) -> str:
    total = int(round(duration_s))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)

    parts = []
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if seconds and not hours:
        parts.append(f"{seconds} second{'s' if seconds != 1 else ''}")
    return " ".join(parts) if parts else "0 seconds"


class TimerCapability:
    def __init__(self, announce: AnnounceFn):
        self._announce = announce
        self._active: dict[str, asyncio.Task] = {}

    def start(self, duration_s: float) -> str:
        """Schedules the timer and returns the confirmation text to speak
        immediately - scheduling itself never awaits anything, so this never
        blocks the caller on the timer actually firing.
        """
        timer_id = uuid.uuid4().hex[:8]
        task = asyncio.ensure_future(self._run(timer_id, duration_s))
        self._active[timer_id] = task
        return f"Okay, timer set for {format_duration(duration_s)}."

    async def _run(self, timer_id: str, duration_s: float) -> None:
        try:
            await asyncio.sleep(duration_s)
            log.info(
                "Timer fired",
                extra={"extra_fields": {"timer_id": timer_id, "duration_s": duration_s}},
            )
            await self._announce(f"Your {format_duration(duration_s)} timer is up.")
        finally:
            self._active.pop(timer_id, None)

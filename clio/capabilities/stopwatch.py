"""A stopwatch, which is the other half of timers.

A timer counts down to something he already knows the length of. A stopwatch
answers "how long did that actually take", which is the question worth asking
about work. One running at a time - "the stopwatch" has to mean something.
"""

from __future__ import annotations

import re
import time

from clio.capabilities.timer import format_duration

_STRIP = re.compile(r"[.!?,;:]+$")

_PATTERNS: list[tuple[str, str]] = [
    ("start", r"start (?:a |the )?stopwatch|start timing|start the clock"),
    ("stop", r"stop (?:the )?stopwatch|stop timing|stop the clock"),
    ("check", r"how long (?:has it been|have i been|on the stopwatch)|"
              r"(?:check|read) the stopwatch|what(?:'?s| is) (?:on )?the stopwatch"),
]

_COMPILED = [(kind, re.compile(p)) for kind, p in _PATTERNS]


def parse_stopwatch_command(text: str) -> str | None:
    lowered = " ".join(_STRIP.sub("", text.strip().lower()).split())
    for kind, pattern in _COMPILED:
        if pattern.search(lowered):
            return kind
    return None


class Stopwatch:
    """Monotonic, not wall clock: a clock change or a daylight saving jump must
    not turn a five minute measurement into an hour."""

    def __init__(self) -> None:
        self._started_at: float | None = None

    @property
    def running(self) -> bool:
        return self._started_at is not None

    def elapsed_s(self) -> float:
        return 0.0 if self._started_at is None else time.monotonic() - self._started_at

    def handle(self, command: str) -> str:
        if command == "start":
            if self.running:
                # Restarting silently would throw away a measurement he may
                # have been in the middle of.
                return f"Already running, {format_duration(self.elapsed_s())} so far."
            self._started_at = time.monotonic()
            return "Right, the stopwatch is running."

        if command == "stop":
            if not self.running:
                return "The stopwatch isn't running just now."
            elapsed = self.elapsed_s()
            self._started_at = None
            return f"Stopped it at {format_duration(elapsed)}."

        if not self.running:
            return "The stopwatch isn't running just now."
        return f"You're {format_duration(self.elapsed_s())} in so far."

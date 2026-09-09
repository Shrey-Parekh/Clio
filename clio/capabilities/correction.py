"""Detect "no, not like that" and keep it as a standing rule, verbatim."""

from __future__ import annotations

import re

_MARKERS = (
    r"no,?\s+(?:i\s+said|i\s+meant|that'?s\s+not|it'?s\s+not)",
    r"that'?s\s+not\s+what\s+i\s+(?:said|meant|asked)",
    r"i\s+did\s?n'?o?t\s+(?:say|mean|ask\s+for)",
    r"(?:please\s+)?(?:stop|do\s?n'?o?t)\s+(?:calling|call|saying|say|doing|do|assuming|assume)\s+",
    r"from\s+now\s+on\b",
    r"next\s+time\b",
    r"remember(?:\s+that)?,?\s+",
)

# Anchored at the start: a mid-sentence "no" is usually part of an answer, and a
# false positive here writes a rule that outlives every later session.
_CORRECTION = re.compile(r"^(?:" + "|".join(_MARKERS) + r")", re.IGNORECASE)

# A bare marker ("next time") states no rule worth keeping.
_MIN_WORDS = 4


def parse_correction(text: str) -> str | None:
    """The utterance verbatim as a standing rule, or None if it wasn't a correction."""
    cleaned = " ".join(text.split())
    if len(cleaned.split()) < _MIN_WORDS or not _CORRECTION.match(cleaned):
        return None
    return cleaned

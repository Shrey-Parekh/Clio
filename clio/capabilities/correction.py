"""Deterministic detection of "no, not like that".

A correction is kept in his own words and scoped to nothing wider than what he
actually said. Inferring a broader rule than the correction it came from makes
it misfire in situations he never spoke about, and facts.md is permanent - so
the whole utterance is stored verbatim and nothing is generalised from it.

Markers are anchored at the start. "No" in the middle of a sentence is usually
part of an answer rather than a correction of one, and a false positive here
writes a standing rule that survives every later session.
"""

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

_CORRECTION = re.compile(r"^(?:" + "|".join(_MARKERS) + r")", re.IGNORECASE)

# A marker on its own ("next time") states no rule worth keeping.
_MIN_WORDS = 4


def parse_correction(text: str) -> str | None:
    """Returns the standing rule - the utterance verbatim - or None if this
    wasn't a correction."""
    cleaned = " ".join(text.split())
    if len(cleaned.split()) < _MIN_WORDS or not _CORRECTION.match(cleaned):
        return None
    return cleaned

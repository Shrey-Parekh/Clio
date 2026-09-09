"""Deterministic match for "do that again" — a router action never reached the
model, so repeating one needs the router to remember its last plan."""

from __future__ import annotations

import re

_REPEAT_PHRASES = {
    "again", "do that again", "do it again", "same again", "one more time",
    "repeat that", "repeat", "once more", "another one",
}

_STRIP = re.compile(r"[.!?,;:]+$")


def is_repeat_command(text: str) -> bool:
    return _STRIP.sub("", text.strip().lower()) in _REPEAT_PHRASES

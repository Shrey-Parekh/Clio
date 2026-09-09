"""Deterministic match for "what still works" — answering a connectivity
question by calling the network would be self-defeating, so it never does."""

from __future__ import annotations

import re

_STATUS_PHRASES = {
    "status", "are you online", "are you offline", "what is working",
    "what's working", "whats working", "are you connected", "connection status",
    "what still works", "are you ok", "system status",
}

_STRIP = re.compile(r"[.!?,;:]+$")


def is_status_query(text: str) -> bool:
    return _STRIP.sub("", text.strip().lower()) in _STATUS_PHRASES

"""Deterministic match for "stop talking", so it never reaches the LLM.

Sending it to the model produces a reply, which is the opposite of what was
asked for. Same fast-path rule as timers: no API call.
"""

from __future__ import annotations

import re

_STOP_PHRASES = {
    "stop", "stop it", "stop talking", "shut up", "shush", "hush", "quiet",
    "be quiet", "that's enough", "thats enough", "enough", "silence",
    "never mind", "nevermind", "cancel", "cancel that", "forget it", "nothing",
    "nvm",
}

_STRIP_PUNCT = re.compile(r"[.!?,;:]+$")


def is_stop_command(text: str) -> bool:
    """True only if the whole utterance is a stop phrase. Substring matching
    would swallow real commands like "stop the timer".
    """
    normalized = _STRIP_PUNCT.sub("", text.strip().lower())
    return normalized in _STOP_PHRASES

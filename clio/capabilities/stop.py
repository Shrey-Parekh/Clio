"""Recognizing "stop talking" as its own thing, deterministically. Found from a
real live-mic session: barge-in correctly cut Clio off mid-reply, but the words
used to interrupt her ("Shut up.") then went to the LLM like any other question
and came back as a fresh 655-character reply - talking over the "stop talking."
This is the same fast-path idea as timers: a clear intent that must never reach
the model, because the model is exactly what needs to not run right now.
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
    """True only if the whole utterance is a stop phrase, not merely contains
    one - "stop the timer" is a real request and must not be swallowed here.
    """
    normalized = _STRIP_PUNCT.sub("", text.strip().lower())
    return normalized in _STOP_PHRASES

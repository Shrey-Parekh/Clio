"""Deterministic match for "what went wrong", and the answer to it.

Every failure is already published on the bus with the stage it happened in,
its category, and whether a retry could help. Answering from that record is
the difference between "something went wrong" and naming the thing that did.
"""

from __future__ import annotations

import re
import time

_DIAGNOSIS_PHRASES = {
    "what went wrong", "what happened", "what failed", "what broke",
    "why did that fail", "why did that not work", "why didn't that work",
    "why didnt that work", "what was that error", "diagnose",
}

_STRIP = re.compile(r"[.!?,;:]+$")

# Spoken aloud, so the raw exception text is trimmed to roughly a sentence.
_MAX_DETAIL_CHARS = 160

# Categories are log labels; these are the words for saying them out loud.
_SPOKEN_CATEGORY = {"llm": "language model", "tool": "tool call", "stt": "transcription"}


def is_diagnosis_query(text: str) -> bool:
    return _STRIP.sub("", text.strip().lower()) in _DIAGNOSIS_PHRASES


def explain_failure(failure: dict | None, now: float | None = None) -> str:
    """`failure` is an ERROR_EVENT payload plus the `at` timestamp it carried."""
    if not failure:
        return "Nothing has failed since I started, so there's nothing to explain."

    when = _ago((now if now is not None else time.time()) - float(failure.get("at", 0.0)))
    category = failure.get("category", "unknown")
    category = _SPOKEN_CATEGORY.get(category, category)
    article = "an" if category[:1].lower() in "aeiou" else "a"
    line = f"{when}, {failure.get('context', 'something')} failed - {article} {category} problem"

    detail = " ".join(str(failure.get("detail", "")).split())
    if len(detail) > _MAX_DETAIL_CHARS:
        detail = detail[:_MAX_DETAIL_CHARS].rstrip() + "..."
    line = f"{line}: {detail}." if detail else f"{line}."

    outlook = (
        "That one is worth another try."
        if failure.get("retryable")
        else "Trying it again would fail the same way."
    )
    return f"{line} {outlook}"


def _ago(seconds: float) -> str:
    if seconds < 60:
        return "Just now"
    if seconds < 3600:
        return f"About {_plural(round(seconds / 60), 'minute')} ago"
    return f"About {_plural(round(seconds / 3600), 'hour')} ago"


def _plural(count: int, unit: str) -> str:
    return f"{count} {unit}" if count == 1 else f"{count} {unit}s"

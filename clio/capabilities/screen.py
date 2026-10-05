"""Screen reading (8.2): "what's on my screen", "read this to me", "what does
this error mean".

Fixed phrases, so deciding that a sentence is about the screen never costs a
model call. "This" means the screen and "that" does not: "explain that" after
one of her answers is about what she said, and stays conversation.

Asking is the permission (his choice): the capture is FREE and not read back.
She never looks unless asked.
"""

from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from clio.core.screen import CaptureError
from clio.llm.provider import LLMPermanentError, LLMRateLimited

_LEAD = r"(?:(?:hey |ok |okay )?clio,? )?(?:(?:can|could|would) you |please )?"
_WHOLE = r"(?:the |my )?(?:whole|entire|full) screen"
_SCREEN = rf"(?:(?:my |the )?screen|{_WHOLE})"
_PATTERNS = [
    rf"what(?:'s| is) on {_SCREEN}",
    rf"(?:look at|check|see|read) {_SCREEN}",
    rf"what (?:do you see|can you see) on {_SCREEN}",
    r"what am i looking at",
    r"read (?:this|the screen|what's on screen)(?: out)?(?: to me| for me)?(?: out loud)?",
    r"what does this (?:say|mean)",
    r"what does this (?:error|message|warning|code|screen) (?:say|mean)",
    r"what(?:'s| is) this (?:error|message|warning)",
    r"explain this(?: (?:error|message|warning|code|screen|page))?(?: to me)?",
]
_MATCH = re.compile(rf"^{_LEAD}(?:{'|'.join(_PATTERNS)})(?: please)?$")
_IS_WHOLE = re.compile(_WHOLE)

# Spoken, so plain sentences; markdown would be read out as symbols.
SYSTEM = (
    "You are Clio, a voice assistant, looking at a screenshot of his screen. "
    "Answer his question out loud in two to four short plain sentences: no "
    "markdown, no lists, no code blocks. If he asks you to read it, read the main "
    "text, not menus or buttons, and stop after a short paragraph. If it is an "
    "error, say what it means and the usual fix."
)


@dataclass(frozen=True)
class ScreenRequest:
    whole: bool
    question: str


def parse_screen_request(text: str) -> ScreenRequest | None:
    said = re.sub(r"[.!?]+$", "", text.strip().lower())
    if not _MATCH.match(said):
        return None
    return ScreenRequest(whole=bool(_IS_WHOLE.search(said)), question=text.strip())


def clean(answer: str) -> str:
    """What reaches the speaker: no reasoning block, no markdown symbols."""
    answer = re.sub(r"<think>.*?</think>", "", answer, flags=re.S)
    # Not "_": it is part of names like my_var that he may need to hear.
    answer = re.sub(r"[*`]+|^#+\s*", "", answer, flags=re.M)
    return " ".join(answer.split())


_BUDGET = re.compile(
    rf"^{_LEAD}(?:how many (?:screen )?looks(?: at the screen)? (?:are |have i got |do i have )?left"
    r"(?: today)?|what'?s (?:left of )?(?:my |the )?(?:screen|vision) budget)$")


def parse_budget_request(text: str) -> bool | None:
    return True if _BUDGET.match(re.sub(r"[.!?]+$", "", text.strip().lower())) else None


class Budget:
    """Pictures sent per day (8.4). Groq's free tier costs nothing in money but
    caps what can be sent, so the budget is a count, kept on disk so a restart
    doesn't reset it, and starting again each day."""

    def __init__(self, path: Path, cap: int, today=date.today):
        self._path, self.cap, self._today = Path(path), cap, today

    def used(self) -> int:
        try:
            saved = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        return int(saved.get("used", 0)) if saved.get("date") == self._today().isoformat() else 0

    def left(self) -> int:
        return max(self.cap - self.used(), 0)

    def spend(self) -> int:
        used = self.used() + 1
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps({"date": self._today().isoformat(), "used": used}),
                              encoding="utf-8")
        return used

    def spoken(self) -> str:
        left = self.left()
        if left == 0:
            return (f"I've used today's {self.cap} looks at the screen - ask me tomorrow, "
                    "or raise vision_daily_cap in the settings.")
        return f"{left} of today's {self.cap} looks at the screen left."


def same_view(a: bytes, b: bytes) -> bool:
    """Whether two screenshots show the same thing, so the last answer still
    holds. Strict on purpose: measured on drawn terminals, one changed letter
    in an error moves as many pixels as a blinking cursor, so nothing can tell
    them apart. Only a clock-tick's worth of change counts as the same; a
    blink costs a fresh look, but an answer is never stale."""
    from PIL import Image, ImageChops

    def thumb(jpeg: bytes):
        return Image.open(io.BytesIO(jpeg)).convert("L").resize((128, 128), Image.BOX)

    changed = ImageChops.difference(thumb(a), thumb(b)).point(lambda v: 255 if v > 12 else 0)
    return changed.histogram()[255] <= 1


def explain_failure(exc: Exception) -> str:
    if isinstance(exc, CaptureError):
        return "I couldn't take a picture of the screen."
    if isinstance(exc, LLMRateLimited):
        return "I've hit Groq's limit for pictures - try again in a minute."
    if isinstance(exc, LLMPermanentError):
        return ("Groq's image model isn't answering requests any more - the "
                "model_vision setting probably needs changing.")
    return "I can't see the screen right now - Groq's image model didn't answer."

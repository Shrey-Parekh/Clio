""""Note this down" - into a file he can open, read and edit himself.

Plain markdown, appended, grouped by day. Not a database, not JSON, not a row
in the memory index: the point of quick capture is that the thing he captured
is still there in six months, findable by anything, including by him with a
text editor and no Clio running at all.

It lands under the memory root, which sits inside his Documents folder - so
3.6's file search finds notes without being told about them.

Deliberately separate from `facts.md`. That file holds what the model distilled
about him and what he corrected her on; this holds what he asked her to write
down, in his words, unchanged. Mixing them would mean a consolidation pass
could one day rewrite a note he dictated.

Nothing here deletes. Editing and pruning is what a text editor is for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from clio.core.logging import get_logger

log = get_logger("clio.notes")

_HEADER = """# Notes

Captured by voice. Plain text on purpose - edit, reorder or delete anything
here by hand; nothing reads this file expecting a particular shape.
"""

_STRIP = re.compile(r"[.!?,;:]+$")
_MAX_SPOKEN = 5

# Whisper puts a word in front of almost everything: "read my notes" came back
# as "I read my notes", which an anchored pattern rejects outright. Absorbed
# here rather than loosened to a search, so "denote" and "he wrote it down"
# still match nothing.
_LEAD = r"^(?:(?:i|you|we|can you|could you|would you|please|clio|hey|ok|okay|now|just|"
_LEAD += r"i'?d like to|i want to|let'?s)\s+)*"

_PATTERNS: list[tuple[str, str]] = [
    ("read", _LEAD + r"(?:what(?:'?s| is) (?:in )?(?:my |the )?notes|"
                     r"read (?:me |back |out )?(?:my |the )?notes|"
                     r"what did i note|my notes|last few notes)"),
    ("add", _LEAD + r"add (?:this |that )?to (?:my |the )?notes\s*:?\s*(?P<content2>.*)"),
    # Content on the same breath: "note down that the bins go out on Tuesday".
    ("add", _LEAD + r"(?:take a note|take note|make a note|note|jot|write)\s+"
                    r"(?:this |that |it )?(?:down |of )?(?:that |about )?(?P<content>.*)"),
]

_COMPILED = [(kind, re.compile(p)) for kind, p in _PATTERNS]

# Said with nothing after it, "note that down" refers to the conversation
# rather than to a missing sentence.
_REFERENTIAL = {"", "this", "that", "it", "this down", "that down", "down"}


@dataclass(frozen=True)
class Request:
    kind: str
    content: str = ""


def parse_note_request(text: str) -> Request | None:
    lowered = " ".join(_STRIP.sub("", text.strip().lower()).split())
    for kind, pattern in _COMPILED:
        found = pattern.match(lowered)
        if found is None:
            continue
        if kind == "read":
            return Request("read")
        parts = found.groupdict()
        content = (parts.get("content") or parts.get("content2") or "").strip()
        # Empty content means "whatever we were just talking about", which the
        # caller resolves - it is the only part of this that needs the
        # conversation.
        return Request("add", "" if content in _REFERENTIAL else content)
    return None


class NoteBook:
    def __init__(self, path: str | Path):
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def add(self, content: str, now: datetime | None = None) -> str:
        """Appends under today's heading, creating the file and the day as
        needed. Append-only: a note is never rewritten, so a crash halfway
        through can lose the newest line and nothing else.
        """
        content = " ".join(content.split())
        if not content:
            return "Nothing to write down."

        now = now or datetime.now()
        today = f"## {now:%Y-%m-%d}"
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fresh = not self._path.exists() or not self._path.stat().st_size
            existing = "" if fresh else self._path.read_text(encoding="utf-8")
            with self._path.open("a", encoding="utf-8") as handle:
                if fresh:
                    handle.write(_HEADER)
                if today not in existing:
                    handle.write(f"\n{today}\n")
                handle.write(f"- {now:%H:%M} {content}\n")
        except OSError as exc:
            log.warning("Note not saved", extra={"extra_fields": {"error": str(exc)}})
            return "I couldn't write that down - the notes file wouldn't open."

        log.info("Note captured", extra={"extra_fields": {"chars": len(content)}})
        return f"Noted: {content}"

    def recent(self, limit: int = _MAX_SPOKEN) -> str:
        if not self._path.exists():
            return "You haven't got any notes yet."
        try:
            lines = [
                line.strip()[2:]
                for line in self._path.read_text(encoding="utf-8").splitlines()
                if line.startswith("- ")
            ]
        except OSError:
            return "I couldn't open your notes."
        if not lines:
            return "You haven't got any notes yet."

        newest = lines[-limit:][::-1]
        # The times are in the file for when he reads it himself. Spoken back
        # they are noise - he is asking what the notes say, not when.
        spoken = [re.sub(r"^\d{2}:\d{2}\s+", "", line) for line in newest]
        head = f"Your last {len(spoken)} notes: " if len(spoken) > 1 else "One note: "
        return head + ". ".join(spoken) + "."

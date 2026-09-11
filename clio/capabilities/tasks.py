"""A to-do list in a plain markdown file (6.5).

`memory/tasks.md`, one `- [ ]` checkbox per task, beside notes.md. Editable by
hand with no Clio running, and the file search finds it. Unlike notes, ticking
off rewrites a line, so every write goes through a temp file and a rename: a
crash leaves the old list or the new one, never half of either. Nothing here
deletes a task - ticked ones stay until he clears them by hand, and saying one
again puts it back.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from clio.capabilities.notes import _LEAD
from clio.core.logging import get_logger

log = get_logger("clio.tasks")

_HEADER = [
    "# Tasks",
    "",
    "Kept by voice. Edit, reorder or delete anything here by hand. A line is a task",
    "if it starts with `- [ ]`, and a ticked one if it starts with `- [x]`.",
    "",
]

_MAX_SPOKEN = 5
_STRIP = re.compile(r"[.!?,;:]+$")
_LINE = re.compile(r"^\s*[-*] \[(?P<mark>[ xX])\] (?P<text>.+?)\s*$")
_ARTICLES = re.compile(r"^(?:the|my|a|an)\s+")

# "my list", "the to-do list", "my tasks". A named list ("shopping list") is not
# this list, so it falls through rather than landing here unasked.
_LIST = r"(?:(?:my |the )?(?:(?:to[ -]?do|task) )?list|(?:my |the )?(?:tasks|to[ -]?dos))"
_OFF_LIST = r"(?: (?:on|from|off) " + _LIST + r")?"

_PATTERNS: list[tuple[str, str]] = [
    ("list", r"(?:what(?:'?s| is) (?:left |still )?on|read(?: me| out)?|show(?: me)?|check|list) "
             + _LIST + r"$"),
    ("list", r"what(?:'?s| is| are) (?:my |the )?(?:tasks|to[ -]?dos)$"),
    ("list", r"what do i (?:still )?(?:have|need) to do(?: today)?$"),
    ("done", r"(?:tick|cross|check) off (?P<text>.+?)" + _OFF_LIST + r"$"),
    ("done", r"(?:tick|cross|check) (?P<text>.+?) off(?: " + _LIST + r")?$"),
    ("done", r"mark (?P<text>.+?) (?:as )?(?:done|complete|completed|finished)" + _OFF_LIST + r"$"),
    ("add", r"(?:add|put|stick|write) (?P<text>.+?) (?:to|on|onto) " + _LIST + r"$"),
    ("add", r"(?:add|create|make|new) (?:a )?(?:new )?(?:task|to[ -]?do)(?: item)?"
            r"(?: to| for| called|:)? (?P<text>.+)$"),
]

_COMPILED = [(kind, re.compile(_LEAD + p)) for kind, p in _PATTERNS]
_REFERENTIAL = {"it", "this", "that"}


@dataclass(frozen=True)
class TaskRequest:
    kind: str       # "add", "list" or "done"
    text: str = ""  # "" for a list, or for "add that" with nothing to add


def parse_task_request(text: str) -> TaskRequest | None:
    # Commas dropped: "Clio, add..." would otherwise stop the lead words matching.
    lowered = " ".join(_STRIP.sub("", text.strip().lower()).replace(",", " ").split())
    for kind, pattern in _COMPILED:
        found = pattern.match(lowered)
        if found is None:
            continue
        if kind == "list":
            return TaskRequest("list")
        task = found.group("text").strip()
        return TaskRequest(kind, "" if task in _REFERENTIAL else task)
    return None


class TaskList:
    def __init__(self, path: str | Path):
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def add(self, text: str) -> tuple[str, bool]:
        """Returns what to say, and whether a new task was written."""
        text = " ".join(text.split())
        if not text:
            return "Add what to your list?", False
        try:
            lines = self._read() or list(_HEADER)
            for index, done, existing in _tasks(lines):
                if existing.casefold() != text.casefold():
                    continue
                if not done:
                    return f"{existing} is already on your list.", False
                # Saying a ticked task again is how ticking off is undone.
                lines[index] = f"- [ ] {existing}"
                self._write(lines)
                return f"Put {existing} back on your list.", False
            lines.append(f"- [ ] {text}")
            self._write(lines)
        except OSError as exc:
            log.warning("Task not saved", extra={"extra_fields": {"error": str(exc)}})
            return "I couldn't add that - the task list wouldn't open.", False
        log.info("Task added", extra={"extra_fields": {"chars": len(text)}})
        return f"Added {text} to your list.", True

    def listing(self) -> str:
        try:
            tasks = _tasks(self._read())
        except OSError:
            return "I couldn't open your task list."
        open_tasks = [text for _, done, text in tasks if not done]
        if not open_tasks:
            if tasks:
                return "Everything on your list is ticked off."
            return "Your list is empty."
        if len(open_tasks) == 1:
            return f"Just one thing on your list: {open_tasks[0]}."
        spoken = open_tasks[:_MAX_SPOKEN]
        rest = len(open_tasks) - len(spoken)
        if rest:
            joined = ", ".join(spoken) + f", and {rest} more"
        else:
            joined = ", ".join(spoken[:-1]) + f" and {spoken[-1]}"
        return f"You've got {len(open_tasks)} things on your list: {joined}."

    def tick(self, query: str) -> str:
        if not query.strip():
            return "Tick off which one?"
        try:
            lines = self._read()
            tasks = _tasks(lines)
            if not tasks:
                return "Your list is empty."
            found = _find(query, tasks)
            if not found:
                return f"There's nothing on your list about {query}."
            still_open = [t for t in found if not t[1]]
            if not still_open:
                return f"{found[0][2]} is already ticked off."
            if len(still_open) > 1:
                names = " or ".join(t[2] for t in still_open[:3])
                return f"That could be {names}. Which one?"
            index, _, text = still_open[0]
            lines[index] = f"- [x] {text}"
            self._write(lines)
        except OSError as exc:
            log.warning("Task not ticked", extra={"extra_fields": {"error": str(exc)}})
            return "I couldn't update your task list - the file wouldn't open."
        log.info("Task ticked off")
        return f"Ticked off {text}."

    def _read(self) -> list[str]:
        if not self._path.exists():
            return []
        return self._path.read_text(encoding="utf-8").splitlines()

    def _write(self, lines: list[str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp = self._path.with_name(self._path.name + ".tmp")
        temp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(temp, self._path)


def _tasks(lines: list[str]) -> list[tuple[int, bool, str]]:
    """(line index, ticked, text) for every checkbox line; everything else is his."""
    found = []
    for index, line in enumerate(lines):
        match = _LINE.match(line)
        if match:
            found.append((index, match.group("mark") != " ", match.group("text")))
    return found


def _find(query: str, tasks: list[tuple[int, bool, str]]) -> list[tuple[int, bool, str]]:
    """Exact, then contained, then every word present - so "tick off the invoice"
    reaches "send the invoice to Priya" without needing the whole sentence."""
    wanted = _ARTICLES.sub("", query.strip().lower())
    exact = [t for t in tasks if _ARTICLES.sub("", t[2].lower()) == wanted]
    if exact:
        return exact
    contained = [t for t in tasks if wanted in t[2].lower()]
    if contained:
        return contained
    words = set(wanted.split())
    return [t for t in tasks if words and words <= set(t[2].lower().split())]

"""Changing his files by voice (7.4). What he said, and what she says back.

Names are resolved against the same index 3.6 reads from, so "the budget
spreadsheet" means a file that exists, found the same way "read the budget
spreadsheet" would find it. A spoken *path* is never accepted - only names -
and a new name is cleaned until it cannot be anything but a name.

Resolution happens in the matcher, before the permission gate, so that the
readback the gate speaks names the real file ("Moving budget.xlsx from
Downloads to Documents") rather than repeating what he said. Anything that
cannot be resolved - no such file, three files that all fit, a folder outside
the roots - is refused before he is asked to confirm anything.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from pathlib import Path

from clio.capabilities import files
from clio.capabilities.web import _LEAD
from clio.core.fileops import FileOpError, FileOps
from clio.core.logging import get_logger

log = get_logger("clio.capabilities.filewrite")

_PUNCT = re.compile(r"[.!?,;:]+$")
_THE = re.compile(r"^(?:the|my|a|an)\s+")
_THAT = {"it", "that", "this", "that file", "this file", "that folder", "this folder",
         "that one", "this one"}

_PATTERNS: list[tuple[str, str]] = [
    ("folder", r"^(?:make|create|add) (?:a |an )?(?:new )?folder (?:called|named) (?P<name>.+?)"
               r"(?: (?:in|inside|on) (?P<where>.+))?$"),
    ("file", r"^(?:make|create|add) (?:a |an )?(?:new )?(?:empty )?file (?:called|named) "
             r"(?P<name>.+?)(?: (?:in|inside|on) (?P<where>.+))?$"),
    ("copy", r"^(?:copy|duplicate) (?P<what>.+?) (?:to|into|onto) (?P<where>.+)$"),
    ("move", r"^move (?P<what>.+?) (?:to|into|onto) (?P<where>.+)$"),
    ("rename", r"^rename (?P<what>.+?) (?:to|as) (?P<name>.+)$"),
    ("delete", r"^(?:delete|bin|trash|throw away|get rid of|recycle) (?P<what>.+)$"),
    ("undo", r"^(?:undo(?: that| it| the last one| the last thing)?|take that back)$"),
]
_COMPILED = [(kind, re.compile(p)) for kind, p in _PATTERNS]

# Words that follow these verbs and belong to other capabilities.
_NOT_FILES = {"reminder", "reminders", "my reminder", "my reminders", "the timer",
              "timer", "the draft", "draft", "that email", "the email"}


_DIRECTORY_DEPTH = 4


def _find_directory(name: str, roots: tuple[Path, ...]) -> Path | None:
    """A folder by name, empty or not, a few levels down. The shallowest wins:
    "invoices" most likely means the one near the top, not one buried in a
    project's build output."""
    hits: list[Path] = []
    for root in roots:
        base = len(Path(root).parts)
        for folder, dirs, _ in os.walk(root):
            dirs[:] = [d for d in dirs if d not in files._SKIP_DIRS and not d.startswith(".")]
            if len(Path(folder).parts) - base >= _DIRECTORY_DEPTH:
                dirs[:] = []
            hits += [Path(folder) / d for d in dirs if d.lower() == name]
    return min(hits, key=lambda p: len(p.parts)) if hits else None


@dataclass(frozen=True)
class WriteRequest:
    kind: str                  # "folder", "file", "copy", "rename", "move", "delete", "undo"
    source: Path | None = None
    folder: Path | None = None
    name: str = ""
    refusal: str = ""          # said instead, when it cannot be carried out


def parse_write(text: str) -> tuple[str, dict] | None:
    """The shape of the sentence only. Nothing is looked up here."""
    spoken = _PUNCT.sub("", " ".join(text.strip().lower().split()))
    spoken = _LEAD.sub("", spoken).strip()
    for kind, pattern in _COMPILED:
        found = pattern.match(spoken)
        if found is None:
            continue
        parts = {k: (v or "").strip() for k, v in found.groupdict().items()}
        if parts.get("what", "") in _NOT_FILES:
            return None
        # A spoken path is never resolved - the transcriber will produce one
        # eventually, and "C colon backslash Windows" must not reach a delete.
        if any(files._LOOKS_LIKE_PATH.search(v) for k, v in parts.items() if k != "name"):
            return None
        return kind, parts
    return None


class FileWriter:
    def __init__(self, roots: tuple[Path, ...]):
        self._roots = tuple(roots)
        self._ops = FileOps(self._roots)
        # What "it" means: the last file she found or changed.
        self._last: Path | None = None

    def note(self, path: Path) -> None:
        """Called by the read side too, so "find the budget sheet... move it to
        Desktop" works across the two capabilities."""
        self._last = path

    # --- turning words into paths ---

    def resolve(self, text: str) -> WriteRequest | None:
        parsed = parse_write(text)
        if parsed is None:
            return None
        kind, parts = parsed
        if kind == "undo":
            return WriteRequest("undo")
        if not self._roots:
            return WriteRequest(kind, refusal=(
                "I haven't been given any folders to work in. Add them under files in "
                "the config."))

        source = None
        if kind in ("copy", "move", "rename", "delete"):
            source, problem = self._find(parts["what"])
            if source is None:
                return WriteRequest(kind, refusal=problem)

        folder = None
        if kind in ("copy", "move") or (kind in ("folder", "file") and parts.get("where")):
            folder = self._folder(parts["where"])
            if folder is None:
                return WriteRequest(kind, refusal=f"I can't find a folder called {parts['where']}.")
        elif kind in ("folder", "file"):
            # No place given: the first configured root, and she says where.
            folder = self._roots[0]

        return WriteRequest(kind, source=source, folder=folder, name=parts.get("name", ""))

    def _find(self, what: str) -> tuple[Path | None, str]:
        wanted = _THE.sub("", what.strip())
        if wanted in _THAT:
            if self._last is not None and self._last.exists():
                return self._last, ""
            return None, "I'm not sure what you mean by that. Say the file's name."
        index = files._index(self._roots)
        matches = [p for p in files._by_name(wanted, index) if p.exists()]
        if not matches:
            folder = files._find_folder(wanted, self._roots, index)
            if folder is not None and folder not in self._roots:
                return folder, ""
            return None, f"I can't find anything called {wanted}."
        exact = [p for p in matches if p.stem.lower() == wanted]
        if len(exact) == 1:
            return exact[0], ""
        if len(matches) > 1:
            names = ", ".join(f"{p.name} in {p.parent.name}" for p in matches[:3])
            return None, f"That could be {names}. Say which one."
        return matches[0], ""

    def _folder(self, where: str) -> Path | None:
        wanted = _THE.sub("", where.strip())
        if wanted in _THAT and self._last is not None:
            return self._last if self._last.is_dir() else self._last.parent
        found = files._find_folder(wanted, self._roots, files._index(self._roots))
        # The read side knows folders only through the files inside them, so an
        # empty one - including one she has just made - is invisible to it. A
        # destination is very often empty, so look at the folders themselves.
        return found or _find_directory(wanted, self._roots)

    # --- what the permission gate reads back ---

    @staticmethod
    def describe(request: WriteRequest) -> str:
        source = request.source
        if request.kind == "rename":
            return f"Renaming {source.name}, in {source.parent.name}, to {request.name}"
        if request.kind == "move":
            return f"Moving {source.name} from {source.parent.name} to {request.folder.name}"
        if request.kind == "delete":
            return f"Putting {source.name}, from {source.parent.name}, in the Recycle Bin"
        return ""

    # --- doing it ---

    async def run(self, request: WriteRequest) -> str:
        if request.refusal:
            return request.refusal
        if request.kind == "undo":
            spoken = await asyncio.to_thread(self._ops.undo)
            files.forget_index()
            return spoken
        try:
            spoken = await asyncio.to_thread(self._apply, request)
        except FileOpError as exc:
            return f"I can't do that - {exc}."
        except OSError as exc:
            log.warning("File operation failed",
                        extra={"extra_fields": {"kind": request.kind, "error": str(exc)}})
            return f"Windows wouldn't let me: {exc.strerror or exc}."
        # The read side caches its index for a minute; without this she could
        # not find the folder she has just made.
        files.forget_index()
        return spoken

    def _apply(self, request: WriteRequest) -> str:
        ops, source, folder = self._ops, request.source, request.folder
        if request.kind == "folder":
            made = ops.create_folder(folder, request.name)
            self._last = made
            return f"Made a folder called {made.name} in {folder.name}."
        if request.kind == "file":
            made = ops.create_file(folder, request.name)
            self._last = made
            return f"Made {made.name} in {folder.name}."
        if request.kind == "copy":
            made = ops.copy(source, folder)
            self._last = made
            renamed = (f", as {made.name} because the name was taken"
                       if made.name != source.name else "")
            return f"Copied {source.name} to {folder.name}{renamed}."
        if request.kind == "rename":
            made = ops.rename(source, request.name)
            self._last = made
            return f"Renamed it to {made.name}."
        if request.kind == "move":
            made = ops.move(source, folder)
            self._last = made
            renamed = (f", as {made.name} because the name was taken"
                       if made.name != source.name else "")
            return f"Moved {source.name} to {folder.name}{renamed}."
        ops.recycle(source)
        self._last = None
        return f"{source.name} is in the Recycle Bin. Say undo if that was wrong."

"""Read-only lookups over the user's own files — never writes, so FREE.

Bounded to configured roots (not the whole drive), and never resolves a path
out of the utterance (names are matched against the walk, same rule as
launching). Content search is deadline-bounded, since there can be tens of
thousands of files: "I got through this much" beats a ten-second silence.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from clio.core.logging import get_logger

log = get_logger("clio.files")

# Readable text. Anything else is found by name but never opened.
_TEXT = {
    ".txt", ".md", ".py", ".json", ".toml", ".yaml", ".yml", ".csv", ".log",
    ".ini", ".cfg", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".sql", ".sh", ".rst",
}

_SKIP_DIRS = {"node_modules", "__pycache__", "venv", ".venv", "dist", "build", "site-packages"}

_MAX_READ_BYTES = 200_000
_MAX_SEARCH_BYTES = 1_000_000
_SEARCH_DEADLINE_S = 4.0
_MAX_HITS = 3
_MAX_LISTED = 5
_SPOKEN_CHARS = 700


@dataclass(frozen=True)
class Request:
    kind: str
    value: str
    matches: tuple[Path, ...] = ()


_PATTERNS: list[tuple[str, str]] = [
    ("search", r"(?:which|what) files?\s+(?:mentions?|talks? about|contains?|says?)\s+(?P<value>.+)|"
               r"search (?:my |the )?files? for (?P<value2>.+)|"
               r"find (?:files?|anything) (?:that )?(?:mentions?|contains?) (?P<value3>.+)"),
    ("summarise", r"summari[sz]e (?:the |my )?(?P<value>.+)"),
    ("read", r"read (?:me |out )?(?:the |my )?(?P<value>.+)"),
    ("list", r"(?:what'?s|what is) in (?:my |the )?(?P<value>[\w \-]+)|"
             r"list (?:my |the )?(?P<value2>[\w \-]+?)(?: folder| directory)?$"),
    ("find", r"(?:find|where(?:'?s| is)|do i have)\s+(?:a |the |my )?(?:files? )?"
             r"(?:called |named |about )?(?P<value>.+)"),
]

_COMPILED = [(kind, re.compile(p)) for kind, p in _PATTERNS]
_STRIP = re.compile(r"[.!?,;:]+$")

# A spoken path is never resolved — the transcriber will produce one eventually.
_LOOKS_LIKE_PATH = re.compile(r"[/\\]|\.\.|^[a-z] colon\b|^[a-z]:")


def _normalise(text: str) -> str:
    return " ".join(_STRIP.sub("", text.strip().lower()).split())


_INDEX_TTL_S = 60.0
_index_cache: tuple[float, tuple[Path, ...]] = (0.0, ())


def _index(roots: tuple[Path, ...]) -> tuple[Path, ...]:
    """Every path under the roots, rebuilt at most once a minute. The matcher
    needs it: a name matching nothing must fall through to conversation, which
    it can only know by looking.

    ponytail: one global cache keyed on nothing — roots come from config and
    don't change within a run. Key it if they ever become dynamic.
    """
    global _index_cache
    age, cached = _index_cache
    if cached and time.monotonic() - age < _INDEX_TTL_S:
        return cached
    found = tuple(_walk(list(roots)))
    _index_cache = (time.monotonic(), found)
    log.info("File index built", extra={"extra_fields": {"files": len(found)}})
    return found


def parse_file_request(text: str, roots: tuple[Path, ...] = ()) -> Request | None:
    lowered = _normalise(text)
    for kind, pattern in _COMPILED:
        found = pattern.search(lowered)
        if found is None:
            continue
        value = next(
            (v for v in (found.groupdict().get(g) for g in ("value", "value2", "value3")) if v),
            "",
        ).strip()
        if not value or _LOOKS_LIKE_PATH.search(value):
            return None
        if kind == "search":
            return Request(kind=kind, value=value)
        if not roots:
            # Nothing configured: still claim it, so she says she has nowhere
            # to look rather than improvising.
            return Request(kind=kind, value=value)

        everything = _index(roots)
        if kind == "list":
            folder = _find_folder(value, roots, everything)
            return None if folder is None else Request(kind, value, (folder,))

        matches = _by_name(value, everything)
        # An unmatched name is not a file request at all.
        return Request(kind, value, matches) if matches else None
    return None


def _walk(roots: list[Path], deadline: float | None = None):
    """Bounded walk, skipping hidden dirs and build output (node_modules etc.)."""
    for root in roots:
        if not root.is_dir():
            continue
        for folder, dirs, names in os.walk(root):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
            for name in names:
                yield Path(folder) / name
            if deadline is not None and time.monotonic() > deadline:
                return


def _where(path: Path) -> str:
    """The containing folder, not the full path — a path read aloud is unusable."""
    return path.parent.name or str(path.parent)


def _spoken_name(path: Path) -> str:
    return f"{path.stem}, in {_where(path)}"


def _by_name(query: str, everything: tuple[Path, ...]) -> tuple[Path, ...]:
    words = query.split()
    hits = [p for p in everything if all(w in p.stem.lower() for w in words)]
    # Most recently touched first — usually the one meant.
    hits.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0.0, reverse=True)
    return tuple(hits[:20])


def _find_folder(name: str, roots: tuple[Path, ...], everything: tuple[Path, ...]) -> Path | None:
    for root in roots:
        if name in root.name.lower():
            return root
    seen = {p.parent for p in everything}
    return next((p for p in sorted(seen, key=lambda d: len(str(d))) if p.name.lower() == name), None)


def _read_text(path: Path, limit: int) -> str:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        return handle.read(limit)


def _search_contents(query: str, roots: list[Path]) -> tuple[list[Path], int]:
    deadline = time.monotonic() + _SEARCH_DEADLINE_S
    needle = query.lower()
    hits: list[Path] = []
    looked = 0
    for path in _walk(roots, deadline):
        if path.suffix.lower() not in _TEXT:
            continue
        try:
            if path.stat().st_size > _MAX_SEARCH_BYTES:
                continue
            looked += 1
            if needle in _read_text(path, _MAX_SEARCH_BYTES).lower():
                hits.append(path)
                if len(hits) >= _MAX_HITS:
                    break
        except OSError:
            continue
        if time.monotonic() > deadline:
            break
    return hits, looked


def _list_folder(root: Path) -> str:
    entries = sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    if not entries:
        return f"{root.name} is empty."
    newest = ", ".join(p.stem for p in entries[:_MAX_LISTED])
    if len(entries) > _MAX_LISTED:
        return f"{len(entries)} things in {root.name}. Most recent: {newest}."
    return f"In {root.name}: {newest}."


def lookup(request: Request, roots: tuple[Path, ...]) -> tuple[str, str]:
    """Returns what to say, plus the file text when there's something to
    summarise. Blocking — the caller runs it on a thread. Names are already
    resolved in `request.matches` (the matcher looked them up)."""
    if not roots:
        return ("I don't have anywhere to look yet. Add the folders you want me to see "
                "under files in the config, and I'll stay inside them.", "")

    if request.kind == "list":
        return _list_folder(request.matches[0]), ""

    if request.kind == "search":
        hits, looked = _search_contents(request.value, list(roots))
        if not hits:
            return f"Nothing mentioning {request.value} in the {looked} files I got through.", ""
        return f"Found {request.value} in " + ", ".join(_spoken_name(p) for p in hits) + ".", ""

    matches = request.matches
    if request.kind == "find":
        if len(matches) == 1:
            return f"Yes - {_spoken_name(matches[0])}.", ""
        shown = ", ".join(_spoken_name(p) for p in matches[:_MAX_HITS])
        more = f", and {len(matches) - _MAX_HITS} others" if len(matches) > _MAX_HITS else ""
        return f"{len(matches)} of them: {shown}{more}.", ""

    path = matches[0]
    if path.suffix.lower() not in _TEXT:
        return (f"{path.stem} isn't something I can read out - "
                f"it's a {path.suffix.lstrip('.')} file."), ""

    try:
        text = _read_text(path, _MAX_READ_BYTES)
    except OSError as exc:
        log.warning("Read failed", extra={"extra_fields": {"file": path.name, "error": str(exc)}})
        return f"I found {path.stem} but couldn't open it.", ""

    if request.kind == "summarise":
        # The text goes back to the caller, which owns the LLM. Finding and
        # reading stays deterministic; only the compression is a model's job.
        return "", text

    spoken = " ".join(text[:_SPOKEN_CHARS].split())
    tail = "" if len(text) <= _SPOKEN_CHARS else " That's the start of it - want the rest summarised?"
    return f"{spoken}{tail}", ""


async def look_up(request: Request, roots: tuple[Path, ...]) -> tuple[str, str]:
    log.info(
        "File lookup",
        extra={"extra_fields": {"kind": request.kind, "value": request.value, "roots": len(roots)}},
    )
    return await asyncio.to_thread(lookup, request, roots)

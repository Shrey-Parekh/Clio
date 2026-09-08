"""Opening things by the name he'd actually say - apps, folders, URLs, projects.

The first capability that does something to the machine rather than reporting
on it, so what it will agree to open is deliberately bounded:

- Shortcuts he configured himself, by name.
- Anything already in his Start Menu, which is the list of things he has
  installed on purpose.
- A handful of well-known folders, and a spoken URL.

It never resolves a path out of the utterance. "Open C colon backslash..." is
not a thing she can be talked into, because the transcriber will eventually
mishear something into that slot, and running whatever came out of it is a
different class of problem from opening the wrong app.

Nothing here is destructive - the worst outcome is a window he closes again -
so it stays FREE. Asking "shall I?" before every launch would make the
capability worse than the Start Menu it replaces.
"""

from __future__ import annotations

import difflib
import functools
import os
import re
from dataclasses import dataclass
from pathlib import Path

from clio.core.logging import get_logger

log = get_logger("clio.launch")

_OPEN = re.compile(
    r"^(?:please\s+|hey\s+)?(?:can you\s+|could you\s+)?"
    r"(?:open|launch|start|run|fire up|pull up|bring up)\s+"
    r"(?:up\s+)?(?P<article>the\s+|my\s+|a\s+|an\s+)?(?P<target>.+)$"
)

# A long phrase after "open" is far more likely to be conversation than a
# request to launch something - "open up about what's bothering you" is not a
# missing application.
_MAX_TARGET_WORDS = 5

# Claiming "I couldn't find that" is only safe for a short name. "Open up about
# what's bothering you" resolved to nothing and said so, which is a worse
# answer than letting it fall through to conversation - so past this length an
# unmatched phrase is treated as speech, not as a missing application.
_MAX_UNKNOWN_WORDS = 2

_SPOKEN_URL = re.compile(
    r"^(?P<host>[\w-]+(?:\.[\w-]+)*)\s*(?:\.|\s+dot\s+)\s*(?P<tld>com|org|net|io|dev|ai|co|uk|in)$"
)

_MATCH_CUTOFF = 0.78
_STRIP = re.compile(r"[.!?,;:]+$")

# Windows' own folder names, and what he'd say for them.
_FOLDERS = ("downloads", "documents", "desktop", "pictures", "music", "videos")


@dataclass(frozen=True)
class Target:
    """`kind` exists for the sentence spoken back, so "opening Spotify" does
    not come out as "opening your Spotify folder"."""

    name: str
    path: str
    kind: str


def _normalise(text: str) -> str:
    return " ".join(_STRIP.sub("", text.strip().lower()).split())


def _start_menu_dirs() -> list[Path]:
    roots = []
    for var in ("ProgramData", "APPDATA"):
        base = os.environ.get(var)
        if base:
            roots.append(Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    return [r for r in roots if r.is_dir()]


@functools.lru_cache(maxsize=1)
def installed_apps() -> dict[str, str]:
    """Start Menu shortcut name -> shortcut path.

    Cached for the life of the process: scanning two trees on every "open
    something" would be paid on the hot path, and installing an app mid-session
    is rare enough to be worth a restart. Shortcuts are opened as shortcuts -
    Windows resolves the target, so nothing here has to parse a .lnk.
    """
    apps: dict[str, str] = {}
    for root in _start_menu_dirs():
        for link in root.rglob("*.lnk"):
            # First one wins: the per-user Start Menu is scanned second and
            # would otherwise replace a machine-wide entry of the same name.
            apps.setdefault(_normalise(link.stem), str(link))
    return apps


def _best_app(query: str, apps: dict[str, str]) -> str | None:
    names = list(apps)
    if query in names:
        return query
    # Substring before fuzzy: "chrome" is contained in "google chrome" but is
    # only about 0.6 similar to it. Shortest wins, so "word" prefers
    # "microsoft word" over a longer accidental container.
    contained = sorted((n for n in names if query in n), key=len)
    if contained:
        return contained[0]
    close = difflib.get_close_matches(query, names, n=1, cutoff=_MATCH_CUTOFF)
    return close[0] if close else None


def resolve(text: str, shortcuts: dict[str, str] | None = None) -> Target | None:
    """None means this was not an open request at all, and it falls through to
    conversation. A request that *was* one but matched nothing comes back as
    kind "unknown", so she says she couldn't find it rather than handing the
    model a chance to improvise a confirmation for something that never ran.
    """
    match = _OPEN.match(_normalise(text))
    if match is None:
        return None

    target = _normalise(match.group("target"))
    if not target or len(target.split()) > _MAX_TARGET_WORDS:
        return None

    # Both with and without the article, longer first: stripping "the" off
    # "open the roadmap" makes a shortcut he named "the roadmap" unreachable,
    # and he names his own shortcuts however he says them.
    article = _normalise(match.group("article") or "")
    spoken = [f"{article} {target}".strip(), target] if article else [target]
    for name, path in (shortcuts or {}).items():
        if _normalise(name) in spoken:
            return Target(name=name, path=path, kind="shortcut")

    url = _SPOKEN_URL.match(target)
    if url:
        host = f"{url.group('host').rstrip('.')}.{url.group('tld')}"
        return Target(name=host, path=f"https://{host}", kind="site")

    folder = target.removesuffix(" folder")
    if folder in _FOLDERS:
        path = Path.home() / folder.capitalize()
        if path.is_dir():
            return Target(name=folder, path=str(path), kind="folder")

    apps = installed_apps()
    found = _best_app(target, apps)
    if found:
        return Target(name=found, path=apps[found], kind="app")

    if len(target.split()) > _MAX_UNKNOWN_WORDS:
        return None
    return Target(name=target, path="", kind="unknown")


def open_target(target: Target) -> str:
    if target.kind == "unknown":
        return f"I couldn't find anything called {target.name}."

    try:
        # Windows' own "open with whatever handles this", which covers a
        # shortcut, a folder and a URL without three code paths.
        os.startfile(target.path)  # noqa: S606
    except OSError as exc:
        log.warning(
            "Launch failed",
            extra={"extra_fields": {"target": target.name, "path": target.path, "error": str(exc)}},
        )
        return f"I found {target.name} but Windows wouldn't open it."

    log.info("Opened", extra={"extra_fields": {"target": target.name, "kind": target.kind}})
    if target.kind == "folder":
        return f"Opening your {target.name}."
    return f"Opening {target.name}."

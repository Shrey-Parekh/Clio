"""Open apps, folders, URLs and configured shortcuts by spoken name.

Bounded to configured shortcuts, the Start Menu, a few well-known folders, and
a spoken URL. It never resolves a path out of the utterance — the transcriber
would eventually mishear one, and running that is a different class of problem.
Worst case is a window closed again, so it stays FREE.
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

# A long phrase after "open" is more likely conversation than a launch —
# "open up about what's bothering you" is not a missing app.
_MAX_TARGET_WORDS = 5

# Past this length, an unmatched phrase is treated as speech, not a missing app:
# "I couldn't find that" is only a safe answer for a short name.
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
    """Start Menu shortcut name -> shortcut path. Cached for the process life
    (a new app mid-session is rare enough to be worth a restart)."""
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
    # And the other way round. Found live, 2026-10-02: "open Microsoft Word"
    # opened Microsoft Edge. Word is installed as plain "word", which is not
    # found by looking for his words inside app names. An app whose whole name
    # appears in what he said is the app; longest wins, so "visual studio code"
    # beats "code".
    words = query.split()
    inside = sorted(
        (n for n in names if len(n) >= 3 and _contains_words(words, n.split())),
        key=len, reverse=True)
    if inside:
        return inside[0]
    for name in difflib.get_close_matches(query, names, n=3, cutoff=_MATCH_CUTOFF):
        if not _only_shares_a_prefix(words, name.split()):
            return name
    return None


def _contains_words(words: list[str], name: list[str]) -> bool:
    """Whether `name` appears in `words` as a run of whole words."""
    return any(words[i:i + len(name)] == name for i in range(len(words) - len(name) + 1))


def _only_shares_a_prefix(words: list[str], name: list[str]) -> bool:
    """"microsoft word" and "microsoft edge" are 71% alike as strings and not
    alike at all as apps: everything they share is the vendor. A fuzzy match
    must also be close in the words that differ."""
    shared = set(words) & set(name)
    if not shared:
        return False
    rest_said = " ".join(w for w in words if w not in shared)
    rest_name = " ".join(w for w in name if w not in shared)
    if not rest_said or not rest_name:
        return False
    return difflib.SequenceMatcher(None, rest_said, rest_name).ratio() < _MATCH_CUTOFF


def resolve(text: str, shortcuts: dict[str, str] | None = None) -> Target | None:
    """None means not an open request (falls through to conversation). One that
    was but matched nothing returns kind "unknown", so she says she couldn't
    find it rather than letting the model improvise."""
    match = _OPEN.match(_normalise(text))
    if match is None:
        return None

    target = _normalise(match.group("target"))
    if not target or len(target.split()) > _MAX_TARGET_WORDS:
        return None

    # Try with and without the article: a shortcut named "the roadmap" must
    # stay reachable, but "the" must also be strippable.
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


def describe_target(target: Target) -> str:
    """What opening it will do, for a readback. An unknown target says so,
    rather than promising a launch that will not happen."""
    if target.kind == "unknown":
        return f"I don't know anything called {target.name} to open"
    return f"Opening {target.name}"


def open_target(target: Target) -> str:
    if target.kind == "unknown":
        return f"I couldn't find anything called {target.name}."

    try:
        # Windows' own handler dispatch — one path for shortcut, folder and URL.
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

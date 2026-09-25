"""Installing, updating and uninstalling apps by voice, through winget (7.8).

"Install 7-Zip", "update Chrome", "uninstall VLC", "what needs updating",
"is VLC installed". Decided with him:

- **The package is resolved before the permission gate**, so the readback names
  the exact id: "Installing 7-Zip, id 7zip.7zip". Searching "7zip" also finds
  NanaZip and 7-Zip ZS; a similar name must never be what gets installed.
- **A clear match, or he picks.** One package that is exactly what he said is
  read back. Otherwise she reads the top three and he says "the second one".
- **One app at a time.** "Update everything" is refused, by his choice - 33
  updates at once is not something to wave through with one yes.
- **Uninstalling always asks**, and says it can't be undone from here.
- Checking - what needs updating, whether something is installed - is free.

ponytail: resolving runs winget inside the matcher, which blocks the event
loop for the 2-5 seconds a search takes. Acceptable because nothing else is
happening mid-turn; move it into the handler if that ever stalls audio.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from clio.capabilities.web import _LEAD
from clio.core import jobs, winget
from clio.core.logging import get_logger
from clio.core.winget import Package, WingetError

log = get_logger("clio.capabilities.software")

_CHOICES_S = 120.0      # how long "the second one" still means this list
_SPOKEN_UPDATES = 3
_WORDS = ["one", "two", "three"]
_PUNCT = re.compile(r"[.!?,]+$")
_EVERYTHING = {"everything", "all", "all my apps", "all apps", "all of them",
               "all my programs", "all programs", "all my software", "everything on my pc",
               "everything on my computer", "my apps", "my programs", "my software"}
_ORDINALS = {"first": 0, "one": 0, "1": 0, "second": 1, "two": 1, "2": 1,
             "third": 2, "three": 2, "3": 2}

_PATTERNS = [
    ("updates", re.compile(r"^(?:what|which) (?:apps? |programs? |software )?(?:needs?|have|has) "
                           r"(?:an? )?(?:updat(?:e|es|ing))$|^(?:are there )?any (?:app )?updates$|"
                           r"^what can i update$")),
    ("check", re.compile(r"^(?:is|have i got|do i have) (?P<name>.+?) installed$")),
    ("choose", re.compile(r"^(?:install |update |uninstall )?(?:the )?(?:number |option )?"
                          r"(?P<which>first|second|third|one|two|three|1|2|3)(?: one)?$")),
    ("install", re.compile(r"^install (?P<name>.+)$")),
    ("update", re.compile(r"^(?:update|upgrade) (?P<name>.+)$")),
    ("uninstall", re.compile(r"^uninstall (?P<name>.+)$")),
]


@dataclass(frozen=True)
class SoftwareRequest:
    action: str = ""                 # install / update / uninstall, when it will change something
    package: Package | None = None
    said: str = ""                   # the answer, for anything that only reads
    listing: str = ""                # the full list, for the chat window
    choices: list[Package] = field(default_factory=list)

    @property
    def changes(self) -> bool:
        return self.package is not None


def parse_software(text: str) -> tuple[str, str] | None:
    spoken = _PUNCT.sub("", " ".join(text.strip().lower().split()))
    spoken = _LEAD.sub("", spoken).strip()
    for kind, pattern in _PATTERNS:
        found = pattern.match(spoken)
        if found:
            groups = found.groupdict()
            name = re.sub(r"^the ", "", (groups.get("name") or groups.get("which") or ""))
            return kind, re.sub(r" (?:app|program|application)$", "", name).strip()
    return None


class Software:
    def __init__(self, runner: jobs.JobRunner):
        self._runner = runner
        self._choices: tuple[str, list[Package], float] | None = None
        self._last: tuple[str, SoftwareRequest | None] = ("", None)

    def resolve(self, text: str) -> SoftwareRequest | None:
        """Cached for the one sentence, because two matchers ask about it and
        each question is a winget run."""
        if self._last[0] == text:
            return self._last[1]
        request = self._resolve(text)
        self._last = (text, request)
        return request

    def _resolve(self, text: str) -> SoftwareRequest | None:
        parsed = parse_software(text)
        if parsed is None:
            return None
        kind, name = parsed
        if kind in ("update", "uninstall") and name.startswith("my ") and name not in _EVERYTHING:
            # Found live: "update my notes" became Microsoft Sticky Notes, after
            # seven seconds of winget. "My" is his own things - notes, tasks -
            # never an app, so it is left for them without asking winget.
            return None
        try:
            if kind == "updates":
                return self._updates()
            if kind == "choose":
                return self._chosen(name)
            if kind in ("update", "install") and name in _EVERYTHING:
                return SoftwareRequest(said=(
                    "I only update one app at a time. Say update and its name - "
                    "and what needs updating lists them."))
            if kind == "check":
                found = winget.clear_installed(name, winget.installed(name))
                return SoftwareRequest(said=(
                    f"Yes, {found.name} is installed, version {found.version}." if found
                    else f"I can't see {name} installed."))
            if kind == "install":
                return self._install(name)
            return self._change_installed(kind, name)
        except WingetError as exc:
            return SoftwareRequest(said=f"I couldn't ask winget: {exc}.")

    def _updates(self) -> SoftwareRequest:
        pending = winget.upgrades()
        if not pending:
            return SoftwareRequest(said="Everything's up to date.")
        names = [p.name for p in pending]
        head = ", ".join(names[:_SPOKEN_UPDATES])
        more = len(names) - _SPOKEN_UPDATES
        said = (f"{len(names)} apps have updates: {head}"
                + (f" and {more} more. The full list is in the window." if more > 0 else "."))
        listing = "\n".join(f"{p.name}  {p.version} -> {p.available}" for p in pending)
        return SoftwareRequest(said=said, listing=listing)

    def _install(self, name: str) -> SoftwareRequest:
        found = winget.search(name)
        if not found:
            return SoftwareRequest(said=f"winget doesn't have anything called {name}.")
        clear = winget.clear_install(name, found)
        if clear is not None:
            return SoftwareRequest(action="install", package=clear)
        return self._offer("install", found)

    def _change_installed(self, kind: str, name: str) -> SoftwareRequest | None:
        rows = winget.upgrades() if kind == "update" else winget.installed(name)
        clear = winget.clear_installed(name, rows)
        if clear is not None:
            return SoftwareRequest(action=kind, package=clear)
        close = [p for p in rows if winget._norm(name) in winget._norm(p.name)]
        if len(close) > 1:
            return self._offer(kind, close)
        if kind == "update":
            # Not claimed: "update my notes" is not about software, and an app
            # with nothing to update has nothing to confirm.
            installed = winget.clear_installed(name, winget.installed(name))
            if installed is None:
                return None
            return SoftwareRequest(said=f"{installed.name} is already up to date.")
        return None

    def _offer(self, kind: str, found: list[Package]) -> SoftwareRequest:
        top = found[:3]
        self._choices = (kind, top, time.monotonic() + _CHOICES_S)
        listed = "; ".join(f"{_WORDS[i]}, {p.name}" for i, p in enumerate(top))
        return SoftwareRequest(said=f"There are a few. {listed}. Which one?", choices=top)

    def _chosen(self, which: str) -> SoftwareRequest | None:
        if self._choices is None or time.monotonic() > self._choices[2]:
            return None
        kind, top, _ = self._choices
        index = _ORDINALS.get(which)
        if index is None or index >= len(top):
            return None
        self._choices = None
        return SoftwareRequest(action=kind, package=top[index])

    @staticmethod
    def describe(request: SoftwareRequest) -> str:
        """What the gate reads back: the name and the exact id."""
        p = request.package
        if request.action == "install":
            return (f"Installing {p.name}, id {p.id}, from winget, "
                    "accepting its licence terms")
        if request.action == "update":
            return f"Updating {p.name} from {p.version} to {p.available}, id {p.id}"
        return f"Uninstalling {p.name}, id {p.id}. That can't be undone from here"

    async def run(self, request: SoftwareRequest) -> str:
        p = request.package
        argv = {"install": winget.install_args, "update": winget.upgrade_args,
                "uninstall": winget.uninstall_args}[request.action](p)
        verb = {"install": "Installing", "update": "Updating", "uninstall": "Uninstalling"}
        try:
            job = await asyncio.to_thread(
                self._runner.start, f"{request.action} {p.name}", argv, Path.home())
        except OSError as exc:
            return f"Windows wouldn't start winget: {exc.strerror or exc}."
        self._runner.watch(job)
        log.info("Software change started", extra={"extra_fields": {
            "action": request.action, "id": p.id}})
        return (f"{verb[request.action]} {p.name}. If Windows asks for permission, "
                "that's the installer. I'll tell you when it's done.")

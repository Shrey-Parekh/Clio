"""Asking winget what exists and what is installed (7.8).

Read-only: this module searches and lists. Installing, updating and
uninstalling are argument lists built here and run by the job runner, so they
get status, "stop it" and an announcement like any other long job.

winget prints fixed-width tables. Columns are cut at the header's offsets
rather than split on spaces, because names have spaces in them ("VLC media
player") and ids never do.

Only winget's own catalogue is searched for installs - never a URL, a local
file, or the Store. Source agreements are his to accept once; nothing here
accepts them for him.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

from clio.core.logging import get_logger

log = get_logger("clio.winget")

_TIMEOUT_S = 60
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class WingetError(Exception):
    """Something to say, not a traceback."""


@dataclass(frozen=True)
class Package:
    name: str
    id: str
    version: str = ""
    available: str = ""
    source: str = ""
    match: str = ""


def run(args: list[str]) -> str:
    """winget's output. Swapped out by the tests, which never touch winget."""
    try:
        done = subprocess.run(
            ["winget", *args, "--disable-interactivity"], capture_output=True,
            encoding="utf-8", errors="replace", timeout=_TIMEOUT_S,
            creationflags=_NO_WINDOW)
    except FileNotFoundError as exc:
        raise WingetError("winget isn't installed on this machine") from exc
    except subprocess.TimeoutExpired as exc:
        raise WingetError("winget took more than a minute to answer") from exc
    text = done.stdout or ""
    if re.search(r"source agreements|agree to all the source", text, re.I):
        raise WingetError("winget wants you to accept its source terms first. "
                          "Run winget list in a terminal once and say yes")
    return text


def parse_table(text: str) -> list[Package]:
    """The rows of winget's table. Spinner frames are written with carriage
    returns before it, so only what follows the last one on a line counts."""
    lines = [line.rsplit("\r", 1)[-1].rstrip() for line in text.splitlines()]
    for index, line in enumerate(lines[:-1]):
        if line.startswith("Name") and " Id " in line and set(lines[index + 1]) == {"-"}:
            header = line
            body = lines[index + 2:]
            break
    else:
        return []

    columns = [(m.group(), m.start()) for m in re.finditer(r"\S+", header)]
    rows = []
    for line in body:
        if not line.strip():
            break
        fields = {}
        for position, (title, start) in enumerate(columns):
            end = columns[position + 1][1] if position + 1 < len(columns) else None
            fields[title] = line[start:end].strip()
        if not fields.get("Id") or " " in fields["Id"]:
            break                                    # "33 upgrades available."
        rows.append(Package(
            name=fields.get("Name", ""), id=fields["Id"], version=fields.get("Version", ""),
            available=fields.get("Available", ""), source=fields.get("Source", ""),
            match=fields.get("Match", "")))
    return rows


def search(query: str) -> list[Package]:
    return parse_table(run(["search", query, "--source", "winget", "--count", "12"]))


def installed(query: str) -> list[Package]:
    return parse_table(run(["list", query]))


def upgrades() -> list[Package]:
    return parse_table(run(["upgrade"]))


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def clear_install(query: str, found: list[Package]) -> Package | None:
    """The one package he plainly meant: its name or the last part of its id
    is exactly what he said. "7zip" is 7-Zip; it is not NanaZip or 7-Zip ZS,
    which also come up.

    Not winget's "moniker": publishers choose their own, and live, "zip" was
    the moniker of LiteMonitor - a system monitor - so "install zip" read back
    the wrong app with confidence."""
    wanted = _norm(query)
    hits = {p.id: p for p in found
            if wanted in (_norm(p.name), _norm(p.id.split(".")[-1]))}
    return next(iter(hits.values())) if len(hits) == 1 else None


def clear_installed(query: str, rows: list[Package]) -> Package | None:
    """Among what is installed, one exact name, or else one name that
    contains his words. "Chrome" is Google Chrome, if only one thing is."""
    wanted = _norm(query)
    exact = [p for p in rows if wanted in (_norm(p.name), _norm(p.id))]
    if len(exact) == 1:
        return exact[0]
    loose = [p for p in rows if wanted and wanted in _norm(p.name)]
    return loose[0] if len(loose) == 1 else None


def install_args(package: Package) -> list[str]:
    return ["winget", "install", "--id", package.id, "--exact", "--source", "winget",
            "--silent", "--accept-package-agreements", "--disable-interactivity"]


def upgrade_args(package: Package) -> list[str]:
    return ["winget", "upgrade", "--id", package.id, "--exact",
            "--silent", "--accept-package-agreements", "--disable-interactivity"]


def uninstall_args(package: Package) -> list[str]:
    return ["winget", "uninstall", "--id", package.id, "--exact",
            "--silent", "--disable-interactivity"]

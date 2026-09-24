"""Running a command by voice (7.5). "Run pip install requests in ewaste",
"what's the git status of clio".

`clio/core/shell.py` decides whether the words may run; this decides where, and
what to say. Everything is resolved in the matcher, before the permission gate,
so the readback names the exact command and the real folder - and a refusal is
said without anyone being asked to confirm something that was never going to run.

A sentence is only claimed when its first word is a tool this knows about,
allowed or refused. "Run the tests in clio" is not a command - "the" is not a
program - so it carries on to conversation instead of being refused as one.

Short commands answer inline. Anything still going after a few seconds becomes a
background job, reported the same way a project run is, including 7.3's
plain-language failure explanations.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path

from clio.capabilities import files
from clio.capabilities.filewrite import _find_directory
from clio.capabilities.web import _LEAD
from clio.core import jobs, shell
from clio.core.logging import get_logger

log = get_logger("clio.capabilities.shellcmd")

INLINE_S = 10.0
_POLL_S = 0.25
_SPOKEN_CHARS = 220

_LEAD_ANY_CASE = re.compile(_LEAD.pattern, re.I)
_PUNCT = re.compile(r"[.!?,;]+$")
_WHERE = r"(?:the |my )?(?P<where>[\w .-]+?)(?: project| folder| repo| repository)?"
_PATTERNS = [
    re.compile(r"^run (?P<cmd>.+?) (?:in|inside|on) " + _WHERE + r"$", re.I),
    re.compile(r"^(?:in|inside) " + _WHERE + r",? run (?P<cmd>.+)$", re.I),
]
_GIT_STATUS = re.compile(
    r"^(?:what'?s|what is|check|show me) the git status (?:of|in|for) " + _WHERE + r"$", re.I)


@dataclass(frozen=True)
class ShellRequest:
    argv: list[str] = field(default_factory=list)
    folder: Path | None = None
    read_only: bool = False
    warning: str = ""
    refusal: str = ""

    @property
    def shown(self) -> str:
        return " ".join(self.argv)


def parse_command(text: str) -> tuple[list[str], str] | None:
    """(words, folder name) - the shape of the sentence only, case kept, since
    branch and package names care about it."""
    spoken = _PUNCT.sub("", " ".join(text.strip().split()))
    spoken = _LEAD_ANY_CASE.sub("", spoken).strip()
    found = _GIT_STATUS.match(spoken)
    if found is not None:
        return ["git", "status"], found.group("where").strip()
    for pattern in _PATTERNS:
        found = pattern.match(spoken)
        if found is None:
            continue
        words = found.group("cmd").split()
        # Only a sentence that names a program is a command. Everything else
        # belongs to conversation, not to a refusal.
        if not words or words[0].lower() not in shell.ALLOWED | set(shell.BLOCKED):
            return None
        return words, found.group("where").strip()
    return None


class ShellCommands:
    def __init__(self, roots: tuple[Path, ...], projects, runner: jobs.JobRunner):
        self._roots = tuple(Path(r).resolve() for r in roots)
        self._projects = projects
        self._runner = runner

    def resolve(self, text: str) -> ShellRequest | None:
        parsed = parse_command(text)
        if parsed is None:
            return None
        words, where = parsed
        verdict = shell.vet(words)
        if verdict.refusal:
            return ShellRequest(refusal=verdict.refusal)
        folder = self._folder(where)
        if folder is None:
            return ShellRequest(refusal=(
                f"I can't find a {where} to run it in. It has to be one of your projects, "
                "or somewhere in Documents, Downloads or Desktop."))
        return ShellRequest(argv=verdict.argv, folder=folder,
                            read_only=verdict.read_only, warning=verdict.warning)

    def _folder(self, where: str) -> Path | None:
        """A registered project first, then a folder inside the roots. Never a
        path spoken aloud, and never anywhere outside what he configured."""
        project = self._projects.resolve(where) if self._projects is not None else None
        if project is not None and project.path.is_dir():
            return project.path
        wanted = re.sub(r"^(?:the|my)\s+", "", where.strip().lower())
        for root in self._roots:
            if root.name.lower() == wanted:
                return root
        found = (files._find_folder(wanted, self._roots, files._index(self._roots))
                 or _find_directory(wanted, self._roots))
        if found is None:
            return None
        resolved = Path(found).resolve()
        inside = any(resolved == r or resolved.is_relative_to(r) for r in self._roots)
        return resolved if inside else None

    @staticmethod
    def describe(request: ShellRequest) -> str:
        """What the permission gate reads back: the exact command and the folder."""
        said = f"Running {request.shown}, in {request.folder.name}"
        if request.warning:
            said += f". Careful - {request.warning}"
        return said

    async def run(self, request: ShellRequest) -> tuple[str, str]:
        """What to say, plus the full output for the chat window."""
        _, spoken, output = await self.run_until(request, INLINE_S)
        return spoken, output

    async def run_until(self, request: ShellRequest, wait_s: float) -> tuple[bool | None, str, str]:
        """(finished cleanly?, what to say, output). None means it was still
        going after wait_s and carries on as a background job. A plan (7.7)
        waits longer than a single command, because the next step usually needs
        this one finished - an install before the training.

        ponytail: "cleanly" is read from the log (jobs.explain), since detached
        jobs have no exit code. A failure that prints nothing recognisable
        counts as success; record exit codes in the runner if that bites."""
        if request.refusal:
            return False, request.refusal, ""
        tool = request.argv[0]
        prefix = shell.command_for(tool, request.folder)
        if prefix is None:
            return False, f"{tool} isn't installed on this machine, or it isn't on the path.", ""

        name = " ".join(request.argv[:2])
        try:
            job = await asyncio.to_thread(
                self._runner.start, name, [*prefix, *request.argv[1:]], request.folder)
        except OSError as exc:
            return False, f"Windows wouldn't start {tool}: {exc.strerror or exc}.", ""
        log.info("Command started", extra={"extra_fields": {
            "command": request.shown, "folder": request.folder.name,
            "read_only": request.read_only}})

        waited = 0.0
        while waited < wait_s and self._runner.is_alive(job):
            await asyncio.sleep(_POLL_S)
            waited += _POLL_S

        if self._runner.is_alive(job):
            # Still going: it becomes a job like any other, with status, logs,
            # "stop it" and an announcement when it ends.
            self._runner.watch(job)
            return None, (f"{request.shown} is still going, so it's carrying on in the "
                          "background. I'll tell you when it's done."), ""

        self._runner.forget(job.name)
        output = _output(self._runner.tail(job, 80))
        reason = jobs.explain(output)
        if reason:
            return False, f"That didn't work: {reason}.", output
        return True, _spoken(request.shown, output), output


def _output(logged: str) -> str:
    """The log without the "$ command / in folder" header the runner writes."""
    lines = logged.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("  in "):
            return "\n".join(lines[index + 1:]).strip()
    return logged.strip()


def _spoken(shown: str, output: str) -> str:
    """A command's output is for reading, not listening to - so she says the
    gist and the chat window gets the rest."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return f"Done - {shown} didn't print anything."
    if len(lines) <= 2:
        said = " ".join(lines)
    else:
        said = f"{lines[0]} ... and {len(lines) - 1} more lines in the window"
    return f"Done. {said[:_SPOKEN_CHARS]}"

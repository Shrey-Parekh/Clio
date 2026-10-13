"""A coding agent, started by voice and reviewed before anything lands (9.2).

"Get the agent to fix the failing test in ewaste": Claude Code runs headless
(his choice) in its own git worktree, on a branch of its own, as a background
job - his working folder and branch are never touched while it works. It may
read and edit files, and run only his tests and read-only git; no installs, no
web, no push. When it ends she says what it changed and whether the tests
passed; nothing is merged until he says "merge the agent's work", and "throw
away the agent's work" removes it. One agent at a time, half an hour at most.

The program is called directly, never through the npm `claude.cmd` shim: a
spoken task passed through cmd.exe would let `&` or `|` in it run commands.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from clio.core.logging import get_logger

log = get_logger("clio.capabilities.agent")

JOB = "agent"
MAX_MINUTES = 30
_POLL_S = 5.0

_LEAD = r"^(?:(?:hey |ok |okay )?clio,? )?(?:(?:can|could|would) you |please )?"
_WHO = r"(?:the |a )?(?:coding )?(?:agent|claude(?: code)?)"
_PATTERNS = [
    # The task runs to the last "in/on/for": "fix the bug in the parser in
    # ewaste" is a task about the parser, in the ewaste project.
    ("start", rf"(?:get|have|ask|tell|let) {_WHO} (?:to )?(?P<task>.+) (?:in|on|for) (?:the |my )?"
              rf"(?P<where>[\w.-]+(?: [\w.-]+)?)(?: project| repo)?"),
    ("status", rf"(?:how'?s|how is) {_WHO} (?:doing|getting on)|is {_WHO} (?:done|finished|still going)|"
               rf"what'?s {_WHO} doing"),
    ("stop", rf"(?:stop|cancel|kill) {_WHO}"),
    ("show", rf"(?:show me|what are|tell me) (?:the )?(?:agent'?s|claude'?s) (?:changes|work)|"
             rf"what did {_WHO} (?:change|do)"),
    ("merge", rf"(?:merge|keep|accept) (?:the )?(?:agent'?s|claude'?s) (?:work|changes)"),
    ("discard", rf"(?:throw away|discard|bin|drop|delete) (?:the )?(?:agent'?s|claude'?s) (?:work|changes)"),
]
_COMPILED = [(kind, re.compile(_LEAD + f"(?:{p})$")) for kind, p in _PATTERNS]

_PROMPT = (
    "{task}\n\n"
    "You are working unattended in a separate git worktree of this project; nobody can "
    "answer questions, so make reasonable choices and say what you assumed. Make the "
    "change. Run the project's tests if it has any - the project's Python is {python} "
    "(use it as `{python} -m pytest` or `{python} tests/<file>.py`), or `npm test`. Do not "
    "commit, push, install packages or use the network. Finish with a short plain-English "
    "summary: what you changed, and whether the tests pass."
)


@dataclass(frozen=True)
class AgentRequest:
    kind: str
    task: str = ""
    where: str = ""


@dataclass
class AgentRun:
    task: str
    repo: str          # his project folder
    worktree: str      # the agent's copy
    branch: str
    base: str          # the branch it was cut from, and merges back into
    started: float = field(default_factory=time.time)
    report: str = ""   # set once it has finished
    files: int = 0


def parse_agent_request(text: str) -> AgentRequest | None:
    said = re.sub(r"[.!?]+$", "", text.strip().lower())
    for kind, pattern in _COMPILED:
        found = pattern.match(said)
        if found:
            groups = found.groupdict()
            return AgentRequest(kind, (groups.get("task") or "").strip(), (groups.get("where") or "").strip())
    return None


def find_claude() -> str | None:
    """The real Claude Code program, not the npm shim scripts beside it."""
    bundled = Path(os.environ.get("APPDATA", "")) / "npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe"
    if bundled.is_file():
        return str(bundled)
    found = shutil.which("claude.exe")
    return found if found and found.lower().endswith(".exe") else None


def _git(args: list[str], cwd: str | Path) -> tuple[int, str]:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                            timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return result.returncode, (result.stdout + result.stderr).strip()


class CodingAgent:
    def __init__(self, root: Path | str, jobs, find_folder, announce=None, git=_git, claude=find_claude):
        self._state = Path(root) / "agent.json"
        self._worktrees = Path(root) / "agents"
        self._jobs = jobs
        self._folder = find_folder      # a spoken project name to a folder, or None
        self._announce = announce
        self._git = git
        self._claude = claude
        self._watcher: asyncio.Task | None = None
        self._finishing = threading.Lock()

    # --- state, kept on disk so a restart doesn't orphan a worktree ---

    def current(self) -> AgentRun | None:
        try:
            return AgentRun(**json.loads(self._state.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return None

    def _save(self, run: AgentRun | None) -> None:
        if run is None:
            self._state.unlink(missing_ok=True)
            return
        self._state.parent.mkdir(parents=True, exist_ok=True)
        self._state.write_text(json.dumps(asdict(run), indent=2), encoding="utf-8")

    def _running(self) -> bool:
        job = self._jobs.find(JOB)
        return job is not None and self._jobs.is_alive(job)

    # --- starting: checked when matched, so the readback is specific ---

    def plan(self, request: AgentRequest) -> tuple[str, Path | None]:
        """(refusal or "", the repo) for a start request. Blocking: runs git."""
        run = self.current()
        if run is not None:
            if self._running():
                return f"The agent's still working on {Path(run.repo).name}. One at a time.", None
            return (f"The agent's last work on {Path(run.repo).name} is still waiting - say merge "
                    "the agent's work, or throw it away, first."), None
        if self._claude() is None:
            return "I can't find Claude Code on this PC, so I can't start the agent.", None
        folder = self._folder(request.where)
        if folder is None:
            return f"I can't find a project called {request.where}.", None
        code, top = self._git(["rev-parse", "--show-toplevel"], folder)
        if code != 0:
            return f"{folder.name} isn't a git repo, so the agent would have nowhere safe to work.", None
        return "", Path(top)

    @staticmethod
    def describe(request: AgentRequest, repo: Path) -> str:
        return (f"Starting Claude Code on {repo.name}, in its own copy, to {request.task}. "
                "It uses your Claude plan")

    def start(self, request: AgentRequest, repo: Path) -> str:
        _, base = self._git(["rev-parse", "--abbrev-ref", "HEAD"], repo)
        slug = re.sub(r"[^a-z0-9]+", "-", request.task.lower()).strip("-")[:30] or "task"
        stamp = time.strftime("%m%d-%H%M")
        branch = f"clio/agent-{slug}-{stamp}"
        worktree = self._worktrees / f"{repo.name}-{slug}-{stamp}"
        self._worktrees.mkdir(parents=True, exist_ok=True)
        code, out = self._git(["worktree", "add", "-b", branch, str(worktree), "HEAD"], repo)
        if code != 0:
            log.error("Worktree failed", extra={"extra_fields": {"git": out[-300:]}})
            return "I couldn't make a separate copy of the project for the agent, so I didn't start it."
        # The copy has no venv of its own (it isn't tracked), so the agent is
        # pointed at the project's - and only that interpreter running tests
        # is allowed. ponytail: a test file it writes can still run any code;
        # "run the tests" can't be had without that. Its copy, not his folder.
        python = repo / ".venv" / "Scripts" / "python.exe"
        python_text = python.as_posix() if python.is_file() else "python"
        allowed = ["Read", "Edit", "Write", "Glob", "Grep", "TodoWrite",
                   "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)",
                   f"Bash({python_text} -m pytest:*)", f"Bash({python_text} tests/:*)",
                   "Bash(npm test:*)"]
        command = [self._claude(), "-p", _PROMPT.format(task=request.task, python=python_text),
                   "--permission-mode", "acceptEdits",
                   "--allowedTools", *allowed,
                   "--disallowedTools", "WebFetch", "WebSearch"]
        try:
            job = self._jobs.start(JOB, command, worktree)
        except OSError as exc:
            self._git(["worktree", "remove", "--force", str(worktree)], repo)
            self._git(["branch", "-D", branch], repo)
            log.error("Agent failed to start", extra={"extra_fields": {"error": str(exc)}})
            return "Claude Code wouldn't start, so nothing was changed."
        self._save(AgentRun(task=request.task, repo=str(repo), worktree=str(worktree),
                            branch=branch, base=base or "HEAD"))
        # Watched by the caller, back on the event loop: this runs on a worker
        # thread (git and process start block), which has no loop to watch from.
        return (f"The agent's started on {repo.name}. I'll tell you when it's done - you'll "
                "review it before anything is merged.")

    # --- watching, and the report ---

    def watch(self, job=None) -> None:
        """Called on start, and when Clio starts with an agent still out."""
        if self._watcher is not None and not self._watcher.done():
            return
        job = job or self._jobs.find(JOB)
        run = self.current()
        if job is None and (run is None or run.report):
            return
        # job None with an unreported run: it ended while Clio was closed and
        # the job list was swept at startup - report it now from what's left.
        self._watcher = asyncio.ensure_future(self._until_done(job))

    async def _until_done(self, job) -> None:
        while job is not None and self._jobs.is_alive(job):
            if time.time() - job.started > MAX_MINUTES * 60:
                log.info("Agent hit the time limit")
                self._jobs.stop(JOB)
                break
            await asyncio.sleep(_POLL_S)
        said = await asyncio.to_thread(self.finish)
        if self._announce is not None and said:
            await self._announce(said)

    def finish(self) -> str:
        """Commit what it did on its own branch and say what changed. Safe to
        call again: a run already reported is left alone."""
        # The watcher and a "how's the agent doing" can both get here, each on
        # a worker thread; the second must find the first's report, not race it.
        with self._finishing:
            return self._finish()

    def _finish(self) -> str:
        run = self.current()
        if run is None or run.report:
            return ""
        job = self._jobs.find(JOB)
        summary = ""
        if job is not None:
            tail = self._jobs.tail(job, 30).strip().splitlines()
            summary = " ".join(line.strip() for line in tail[-6:] if line.strip())
            self._jobs.forget(JOB)
        self._git(["add", "-A"], run.worktree)
        code, _ = self._git(["diff", "--cached", "--quiet"], run.worktree)
        if code == 0:
            run.report, run.files = "It finished without changing anything.", 0
        else:
            self._git(["commit", "-q", "-m", f"Agent: {run.task}"], run.worktree)
            _, names = self._git(["diff", "--name-only", f"{run.base}...HEAD"], run.worktree)
            run.files = len([n for n in names.splitlines() if n.strip()])
            run.report = summary or "It didn't say what it did."
        self._save(run)
        name = Path(run.repo).name
        if not run.files:
            # Nothing to review: tidy up now rather than leave him a chore.
            self.discard()
            return f"The agent finished on {name} without changing anything."
        plural = "s" if run.files != 1 else ""
        return (f"The agent's done on {name}: {run.files} file{plural} changed. "
                f"{_first_sentence(run.report)} Say merge the agent's work, or throw it away.")

    # --- what he asks afterwards ---

    def status(self) -> str:
        run = self.current()
        if run is None:
            return "No agent is working at the moment."
        if self._running():
            minutes = int((time.time() - run.started) // 60)
            _, porcelain = self._git(["status", "--porcelain"], run.worktree)
            touched = len([line for line in porcelain.splitlines() if line.strip()])
            return (f"Still working on {Path(run.repo).name}, {minutes} minute{'s' if minutes != 1 else ''} in"
                    f" - {touched} file{'s' if touched != 1 else ''} changed so far.")
        said = self.finish() if not run.report else ""
        return said or f"It's finished on {Path(run.repo).name} and waiting for you to review it."

    def show(self) -> str:
        run = self.current()
        if run is None:
            return "There's no agent work to show."
        if not run.report:
            return self.status()
        _, stat = self._git(["diff", "--stat", f"{run.base}...HEAD"], run.worktree)
        return f"{run.report}\n\n{stat}".strip()

    def stop(self) -> str:
        if not self._running():
            return "The agent isn't running."
        self._jobs.stop(JOB)
        return "Stopped the agent. Whatever it changed so far is kept for you to look at."

    def merge_plan(self) -> tuple[str, AgentRun | None]:
        """(refusal or "", the run) for a merge: only finished work, onto a clean branch."""
        run = self.current()
        if run is None:
            return "There's no agent work to merge.", None
        if self._running():
            return "The agent's still working.", None
        if not run.report:
            self.finish()
            run = self.current()
            if run is None:
                return "The agent didn't change anything, so there's nothing to merge.", None
        _, dirty = self._git(["status", "--porcelain"], run.repo)
        if dirty.strip():
            return (f"You have uncommitted changes in {Path(run.repo).name} - commit or stash them "
                    "first, so the merge can't tangle with them."), None
        return "", run

    @staticmethod
    def describe_merge(run: AgentRun) -> str:
        return (f"Merging the agent's work on {Path(run.repo).name} into {run.base} - "
                f"{run.files} file{'s' if run.files != 1 else ''} changed, locally, not pushed")

    def merge(self) -> str:
        run = self.current()
        if run is None:
            return "There's no agent work to merge."
        code, out = self._git(["merge", "--no-ff", run.branch, "-m", f"Merge agent work: {run.task}"], run.repo)
        if code != 0:
            self._git(["merge", "--abort"], run.repo)
            log.error("Agent merge failed", extra={"extra_fields": {"git": out[-300:]}})
            return ("That merge clashed with changes on your branch, so I undid it. The agent's "
                    f"work is still on {run.branch}.")
        self._git(["worktree", "remove", "--force", run.worktree], run.repo)
        self._git(["branch", "-d", run.branch], run.repo)
        self._save(None)
        return f"Merged into {run.base}. Not pushed - that's yours to do."

    @staticmethod
    def describe_discard(run: AgentRun) -> str:
        return (f"Throwing away the agent's work on {Path(run.repo).name} - "
                f"{run.files} file{'s' if run.files != 1 else ''} changed, and that can't be undone")

    def discard(self) -> str:
        run = self.current()
        if run is None:
            return "There's no agent work to throw away."
        if self._running():
            self._jobs.stop(JOB)
        self._git(["worktree", "remove", "--force", run.worktree], run.repo)
        self._git(["branch", "-D", run.branch], run.repo)
        self._save(None)
        return f"Thrown away. {Path(run.repo).name} is as you left it."


def _first_sentence(text: str) -> str:
    found = re.match(r"(.+?[.!?])(\s|$)", text.strip())
    sentence = (found.group(1) if found else text.strip())[:200]
    return sentence if sentence.endswith((".", "!", "?")) else sentence + "."

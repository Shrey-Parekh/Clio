"""Running his projects as background jobs (7.2).

A job is not a timer. It outlives the turn that started it, it outlives Clio
herself, and it writes its output to a file rather than into a conversation. So
what is kept here is the minimum needed to find it again after a restart: the
pid, when it started, and where its log is.

**Nothing here decides what to run.** The command arrives from the registry,
which he approved; this module starts it, watches it, and stops it.

The one thing a pid cannot tell you is whether it is still *your* process -
Windows reuses them. So the creation time is stored alongside, and a pid whose
process started at a different second is treated as gone rather than killed.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from clio.core.logging import get_logger
# The same toast 6.4 fires for a reminder. `clio/remind.py` imports nothing from
# the voice stack, so borrowing it costs nothing and keeps one implementation.
from clio.remind import toast

log = get_logger("clio.jobs")

_POLL_S = 5.0
_TAIL_LINES = 40
_STOP_GRACE_S = 5.0
# Long enough that he has probably stopped watching, which is the whole reason
# a job runs in the background. Shorter than this and a toast is just noise.
_TOAST_AFTER_S = 60.0

# What went wrong, in his words rather than the traceback's. Deterministic and
# free: these are the failures that actually happen, and a model is no better at
# reading "CUDA out of memory" than a regex is. Anything unmatched falls through
# to the last exception line, which is at least true.
_FAILURES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"cuda (?:error )?out of memory|torch\.cuda\.OutOfMemoryError", re.I),
     "it ran out of GPU memory"),
    (re.compile(r"\bMemoryError\b|Cannot allocate memory", re.I),
     "it ran out of memory"),
    (re.compile(r"ModuleNotFoundError: No module named ['\"]([\w.]+)", re.I),
     "the {0} module isn't installed"),
    (re.compile(r"FileNotFoundError.*?['\"]([^'\"]{3,60})['\"]", re.I),
     "it couldn't find {0}"),
    (re.compile(r"PermissionError|Access is denied", re.I),
     "Windows wouldn't let it open a file it needed"),
    (re.compile(r"address already in use|EADDRINUSE|10048", re.I),
     "the port it wanted is already in use"),
    (re.compile(r"SyntaxError: (.+)", re.I), "there's a syntax error in it: {0}"),
    (re.compile(r"KeyboardInterrupt", re.I), "something interrupted it"),
    (re.compile(r"(?:command not found|is not recognized as an internal)", re.I),
     "the command isn't on this machine"),
    (re.compile(r"npm ERR!.*?(missing script: .+)", re.I), "npm says {0}"),
    (re.compile(r"disk (?:is )?full|No space left on device", re.I),
     "the disk is full"),
    # winget (7.8). 1602 and 1223 are a declined admin prompt, not a broken app.
    (re.compile(r"exit code: (?:1602|1223)\b", re.I),
     "it was cancelled - the admin prompt was probably declined"),
    (re.compile(r"Installer failed with exit code: (-?\d+)", re.I),
     "the installer failed with code {0}"),
    (re.compile(r"No (?:installed )?package found matching", re.I),
     "winget couldn't find that package"),
    (re.compile(r"No (?:applicable|available) upgrade found", re.I),
     "there was no newer version to install"),
]
# The last "SomeError: message" line, when nothing above matched.
_LAST_EXCEPTION = re.compile(r"^\s*(\w*(?:Error|Exception|Failure))\b:?\s*(.*)$")
# Deterministic progress, read out of the log rather than guessed by a model.
_PROGRESS = [
    re.compile(r"\bepoch\s+(\d+)\s*(?:/|of)\s*(\d+)", re.I),
    re.compile(r"\bstep\s+(\d+)\s*/\s*(\d+)", re.I),
    re.compile(r"\b(\d{1,3})%"),
]


@dataclass(frozen=True)
class Job:
    name: str
    pid: int
    command: str
    folder: str
    log: str
    started: float      # wall clock, for "running for 20 minutes"
    created: float      # the process's own creation time, to catch pid reuse


class JobRunner:
    def __init__(self, root: Path | str, announce=None):
        self._root = Path(root)
        self._file = self._root / "jobs.json"
        self._logs = self._root / "jobs"
        self._announce = announce
        self._watchers: dict[str, asyncio.Task] = {}

    # --- starting and stopping ---

    def start(self, name: str, command: str | list[str], folder: Path) -> Job:
        """Detached, with output to a file. Raises OSError for the caller to say.

        A string runs through the shell; a list runs with none. Registered
        projects are strings, because their commands are real command lines
        ("npm run dev") from a config file he approved. Spoken commands (7.5)
        are lists, because nothing that came out of a microphone should ever
        meet a shell - no filter on `;` or `&&` is as safe as having nothing
        there to interpret them."""
        self._logs.mkdir(parents=True, exist_ok=True)
        shown = command if isinstance(command, str) else " ".join(command)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe_name = re.sub(r"[^\w.-]+", "-", name).strip("-") or "job"
        log_path = self._logs / f"{safe_name}-{stamp}.log"
        handle = log_path.open("w", encoding="utf-8", errors="replace")
        handle.write(f"$ {shown}\n  in {folder}\n\n")
        handle.flush()

        creation = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        # No console window flashing up for a spoken command.
        creation |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(
            command, cwd=str(folder), shell=isinstance(command, str), stdout=handle,
            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            creationflags=creation,
        )
        job = Job(name=name, pid=process.pid, command=shown, folder=str(folder),
                  log=str(log_path), started=time.time(), created=_created(process.pid))
        self._remember(job)
        log.info("Job started", extra={"extra_fields": {
            "job": name, "pid": job.pid, "command": command}})
        return job

    def stop(self, name: str) -> str:
        job = self.find(name)
        if job is None:
            return f"There's no {name} running."
        import psutil

        try:
            process = psutil.Process(job.pid)
            children = process.children(recursive=True)
            for victim in [*children, process]:
                victim.terminate()
            _, alive = psutil.wait_procs([*children, process], timeout=_STOP_GRACE_S)
            for stubborn in alive:
                stubborn.kill()
        except psutil.NoSuchProcess:
            pass
        except psutil.Error as exc:
            log.warning("Stop failed", extra={"extra_fields": {"job": name, "error": str(exc)}})
            return f"Windows wouldn't let me stop {job.name}."
        self._forget(job.name)
        log.info("Job stopped", extra={"extra_fields": {"job": job.name, "pid": job.pid}})
        return f"Stopped {job.name}."

    # --- looking at them ---

    def running(self) -> list[Job]:
        """Only the ones actually alive. A finished job is swept as it is found,
        which is also how one that ended while Clio was closed gets cleared."""
        known = self._load()
        alive = [job for job in known if self.is_alive(job)]
        if len(alive) != len(known):
            self._write(alive)
        return alive

    def find(self, name: str) -> Job | None:
        wanted = name.strip().lower()
        for job in self.running():
            if wanted in job.name.lower() or job.name.lower() in wanted:
                return job
        return None

    @staticmethod
    def is_alive(job: Job) -> bool:
        import psutil

        try:
            process = psutil.Process(job.pid)
            # Windows reuses pids. Same number, different start time, different
            # process - and killing it would be killing a stranger.
            return abs(process.create_time() - job.created) < 1.0
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False

    def tail(self, job: Job, lines: int = _TAIL_LINES) -> str:
        try:
            text = Path(job.log).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return "\n".join(text.splitlines()[-lines:])

    def progress(self, job: Job) -> str:
        """Deterministic where the output has a shape, empty where it hasn't -
        the caller falls back to the model rather than this inventing a number."""
        for line in reversed(self.tail(job).splitlines()):
            for pattern in _PROGRESS:
                found = pattern.search(line)
                if found is None:
                    continue
                if len(found.groups()) == 2:
                    return f"{found.group(1)} of {found.group(2)}"
                return f"{found.group(1)} percent"
        return ""

    # --- telling him when it ends ---

    def watch(self, job: Job) -> None:
        """One task per job, announcing the end. Lost on restart, which is why
        `running()` sweeps too: the report is best-effort, the bookkeeping is not."""
        if self._announce is None:
            return
        existing = self._watchers.get(job.name)
        if existing is not None and not existing.done():
            existing.cancel()
        self._watchers[job.name] = asyncio.create_task(self._until_done(job))

    async def _until_done(self, job: Job) -> None:
        while self.is_alive(job):
            await asyncio.sleep(_POLL_S)
        self._forget(job.name)
        spoken = self.outcome(job)
        await self._announce(spoken)
        # A toast as well, for anything that ran long enough that he has
        # probably walked away - which is most of why a job exists at all. The
        # spoken copy only reaches him if he is in the room.
        if time.time() - job.started >= _TOAST_AFTER_S or "stopped" in spoken:
            toast("Clio", spoken)

    def outcome(self, job: Job) -> str:
        """What to say about a job that has ended. There is no exit code - the
        process was detached - so the log's own last words are the evidence."""
        reason = explain(self.tail(job, 25))
        return f"{job.name} stopped: {reason}." if reason else f"{job.name} has finished."

    def missed(self) -> list[str]:
        """Jobs that ended while Clio was closed, reported once and then
        forgotten. Must run before anything else sweeps the file: `running()`
        prunes the dead silently, which is right for a status question and wrong
        for the one report he never got."""
        reports = []
        for job in [j for j in self._load() if not self.is_alive(j)]:
            said = self.outcome(job)
            self._forget(job.name)
            reports.append(f"While I was closed, {said[0].lower()}{said[1:]}")
        return reports

    # --- the little file that survives a restart ---

    def _load(self) -> list[Job]:
        try:
            raw = json.loads(self._file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        jobs = []
        for entry in raw:
            try:
                jobs.append(Job(**entry))
            except TypeError:
                continue    # a record from an older shape is dropped, not fatal
        return jobs

    def _write(self, jobs: list[Job]) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        temp = self._file.with_suffix(".json.tmp")
        temp.write_text(json.dumps([asdict(j) for j in jobs], indent=2), encoding="utf-8")
        temp.replace(self._file)

    def forget(self, name: str) -> None:
        """Drop a finished job from the list. For a command that ended inside
        the turn that started it (7.5): there is nothing left to report later."""
        self._forget(name)

    def _remember(self, job: Job) -> None:
        self._write([j for j in self._load() if j.name != job.name] + [job])

    def _forget(self, name: str) -> None:
        self._write([j for j in self._load() if j.name != name])


def explain(tail: str) -> str:
    """Why a job stopped, said the way he would say it - or empty when the log
    holds no sign of trouble, which is how a clean finish is told apart from a
    failure without an exit code to ask.

    "It ran out of GPU memory" beats reciting eleven lines of traceback, and a
    recited traceback is what he was avoiding by asking her in the first place.
    """
    if not tail.strip():
        return ""
    for pattern, said in _FAILURES:
        found = pattern.search(tail)
        if found is None:
            continue
        groups = [g for g in found.groups() if g]
        return said.format(*groups) if groups else said

    # Nothing known. The last exception line is still better than "an error".
    for line in reversed(tail.splitlines()):
        found = _LAST_EXCEPTION.match(line.strip())
        if found is not None:
            message = " ".join(found.group(2).split())[:120]
            return f"{found.group(1)}{', ' + message if message else ''}"
    if re.search(r"\btraceback\b", tail, re.I):
        return "it ended in an error I couldn't make sense of"
    return ""


def _created(pid: int) -> float:
    import psutil

    try:
        return psutil.Process(pid).create_time()
    except psutil.Error:
        return 0.0


def free_vram_mb() -> int:
    """What the GPU has spare, for saying so before starting a job that wants
    it. Zero when there is no nvidia-smi to ask."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    lines = result.stdout.strip().splitlines()
    try:
        return int(lines[0]) if lines else 0
    except ValueError:
        return 0

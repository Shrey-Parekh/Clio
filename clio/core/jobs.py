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

log = get_logger("clio.jobs")

_POLL_S = 5.0
_TAIL_LINES = 40
_STOP_GRACE_S = 5.0
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

    def start(self, name: str, command: str, folder: Path) -> Job:
        """Detached, with output to a file. Raises OSError for the caller to say."""
        self._logs.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        log_path = self._logs / f"{name}-{stamp}.log"
        handle = log_path.open("w", encoding="utf-8", errors="replace")
        handle.write(f"$ {command}\n  in {folder}\n\n")
        handle.flush()

        # shell=True because the registry holds real command lines ("npm run
        # dev"), which are shell syntax rather than an argv list. What makes
        # that safe is where the string came from: a config file he approved,
        # never a sentence.
        creation = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        process = subprocess.Popen(
            command, cwd=str(folder), shell=True, stdout=handle,
            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            creationflags=creation,
        )
        job = Job(name=name, pid=process.pid, command=command, folder=str(folder),
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
        tail = self.tail(job, 15).lower()
        # No exit code: the process was detached, so what it said last is the
        # only evidence there is. Said as a reading of the log, not a verdict.
        if any(word in tail for word in ("traceback", "error:", "exception", "failed")):
            await self._announce(f"{job.name} stopped, and its log ends in an error.")
        else:
            await self._announce(f"{job.name} has finished.")

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

    def _remember(self, job: Job) -> None:
        self._write([j for j in self._load() if j.name != job.name] + [job])

    def _forget(self, name: str) -> None:
        self._write([j for j in self._load() if j.name != name])


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

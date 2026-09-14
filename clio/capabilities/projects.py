"""His projects: what she knows how to run, and running it (7.1, 7.2).

The registry is the whole safety story. **A command is never assembled from a
sentence** - it is looked up. The worst a mishearing can do is pick the wrong
registered project, which he then hears read back before anything starts. It
cannot invent `rm -rf`, because there is nowhere for that string to come from.

Proposing is separate from registering, and registering is separate from
running. She reads a folder with 6.9, says what she thinks it is and how it
appears to run, and writes nothing until he says yes. What she writes is a plain
TOML block appended to his config, which he can edit or delete like any other
line in it.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from pathlib import Path

from clio.capabilities.web import _LEAD
from clio.core import documents
from clio.core.config import ProjectConfig
from clio.core.jobs import JobRunner, free_vram_mb
from clio.core.logging import get_logger

log = get_logger("clio.capabilities.projects")

_PUNCT = re.compile(r"[.!?,;:]+$")
_THE = re.compile(r"^(?:the|my)\s+")
_SKIP_DIRS = {"node_modules", "__pycache__", "venv", ".venv", "dist", "build",
              "site-packages", ".git", "AppData", "Temp"}
_MARKERS = ("pyproject.toml", "requirements.txt", "package.json", "Cargo.toml",
            "go.mod", "Makefile", "environment.yml")
_SCAN_DEPTH = 2
# Words that already mean something else when said after "run" or "stop".
_NOT_PROJECTS = {"timer", "stopwatch", "music", "alarm", "it", "that", "this",
                 "talking", "recording", "reminder", "reminders", "clock"}
_MAX_PROPOSED = 6
# A GPU job on 8GB wants most of the card; below this she says so out loud.
_TIGHT_VRAM_MB = 4_000


@dataclass(frozen=True)
class ProjectRequest:
    kind: str      # "list", "scan", "register", "run", "status", "stop", "log"
    name: str = ""


@dataclass(frozen=True)
class Proposal:
    name: str
    path: Path
    command: str
    summary: str


_PATTERNS: list[tuple[str, str]] = [
    ("list", r"^(?:what|which) projects? (?:do you know|can you run|have you got)$"),
    ("list", r"^(?:what can you run|list (?:my )?projects)$"),
    ("scan", r"^(?:what projects? can you see|find (?:my )?projects|"
             r"look for (?:my )?projects|scan for projects)$"),
    ("register", r"^(?:register|remember|add) (?:the )?(?P<name>.+?)(?: project)?$"),
    ("status", r"^(?:what'?s running|what have you got running|any jobs running)$"),
    ("status", r"^(?:is|are) (?:the )?(?P<name>.+?) (?:still )?(?:running|going|training)$"),
    ("status", r"^how(?:'?s| is) (?:the )?(?P<name>.+?) (?:going|getting on|doing)$"),
    ("log", r"^(?:what(?:'?s| is) (?:the )?(?P<name>.+?) (?:saying|printed|output)|"
            r"show me (?:the )?(?P<name2>.+?) log)$"),
    ("stop", r"^(?:stop|kill|cancel|end) (?:the )?(?P<name>.+?)(?: job| run| training)?$"),
    ("run", r"^(?:run|start|kick off|launch) (?:the )?(?P<name>.+?)(?: project| job| again)?$"),
]

# The lead words ("hey Clio, could you...") are stripped in the parser below,
# not baked into every pattern.
_COMPILED = [(kind, re.compile(pattern)) for kind, pattern in _PATTERNS]


def parse_project_request(text: str) -> ProjectRequest | None:
    spoken = _PUNCT.sub("", " ".join(text.strip().lower().split()))
    spoken = _LEAD.sub("", spoken).strip()
    for kind, pattern in _COMPILED:
        found = pattern.match(spoken)
        if found is None:
            continue
        parts = found.groupdict()
        name = _THE.sub("", (parts.get("name") or parts.get("name2") or "").strip())
        # "Start a timer", "stop the music": those verbs belong to other
        # capabilities too. The registry is the real gate - a name she doesn't
        # know never runs - but claiming these sentences at all would leave the
        # whole thing depending on registration order.
        if kind in ("run", "stop") and (name.startswith(("a ", "an ")) or name in _NOT_PROJECTS):
            return None
        return ProjectRequest(kind, name)
    return None


class ProjectCapability:
    def __init__(self, projects: dict[str, ProjectConfig], roots: tuple[Path, ...],
                 runner: JobRunner, config_path: Path | None = None):
        self._projects = dict(projects)
        self._roots = roots
        self._runner = runner
        self._config_path = config_path
        self._proposed: list[Proposal] = []

    # --- what she knows ---

    def known(self) -> dict[str, ProjectConfig]:
        return dict(self._projects)

    def resolve(self, spoken: str) -> ProjectConfig | None:
        """By name, then alias, then a loose match - "the ewaste training"
        should find `ewaste` without him repeating the config key exactly."""
        wanted = _THE.sub("", spoken.strip().lower())
        if not wanted:
            return None
        if wanted in self._projects:
            return self._projects[wanted]
        for project in self._projects.values():
            # Aliases are normalised the same way the spoken name is, so an
            # alias written as "the training" still matches "the training".
            if wanted in {_THE.sub("", alias) for alias in project.aliases}:
                return project
            key = project.name.lower()
            if key in wanted or wanted in key:
                return project
        return None

    def listing(self) -> str:
        if not self._projects:
            return ("I don't know how to run anything yet. Ask me what projects I can see "
                    "and I'll have a look through your folders.")
        names = [p.name for p in self._projects.values()]
        if len(names) == 1:
            only = next(iter(self._projects.values()))
            return f"One: {only.name}, which runs {only.command}."
        return f"{len(names)}: " + ", ".join(names) + "."

    # --- proposing, then registering ---

    def scan(self) -> str:
        """Reads each candidate folder with 6.9 and proposes. Writes nothing."""
        self._proposed = []
        for root in self._roots:
            for folder in _candidates(root):
                if any(p.path == folder for p in self._projects.values()):
                    continue
                if self.resolve(folder.name) is not None:
                    continue
                described = documents.describe_project(folder)
                command = documents.run_command(folder)
                if not command:
                    # No command, nothing to register: she would be registering
                    # a folder she cannot run.
                    continue
                self._proposed.append(Proposal(folder.name.lower(), folder, command,
                                               described.summary))
                if len(self._proposed) >= _MAX_PROPOSED:
                    break
        if not self._proposed:
            return ("I couldn't find anything new that says how it's meant to be run, "
                    "in the folders you've given me.")
        lines = [f"{p.name}, {p.command}" for p in self._proposed[:3]]
        rest = len(self._proposed) - len(lines)
        tail = f" And {rest} more." if rest > 0 else ""
        return (f"{len(self._proposed)} I could run: " + "; ".join(lines) + f".{tail} "
                "Say register and the name for any you want.")

    def register(self, name: str) -> str:
        wanted = _THE.sub("", name.strip().lower())
        proposal = next((p for p in self._proposed
                         if wanted == p.name or wanted in p.name or p.name in wanted), None)
        if proposal is None:
            return f"I haven't got a {name} to register. Ask what projects I can see first."
        if self.resolve(proposal.name) is not None:
            return f"{proposal.name} is already registered."

        project = ProjectConfig(name=proposal.name, path=proposal.path,
                                command=proposal.command, gpu=_looks_gpu(proposal))
        try:
            self._append_to_config(project)
        except OSError as exc:
            log.warning("Could not write the registry",
                        extra={"extra_fields": {"error": str(exc)}})
            return "I couldn't write that into your config file."
        self._projects[project.name.lower()] = project
        log.info("Project registered", extra={"extra_fields": {
            "project": project.name, "command": project.command}})
        return (f"Registered {project.name}, running {project.command}. "
                "It's in your config if you want to change it.")

    def _append_to_config(self, project: ProjectConfig) -> None:
        """Appended, never rewritten: his config is full of comments he wrote,
        and a round trip through a TOML writer would eat them."""
        if self._config_path is None:
            return
        aliases = ", ".join(f'"{a}"' for a in project.aliases)
        block = (f"\n[projects.{project.name}]\n"
                 f'# Added by Clio on {time.strftime("%Y-%m-%d")}, with your say-so.\n'
                 f'path = "{project.path.as_posix()}"\n'
                 f'command = "{project.command}"\n'
                 f"aliases = [{aliases}]\n"
                 f"gpu = {str(project.gpu).lower()}\n")
        with self._config_path.open("a", encoding="utf-8") as handle:
            handle.write(block)

    # --- running ---

    def describe_run(self, name: str) -> str:
        """The readback the permission gate speaks. Empty when there is nothing
        to run, so the gate is never asked about a project she cannot find."""
        project = self.resolve(name)
        if project is None:
            return ""
        said = f"Running {project.name}: {project.command}, in {project.path.name}"
        if project.gpu:
            free = free_vram_mb()
            if free and free < _TIGHT_VRAM_MB:
                said += (f". That one wants the GPU and there's only {free} megabytes free - "
                         "my speech models are holding some of it")
            elif free:
                said += f". It wants the GPU; there's {free} megabytes free"
        return said

    async def run(self, name: str) -> str:
        project = self.resolve(name)
        if project is None:
            return f"I don't know a project called {name}."
        if not project.path.exists():
            return f"{project.name} isn't where the config says it is."
        if self._runner.find(project.name) is not None:
            return f"{project.name} is already running."
        try:
            job = await asyncio.to_thread(
                self._runner.start, project.name, project.command, project.path)
        except OSError as exc:
            log.warning("Job failed to start",
                        extra={"extra_fields": {"project": project.name, "error": str(exc)}})
            return f"I couldn't start {project.name}: {exc.strerror or exc}."
        self._runner.watch(job)
        return f"{project.name} is running. I'll tell you when it's done."

    async def status(self, name: str) -> str:
        running = await asyncio.to_thread(self._runner.running)
        if not name:
            if not running:
                return "Nothing's running."
            return f"{len(running)} running: " + ", ".join(j.name for j in running) + "."
        job = next((j for j in running if name in j.name.lower() or j.name.lower() in name), None)
        if job is None:
            known = self.resolve(name)
            if known is None:
                return f"I don't know a project called {name}."
            return f"{known.name} isn't running."
        minutes = max(1, int((time.time() - job.started) / 60))
        progress = self._runner.progress(job)
        said = f"{job.name} has been going {minutes} minute{'s' if minutes > 1 else ''}"
        return f"{said}, at {progress}." if progress else f"{said}."

    async def log_tail(self, name: str) -> tuple[str, str]:
        """Spoken, plus the log text for the model when there is nothing
        deterministic to say about it."""
        running = self._runner.running()
        job = self._runner.find(name) if name else (running[0] if running else None)
        if job is None:
            return (f"There's no {name} running." if name else "Nothing's running."), ""
        tail = self._runner.tail(job, 25)
        if not tail.strip():
            return f"{job.name} hasn't printed anything yet.", ""
        return "", tail

    async def stop(self, name: str) -> str:
        return await asyncio.to_thread(self._runner.stop, name)


def _candidates(root: Path, depth: int = _SCAN_DEPTH) -> list[Path]:
    """Folders that look like projects, a couple of levels down. Deeper than
    that and a scan of Documents walks into other people's dependency trees."""
    found: list[Path] = []
    if not root.exists():
        return found
    stack = [(root, 0)]
    while stack:
        folder, level = stack.pop()
        try:
            entries = list(folder.iterdir())
        except OSError:
            continue
        if any(e.name in _MARKERS for e in entries):
            found.append(folder)
            continue        # a project's own subfolders are not more projects
        if level < depth:
            stack += [(e, level + 1) for e in entries
                      if e.is_dir() and e.name not in _SKIP_DIRS and not e.name.startswith(".")]
    return found


def _looks_gpu(proposal: Proposal) -> bool:
    """A guess, and only ever a default he can edit in the config."""
    words = f"{proposal.name} {proposal.command} {proposal.summary}".lower()
    return any(hint in words for hint in ("train", "cuda", "torch", "tensorflow", "whisper"))

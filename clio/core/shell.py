"""Vetting a spoken command before anything runs it (7.5).

This module decides one thing: whether a list of words may become a process.
It runs nothing itself.

**There is no shell.** A vetted command becomes an argument list handed
straight to the process, so `;`, `&&`, `|`, `>` and backticks are not dangerous
characters that got filtered out - they are ordinary characters with nothing
there to interpret them. Filtering a shell is a losing game; not having one
is not. The argument rules below are a second fence, not the first.

Everything else follows from treating the first word as the whole question:

- **A short list of known tools** can start a command. Anything else is refused
  *by name*, so he hears why rather than a shrug.
- **Interpreters are refused on purpose** - `cmd`, `powershell`, `bash`, `wsl` -
  because each would smuggle a shell back in through the front door.
- **Anything that fetches and runs code from the internet** is refused: `curl`,
  `certutil`, `npx`, and a package manager pointed at a URL.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

# The first word must be one of these.
ALLOWED = {
    "git", "pip", "pip3", "python", "python3", "py", "node", "npm", "cargo",
    "go", "make", "dotnet", "pytest", "ruff", "uv", "poetry", "nvidia-smi",
}

# Refused by name, with the reason he will hear.
BLOCKED = {
    **dict.fromkeys(["cmd", "powershell", "pwsh", "wsl", "bash", "sh", "start", "runas"],
                    "it would open a second shell, which is the one thing this is built to avoid"),
    **dict.fromkeys(["curl", "wget", "iwr", "invoke-webrequest", "certutil", "bitsadmin", "npx"],
                    "it downloads and runs things from the internet"),
    **dict.fromkeys(["del", "erase", "rd", "rmdir", "rm", "format"],
                    "it deletes things, and deleting goes through the Recycle Bin instead"),
    **dict.fromkeys(["reg", "regedit", "bcdedit", "diskpart", "vssadmin", "cipher",
                     "takeown", "icacls", "netsh", "net", "sc", "msiexec", "shutdown"],
                    "it changes how Windows itself is set up"),
    "schtasks": "scheduled tasks are how reminders work, and they manage their own",
}

# Only looking, so these run without asking. Kept exact on purpose: `git branch`
# lists branches, but `git branch -D x` deletes one.
_READ_ONLY_ANY_ARGS = {("git", "status"), ("git", "log"), ("git", "diff"), ("git", "show"),
                       ("pip", "list"), ("pip", "show"), ("pip", "freeze"),
                       ("pip3", "list"), ("npm", "list"), ("npm", "ls"), ("npm", "outdated")}
_READ_ONLY_BARE = {("git", "branch"), ("git", "remote"), ("nvidia-smi",)}

# Allowed, but the readback says what they destroy.
_DESTRUCTIVE: list[tuple[tuple[str, ...], tuple[str, ...], str]] = [
    (("git", "reset"), ("--hard",), "it throws away changes you haven't committed"),
    (("git", "clean"), (), "it deletes files git isn't tracking"),
    (("git", "push"), ("--force", "-f", "--force-with-lease"),
     "it can overwrite history on the remote"),
    (("git", "checkout"), (".", "--"), "it throws away changes you haven't committed"),
    (("git", "restore"), (), "it throws away changes you haven't committed"),
    (("git", "branch"), ("-d", "-D", "--delete"), "it deletes a branch"),
    (("pip", "uninstall"), (), "it removes a package"),
    (("pip3", "uninstall"), (), "it removes a package"),
    (("npm", "uninstall"), (), "it removes a package"),
    (("cargo", "clean"), (), "it deletes the build output"),
    (("make", "clean"), (), "it deletes the build output"),
]

# Flags that turn an interpreter into "run this code I just said".
_INLINE_CODE = {"python": {"-c"}, "python3": {"-c"}, "py": {"-c"},
                "node": {"-e", "--eval", "-p", "--print"}}

# One argument: letters, digits and the punctuation real commands use. Anything
# else is refused before a process is ever created.
_SAFE_ARG = re.compile(r"^[A-Za-z0-9._@:+=/\\-]+$")
_ABSOLUTE = re.compile(r"^(?:[A-Za-z]:|[/\\])")
_FETCH = re.compile(r"^(?:https?:|git\+|file:|ssh:|ftp:)", re.I)


@dataclass(frozen=True)
class Verdict:
    argv: list[str] = field(default_factory=list)
    refusal: str = ""          # empty when it may run
    read_only: bool = False    # may run without asking
    warning: str = ""          # said in the readback when it destroys something


def vet(words: list[str]) -> Verdict:
    """Whether these words may run, and on what terms."""
    if not words:
        return Verdict(refusal="There's no command there.")
    tool = words[0].lower()
    args = words[1:]

    if tool in BLOCKED:
        return Verdict(refusal=f"I won't run {tool} - {BLOCKED[tool]}.")
    if tool not in ALLOWED:
        return Verdict(refusal=(f"{tool} isn't something I'm set up to run. I can run "
                                "git, pip, python, npm, cargo and a few others."))

    for arg in args:
        if not _SAFE_ARG.match(arg):
            return Verdict(refusal=f"I won't run that - {arg!r} has characters I don't pass on.")
        if _ABSOLUTE.match(arg) or ".." in arg:
            return Verdict(refusal="I only run things inside the folder, not with paths out of it.")
        if _FETCH.match(arg):
            return Verdict(refusal=("I won't install from a link - that's running code from the "
                                    "internet. Use the package name instead."))
    if any(arg in _INLINE_CODE.get(tool, set()) for arg in args):
        return Verdict(refusal=f"I won't run code typed inline into {tool}. Put it in a file first.")

    return Verdict(argv=[tool, *args], read_only=_is_read_only(tool, args),
                   warning=_warning(tool, args))


def _is_read_only(tool: str, args: list[str]) -> bool:
    head = (tool, args[0].lower()) if args else (tool,)
    if head in _READ_ONLY_ANY_ARGS:
        return True
    return head in _READ_ONLY_BARE and len(args) <= 1


def _warning(tool: str, args: list[str]) -> str:
    lowered = [a.lower() for a in args]
    for prefix, flags, said in _DESTRUCTIVE:
        if (tool, *lowered[: len(prefix) - 1]) != prefix:
            continue
        if not flags or any(flag.lower() in lowered for flag in flags):
            return said
    return ""


# Python tools, run through the interpreter as modules.
_PYTHON_MODULES = {"pip": "pip", "pip3": "pip", "pytest": "pytest", "ruff": "ruff"}


def command_for(tool: str, folder: Path) -> list[str] | None:
    """How to start this tool in this folder: the start of the argument list.

    A project's own virtual environment wins, so "pip install requests in
    ewaste" installs into ewaste rather than whichever Python is first on the
    PATH - which is what he means by it.

    Python tools go through the interpreter (`python -m pip`), never through
    the `pip.exe` in the venv's Scripts folder. Those launchers have the path
    to their interpreter baked in, and a venv that has been moved or renamed
    leaves every one of them pointing at nothing: they exit 1 and print not a
    word. Found live, on this machine - the project was once called Jarvis, and
    every launcher in its venv still looks for Documents\\Jarvis.
    """
    interpreter = _venv_python(Path(folder))
    if interpreter is not None:
        if tool in ("python", "python3", "py"):
            return [interpreter]
        if tool in _PYTHON_MODULES:
            return [interpreter, "-m", _PYTHON_MODULES[tool]]
    found = shutil.which(tool)
    return [found] if found else None


def _venv_python(folder: Path) -> str | None:
    for venv in (".venv", "venv", "env"):
        candidate = folder / venv / "Scripts" / "python.exe"
        if candidate.exists():
            return str(candidate)
    return None

"""Changing his files (7.4): create, copy, rename, move, and recycle - with undo.

The write half 3.6 deliberately left out, and the first thing in Clio that can
change something he made. Three rules, and every function here enforces them
itself rather than trusting the caller:

1. **Only inside the configured roots**, checked on the fully resolved path, so
   neither `..` nor a shortcut can walk out of them.
2. **Nothing is ever overwritten.** A name that is taken becomes "name (2)",
   the way Explorer does it, so no operation here can lose a file by landing on
   top of one.
3. **Nothing is deleted.** Removal goes to the Recycle Bin through Windows'
   own `SHFileOperation`, and undo brings it back through the shell - both
   verified live on this machine before this module was written.

Standard library only. `send2trash` would have been one more dependency for
something `ctypes` does in twenty lines.
"""

from __future__ import annotations

import ctypes
import re
import shutil
import subprocess
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from clio.core.logging import get_logger

log = get_logger("clio.fileops")

UNDO_DEPTH = 10

# Characters Windows forbids in a name, plus the path separators that would turn
# a spoken name into a path. Whisper will produce both eventually.
_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
             *(f"lpt{i}" for i in range(1, 10))}
_MAX_NAME = 120


class FileOpError(Exception):
    """Something he asked for that must not happen, said in plain words."""


@dataclass(frozen=True)
class Done:
    kind: str                    # "create", "copy", "rename", "move", "recycle"
    target: Path                 # where the thing is now (or was, for a recycle)
    source: Path | None = None   # where it came from, for rename and move


class FileOps:
    def __init__(self, roots: tuple[Path, ...]):
        self._roots = tuple(Path(r).resolve() for r in roots)
        self._undo: deque[Done] = deque(maxlen=UNDO_DEPTH)

    # --- the fence ---

    def inside(self, path: Path) -> bool:
        """Resolved first: `Documents/../../Windows` is outside, whatever it
        looks like written down."""
        try:
            resolved = Path(path).resolve()
        except OSError:
            return False
        return any(resolved == root or resolved.is_relative_to(root) for root in self._roots)

    def _guard(self, *paths: Path) -> None:
        if not self._roots:
            raise FileOpError("I haven't been given any folders to work in")
        for path in paths:
            if not self.inside(path):
                raise FileOpError("that's outside the folders I'm allowed to change")

    # --- the operations ---

    def create_folder(self, parent: Path, name: str) -> Path:
        target = free_name(Path(parent) / clean_name(name))
        self._guard(parent, target)
        target.mkdir(parents=False)
        return self._done(Done("create", target))

    def create_file(self, parent: Path, name: str) -> Path:
        target = free_name(Path(parent) / clean_name(name))
        self._guard(parent, target)
        # "x": fails rather than truncate, if something appeared since free_name.
        target.open("x", encoding="utf-8").close()
        return self._done(Done("create", target))

    def copy(self, source: Path, folder: Path) -> Path:
        target = free_name(Path(folder) / Path(source).name)
        self._guard(source, folder, target)
        if Path(source).is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)
        return self._done(Done("copy", target))

    def rename(self, source: Path, new_name: str) -> Path:
        source = Path(source)
        name = clean_name(new_name)
        # Keep the extension unless he said a new one: "rename the report to
        # final" should not produce a file Windows no longer knows how to open.
        if source.is_file() and not Path(name).suffix:
            name += source.suffix
        target = free_name(source.with_name(name))
        self._guard(source, target)
        source.rename(target)
        return self._done(Done("rename", target, source))

    def move(self, source: Path, folder: Path) -> Path:
        source, folder = Path(source), Path(folder)
        if source.is_dir() and (folder.resolve() == source.resolve()
                                or folder.resolve().is_relative_to(source.resolve())):
            raise FileOpError("I can't move a folder inside itself")
        target = free_name(folder / source.name)
        self._guard(source, folder, target)
        shutil.move(str(source), str(target))
        return self._done(Done("move", target, source))

    def recycle(self, path: Path) -> Path:
        path = Path(path)
        self._guard(path)
        if not path.exists():
            raise FileOpError(f"{path.name} isn't there any more")
        _send_to_recycle_bin(path)
        if path.exists():
            raise FileOpError(f"Windows wouldn't put {path.name} in the Recycle Bin")
        return self._done(Done("recycle", path))

    # --- undo ---

    def undo(self) -> str:
        """Reverses the most recent action; called again, the one before it.
        Undo is not itself undoable - redo is a feature nobody asked for."""
        if not self._undo:
            return "There's nothing for me to undo."
        done = self._undo.pop()
        try:
            if done.kind in ("create", "copy"):
                # Recycled rather than deleted, so even an undo loses nothing.
                if done.target.exists():
                    _send_to_recycle_bin(done.target)
                return f"Undone - {done.target.name} is in the Recycle Bin."
            if done.kind in ("rename", "move"):
                if not done.target.exists():
                    return f"I can't undo that - {done.target.name} has moved since."
                back = free_name(done.source)
                shutil.move(str(done.target), str(back))
                return f"Undone - {back.name} is back where it was."
            if done.kind == "recycle":
                if _restore_from_recycle_bin(done.target):
                    return f"Undone - {done.target.name} is back."
                return (f"I couldn't bring {done.target.name} back myself. "
                        "It's in the Recycle Bin, where you can restore it.")
        except OSError as exc:
            log.warning("Undo failed",
                        extra={"extra_fields": {"kind": done.kind, "error": str(exc)}})
            return "I couldn't undo that - Windows said no."
        return "There's nothing for me to undo."

    def history(self) -> list[Done]:
        return list(self._undo)

    def _done(self, done: Done) -> Path:
        self._undo.append(done)
        log.info("File changed", extra={"extra_fields": {
            "kind": done.kind, "target": done.target.name,
            "source": done.source.name if done.source else ""}})
        return done.target


def clean_name(name: str) -> str:
    """A spoken name made safe to be a name - and only a name, never a path."""
    cleaned = _FORBIDDEN.sub("", " ".join(name.split())).replace("..", "").strip(" .")
    if not cleaned:
        raise FileOpError("that isn't a name I can give a file")
    if Path(cleaned).stem.lower() in _RESERVED:
        raise FileOpError(f"Windows won't allow a file called {cleaned}")
    return cleaned[:_MAX_NAME]


def free_name(path: Path) -> Path:
    """The path itself if free, otherwise "name (2)", "name (3)" and so on."""
    path = Path(path)
    if not path.exists():
        return path
    stem, suffix = (path.name, "") if path.is_dir() else (path.stem, path.suffix)
    for number in range(2, 1000):
        candidate = path.with_name(f"{stem} ({number}){suffix}")
        if not candidate.exists():
            return candidate
    raise FileOpError(f"there are already too many copies of {path.name}")


# --- the Recycle Bin, through Windows itself ---

_FO_DELETE = 3
_FLAGS = 0x0040 | 0x0010 | 0x0004 | 0x0400   # allow undo, no confirm, silent, no error UI


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("wFunc", ctypes.c_uint),
        ("pFrom", ctypes.c_wchar_p),
        ("pTo", ctypes.c_wchar_p),
        ("fFlags", ctypes.c_uint16),
        ("fAnyOperationsAborted", ctypes.c_int),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", ctypes.c_wchar_p),
    ]


def _send_to_recycle_bin(path: Path) -> None:
    operation = _SHFILEOPSTRUCTW()
    operation.wFunc = _FO_DELETE
    # Double-null terminated: the API takes a list of paths.
    operation.pFrom = str(Path(path).resolve()) + "\0\0"
    operation.fFlags = _FLAGS
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    if result != 0 or operation.fAnyOperationsAborted:
        raise OSError(f"SHFileOperationW failed with {result}")


_RESTORE = r"""
$ErrorActionPreference = 'Stop'
$target = '{path}'
$bin = (New-Object -ComObject Shell.Application).NameSpace(10)
$hit = $null
foreach ($item in $bin.Items()) {{
    if ((Join-Path $bin.GetDetailsOf($item, 1) $item.Name) -eq $target) {{ $hit = $item }}
}}
if ($hit -eq $null) {{ Write-Output 'NOT_FOUND'; exit 0 }}
$hit.InvokeVerb('undelete')
Write-Output 'RESTORED'
"""


def _restore_from_recycle_bin(path: Path) -> bool:
    """Through the shell, the same way Explorer's Restore does it. The alternative
    - moving files out of $Recycle.Bin by hand - reaches into Windows internals.
    Checked by looking at the disk afterwards, not by trusting the output."""
    target = str(Path(path).resolve()).replace("'", "''")
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             _RESTORE.format(path=target)],
            capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return Path(path).exists()

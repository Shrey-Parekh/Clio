"""Reading what he copied, changing it, and putting it back.

"Fix the grammar in what I just copied" is the shape of this whole capability:
he copies, says one sentence, pastes. So replacing the clipboard is FREE
rather than confirmed - a capability that asks permission every time is slower
than opening a text box and doing it himself, which defeats the point.

What makes that safe is that the previous contents are kept, so "put it back"
undoes it. What was replaced is the only thing at risk, and it is one sentence
away from being restored.

Two things it will not do:

- Reading is local and stays local. Only a transform sends anything anywhere,
  because only a transform needs a model.
- It refuses to send something that looks like a credential. A clipboard is
  where passwords and API keys live in transit, and "summarise what I copied"
  said while a key is on the clipboard should not put that key in a prompt.

The instruction is passed through verbatim rather than matched against a list
of supported transforms. "Make it less passive aggressive" is not something
anyone would enumerate, and the model can already do it.
"""

from __future__ import annotations

import ctypes
import re
from ctypes import wintypes
from dataclasses import dataclass

from clio.core.logging import get_logger

log = get_logger("clio.clipboard")

_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002
_MAX_TRANSFORM_CHARS = 8000

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
# Every signature is declared. Without argtypes, ctypes marshals a 64-bit
# handle through a C int and SetClipboardData raises "int too long to convert"
# - which is how this was found, on the first real round trip.
_user32.OpenClipboard.argtypes = [wintypes.HWND]
_user32.GetClipboardData.argtypes = [wintypes.UINT]
_user32.GetClipboardData.restype = wintypes.HANDLE
_user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
_user32.SetClipboardData.restype = wintypes.HANDLE
_kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
_kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
_kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalFree.restype = wintypes.HGLOBAL

_STRIP = re.compile(r"[.!?,;:]+$")

# Every pattern needs an explicit reference to the clipboard. Without one,
# "make it shorter" is a remark about the conversation, not a request to
# rewrite whatever happens to be copied.
_REFERS = r"(?:my |the )?clipboard|what i (?:just )?copied|the (?:copied|pasted) (?:text|thing)"

_PATTERNS: list[tuple[str, str]] = [
    ("restore", rf"(?:put|change|set) (?:it |(?:{_REFERS}) )?back|undo (?:that|the clipboard)|"
                rf"restore (?:{_REFERS})"),
    ("read", rf"what(?:'?s| is) (?:in |on )?(?:{_REFERS})|read (?:me )?(?:{_REFERS})|"
             rf"what did i (?:just )?copy"),
    ("transform", rf"^(?P<instruction>.*\b(?:{_REFERS})\b.*)$"),
]

_COMPILED = [(kind, re.compile(p)) for kind, p in _PATTERNS]

# A transform must also ask for a change - otherwise every sentence that
# merely mentions the clipboard becomes a rewrite request.
_CHANGE_VERB = re.compile(
    r"\b(?:fix|correct|rewrite|reword|rephrase|shorten|lengthen|expand|summari[sz]e|"
    r"translate|clean|tidy|polish|improve|simplify|formalise|formalize|proofread|"
    r"make|turn|convert|capitali[sz]e|bullet)\b"
)

# Shapes a secret takes. Deliberately blunt: a false positive costs him one
# rephrase, a false negative puts a key in a prompt.
_SECRET = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY|"
    r"\b(?:sk|pk|ghp|gho|xox[baprs]|AKIA|AIza)[-_][A-Za-z0-9_-]{10,}|"
    r"\b(?:sk|ghp|gho|AKIA|AIza)[A-Za-z0-9_-]{16,}|"
    r"\b(?:password|passwd|api[_ -]?key|secret|token)\s*[:=]\s*\S+",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Request:
    kind: str
    instruction: str = ""


def parse_clipboard_request(text: str) -> Request | None:
    lowered = " ".join(_STRIP.sub("", text.strip().lower()).split())
    for kind, pattern in _COMPILED:
        found = pattern.search(lowered)
        if found is None:
            continue
        if kind != "transform":
            return Request(kind=kind)
        if not _CHANGE_VERB.search(lowered):
            return None
        return Request(kind="transform", instruction=found.group("instruction"))
    return None


def read_text() -> str | None:
    """None means there is no text on the clipboard - it may be empty, or hold
    an image, which is not the same thing as an error."""
    if not _user32.OpenClipboard(None):
        return None
    try:
        handle = _user32.GetClipboardData(_CF_UNICODETEXT)
        if not handle:
            return None
        pointer = _kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.c_wchar_p(pointer).value
        finally:
            _kernel32.GlobalUnlock(handle)
    finally:
        _user32.CloseClipboard()


def write_text(text: str) -> bool:
    if not _user32.OpenClipboard(None):
        return False
    try:
        _user32.EmptyClipboard()
        size = (len(text) + 1) * ctypes.sizeof(ctypes.c_wchar)
        handle = _kernel32.GlobalAlloc(_GMEM_MOVEABLE, size)
        if not handle:
            return False
        pointer = _kernel32.GlobalLock(handle)
        ctypes.memmove(pointer, ctypes.create_unicode_buffer(text), size)
        _kernel32.GlobalUnlock(handle)
        # Windows owns the memory once this succeeds, so it must not be freed
        # here - and must be freed if it fails, or the block leaks.
        if not _user32.SetClipboardData(_CF_UNICODETEXT, handle):
            _kernel32.GlobalFree(handle)
            return False
        return True
    finally:
        _user32.CloseClipboard()


def looks_like_a_secret(text: str) -> bool:
    return _SECRET.search(text) is not None


def summarise_for_speech(text: str, limit: int = 240) -> str:
    """Clipboards hold pages. Reading one out in full is unlistenable, so a
    long one is described rather than recited."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return f"{len(flat.split())} words, starting: {flat[:limit].rstrip()}..."


class Clipboard:
    """Holds what was replaced, so a transform is always one sentence away from
    being undone."""

    def __init__(self) -> None:
        self._previous: str | None = None

    def read(self) -> str:
        text = read_text()
        if not text:
            return "There's nothing on your clipboard - or it's not text."
        return summarise_for_speech(text)

    def restore(self) -> str:
        if self._previous is None:
            return "I haven't changed your clipboard, so there's nothing to put back."
        if not write_text(self._previous):
            return "Windows wouldn't let me write to the clipboard."
        self._previous = None
        return "Put it back."

    def take(self) -> tuple[str, str]:
        """The text to transform, or the reason not to. Exactly one is set."""
        text = read_text()
        if not text or not text.strip():
            return "", "There's nothing on your clipboard - or it's not text."
        if looks_like_a_secret(text):
            log.warning("Refused to send clipboard contents that look like a credential")
            return "", ("That looks like a password or a key, so I'm not sending it anywhere. "
                        "Copy something else and ask again.")
        return text[:_MAX_TRANSFORM_CHARS], ""

    def replace(self, original: str, result: str) -> str:
        if not write_text(result):
            return "I worked it out but Windows wouldn't let me write to the clipboard."
        self._previous = original
        log.info("Clipboard replaced", extra={"extra_fields": {"chars": len(result)}})
        return f"{summarise_for_speech(result, limit=200)} It's on your clipboard."

"""Type text into whatever window has focus, via SendInput.

KEYEVENTF_UNICODE sends characters by code unit rather than by key, so it types
the same into any app regardless of keyboard layout, and needs no mapping from
letters to virtual keys. Characters outside the BMP go as UTF-16 surrogate
pairs, which is exactly what SendInput expects.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

from clio.core.logging import get_logger

log = get_logger("clio.typing")

_INPUT_KEYBOARD = 1
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004
_ULONG_PTR = wintypes.WPARAM  # pointer-sized, so the structs are right on 64-bit


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", _ULONG_PTR),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD), ("dwExtraInfo", _ULONG_PTR),
    ]


class _INPUTUNION(ctypes.Union):
    # Both members present so the union is sized to the larger (MOUSEINPUT);
    # a keyboard-only union would be too small and SendInput would read past it.
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


_user32 = ctypes.windll.user32
_user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
_user32.SendInput.restype = wintypes.UINT


def _utf16_units(text: str) -> list[int]:
    """Each UTF-16 code unit as an int. A BMP char is one unit; an emoji or
    other astral char is a surrogate pair, which SendInput sends as two."""
    raw = text.encode("utf-16-le")
    return [int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)]


def type_text(text: str) -> bool:
    """Type `text` into the focused window. Returns False if Windows accepted
    none of the events (no focused target, or the input was blocked)."""
    if not text:
        return True

    events = []
    for unit in _utf16_units(text):
        for flags in (_KEYEVENTF_UNICODE, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP):
            event = _INPUT(type=_INPUT_KEYBOARD)
            event.u.ki = _KEYBDINPUT(wVk=0, wScan=unit, dwFlags=flags, time=0, dwExtraInfo=0)
            events.append(event)

    array = (_INPUT * len(events))(*events)
    sent = _user32.SendInput(len(events), array, ctypes.sizeof(_INPUT))
    if sent != len(events):
        log.warning("Typed fewer events than sent",
                    extra={"extra_fields": {"sent": sent, "of": len(events)}})
    return sent > 0


if __name__ == "__main__":
    assert _utf16_units("ab") == [0x61, 0x62]
    assert _utf16_units("é") == [0xE9]
    assert len(_utf16_units("😀")) == 2, "an astral char is a surrogate pair"
    assert type_text("") is True, "empty types nothing and succeeds"
    print("typing _utf16_units OK")

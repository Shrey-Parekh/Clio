"""One screenshot, on request only (8.1).

By default only the window in front is taken: it is sharper after shrinking,
cheaper to send, and the other windows on screen (chats, email) never leave the
machine unless he asks for the whole screen. Nothing is written to disk - the
picture exists as JPEG bytes in memory for one model call, then is gone.

Win32 through ctypes, so nothing new is installed for the window work; Pillow
does the grab, the shrink and the JPEG.
"""

from __future__ import annotations

import ctypes
import io
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

LONGEST_SIDE = 1600   # text in a window stays readable; a 4K grab is ~4x the tokens
JPEG_QUALITY = 85

# Clio's own windows: the chat window he may have typed the question into, and
# the HUD. Asking about "this" never means them.
_OWN_EXE = "clio.exe"


class CaptureError(Exception):
    """No picture could be taken (locked screen, secure desktop)."""


@dataclass(frozen=True)
class Window:
    hwnd: int
    title: str
    exe: str
    rect: tuple[int, int, int, int]   # left, top, right, bottom, in physical pixels
    visible: bool = True
    minimised: bool = False
    cloaked: bool = False             # hidden by DWM: suspended Store apps, other desktops


def pick(windows: list[Window]) -> Window | None:
    """The window he means, from a Z-order list that starts at the foreground
    window. Clio's own windows are stepped past to the one behind them."""
    for w in windows:
        left, top, right, bottom = w.rect
        # A minimised window still reports visible, parked at -32000.
        if w.exe.lower() == _OWN_EXE or not w.visible or w.cloaked or w.minimised:
            continue
        # ponytail: a title is the cheap test for "a real app window" - it skips
        # the untitled helper windows Windows keeps visible. A real app with an
        # empty title would be passed over for the one behind it.
        if not w.title or right - left <= 0 or bottom - top <= 0:
            continue
        return w
    return None


def shrink(image) -> bytes:
    """JPEG bytes, longest side at most LONGEST_SIDE."""
    image = image.convert("RGB")
    image.thumbnail((LONGEST_SIDE, LONGEST_SIDE))   # keeps the aspect, never enlarges
    out = io.BytesIO()
    image.save(out, "JPEG", quality=JPEG_QUALITY)
    return out.getvalue()


# --- Win32 -----------------------------------------------------------------

_user32 = ctypes.windll.user32 if hasattr(ctypes, "windll") else None
_GW_HWNDNEXT = 2
_DWMWA_EXTENDED_FRAME_BOUNDS = 9
_DWMWA_CLOAKED = 14
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_MONITOR_DEFAULTTONEAREST = 2
# Per-monitor aware, so every rectangle is in the same physical pixels Pillow
# grabs in. Without it a 150%-scaled monitor reports two-thirds-size rectangles.
_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
_MAX_WALK = 200   # windows below the foreground worth looking through


def process_id(hwnd: int) -> int:
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _exe(hwnd: int) -> str:
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, process_id(hwnd))
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(260)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return Path(buf.value).name
        return ""
    finally:
        kernel32.CloseHandle(handle)


def _describe(hwnd: int) -> Window:
    dwm = ctypes.windll.dwmapi
    rect = wintypes.RECT()
    # The extended frame leaves out the invisible resize border GetWindowRect
    # includes, which would otherwise grab a strip of whatever is behind.
    if dwm.DwmGetWindowAttribute(hwnd, _DWMWA_EXTENDED_FRAME_BOUNDS,
                                 ctypes.byref(rect), ctypes.sizeof(rect)) != 0:
        _user32.GetWindowRect(hwnd, ctypes.byref(rect))
    cloaked = wintypes.DWORD()
    dwm.DwmGetWindowAttribute(hwnd, _DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
    length = _user32.GetWindowTextLengthW(hwnd)
    title = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, title, length + 1)
    return Window(
        hwnd=hwnd, title=title.value, exe=_exe(hwnd),
        rect=(rect.left, rect.top, rect.right, rect.bottom),
        visible=bool(_user32.IsWindowVisible(hwnd)),
        minimised=bool(_user32.IsIconic(hwnd)),
        cloaked=bool(cloaked.value),
    )


def _z_order() -> list[Window]:
    hwnd = _user32.GetForegroundWindow()
    found = []
    while hwnd and len(found) < _MAX_WALK:
        found.append(_describe(hwnd))
        hwnd = _user32.GetWindow(hwnd, _GW_HWNDNEXT)
    return found


def _monitor_of(hwnd: int | None) -> tuple[int, int, int, int]:
    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    if hwnd:
        monitor = _user32.MonitorFromWindow(hwnd, _MONITOR_DEFAULTTONEAREST)
    else:
        monitor = _user32.MonitorFromPoint(wintypes.POINT(0, 0), _MONITOR_DEFAULTTONEAREST)
    info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    _user32.GetMonitorInfoW(monitor, ctypes.byref(info))
    r = info.rcMonitor
    return (r.left, r.top, r.right, r.bottom)


def capture(whole: bool) -> tuple[bytes, str]:
    """JPEG bytes of the window in front (or its whole monitor), and what was
    taken, for the log. Blocking - run it in a thread."""
    from PIL import ImageGrab

    # Per thread, and this runs on a worker thread, so the rest of Clio's
    # coordinates are untouched.
    _user32.SetThreadDpiAwarenessContext(_PER_MONITOR_AWARE_V2)
    window = pick(_z_order())
    if whole or window is None:
        bbox, label = _monitor_of(window.hwnd if window else None), "whole screen"
    else:
        bbox, label = window.rect, window.title
    try:
        image = ImageGrab.grab(bbox=bbox, all_screens=True)
    except OSError as exc:
        # Locked screen, UAC prompt: the secure desktop can't be read.
        raise CaptureError(str(exc)) from exc
    return shrink(image), label

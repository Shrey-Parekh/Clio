"""A global mouse-button trigger — the wake word, fired by a spare button.

There is no RegisterHotKey for the mouse, so this uses a low-level mouse hook
(WH_MOUSE_LL) on its own message-loop thread, the same shape as the keyboard
listener. The hook watches one configured button (a side button or the middle
button) and passes every event straight through, so the button still works
normally — it fires Clio as well, it doesn't steal the click.

  ponytail: a low-level hook sees every mouse event, so the callback stays O(1)
  and returns fast (Windows silently drops a slow LL hook). A full gesture
  recogniser would track pointer paths; a button covers the real need.
"""

from __future__ import annotations

import ctypes
import threading
from collections.abc import Callable
from ctypes import wintypes

from clio.core.logging import get_logger

log = get_logger("clio.mouse")

_WH_MOUSE_LL = 14
_WM_QUIT = 0x0012
_WM_MBUTTONDOWN = 0x0207
_WM_XBUTTONDOWN = 0x020B

# name -> (window message, which X button: 1, 2, or 0 for non-X buttons)
_BUTTONS = {
    "middle": (_WM_MBUTTONDOWN, 0),
    "x1": (_WM_XBUTTONDOWN, 1), "back": (_WM_XBUTTONDOWN, 1),
    "x2": (_WM_XBUTTONDOWN, 2), "forward": (_WM_XBUTTONDOWN, 2),
}

_LRESULT = ctypes.c_ssize_t
_HOOKPROC = ctypes.CFUNCTYPE(_LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)


class _POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", _POINT), ("mouseData", wintypes.DWORD), ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_void_p),
    ]


def parse_button(name: str) -> tuple[int, int, str]:
    """"x2" -> (window message, x-button number, normalised name).

    Only spare buttons are offered — hooking the left or right button would
    hijack ordinary clicking."""
    key = name.strip().lower()
    if key not in _BUTTONS:
        raise ValueError(f"unusable mouse button {name!r} - use middle, x1/back or x2/forward")
    message, xbutton = _BUTTONS[key]
    return message, xbutton, key


class MouseTrigger:
    """Installs one low-level mouse hook and calls `on_press` on the asyncio
    loop when the configured button goes down. `on_press` must be quick and
    non-blocking — set an event, no more."""

    def __init__(self, button: str, on_press: Callable[[], None], loop) -> None:
        self._message, self._xbutton, self.name = parse_button(button)
        self._on_press = on_press
        self._loop = loop
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._hook = None
        self._proc = None  # kept alive; a GC'd callback crashes the hook
        self.installed = False

    def start(self) -> bool:
        """Starts the listener thread and waits for the hook to install.
        Returns False if the OS refused it — Clio still runs on the wake word."""
        self._thread = threading.Thread(target=self._run, name="clio-mouse", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=2.0)
        return self.installed

    def stop(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            return
        ctypes.windll.user32.PostThreadMessageW(self._thread_id, _WM_QUIT, 0, 0)
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self._thread_id = kernel32.GetCurrentThreadId()

        def _proc(n_code, w_param, l_param):
            if n_code == 0 and w_param == self._message and self._matches(l_param):
                self._loop.call_soon_threadsafe(self._on_press)
            return user32.CallNextHookEx(None, n_code, w_param, l_param)

        self._proc = _HOOKPROC(_proc)
        self._hook = user32.SetWindowsHookExW(
            _WH_MOUSE_LL, self._proc, kernel32.GetModuleHandleW(None), 0
        )
        self.installed = bool(self._hook)
        self._ready.set()
        if not self.installed:
            log.warning("Mouse hook not installed", extra={"extra_fields": {"button": self.name}})
            return

        log.info("Mouse trigger armed", extra={"extra_fields": {"button": self.name}})
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                pass  # the hook fires on its own; this loop only keeps the thread pumping
        finally:
            user32.UnhookWindowsHookEx(self._hook)

    def _matches(self, l_param) -> bool:
        # The side buttons share one message, so check which one fired.
        if not self._xbutton:
            return True
        info = ctypes.cast(l_param, ctypes.POINTER(_MSLLHOOKSTRUCT)).contents
        return ((info.mouseData >> 16) & 0xFFFF) == self._xbutton


if __name__ == "__main__":
    assert parse_button("x2") == (_WM_XBUTTONDOWN, 2, "x2")
    assert parse_button("back") == (_WM_XBUTTONDOWN, 1, "back")
    assert parse_button("MIDDLE") == (_WM_MBUTTONDOWN, 0, "middle")
    for bad in ["left", "right", "", "scroll"]:
        try:
            parse_button(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{bad!r} should have been rejected")
    print("mouse parse_button OK")

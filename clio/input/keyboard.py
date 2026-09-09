"""A hold-to-talk key: a low-level keyboard hook reporting key down and key up.

RegisterHotKey (used for the tap hotkey in 4.1) only signals a press, never a
release, so push-to-talk needs a WH_KEYBOARD_LL hook instead — the keyboard
sibling of the mouse hook. It watches one key and passes every event through,
so the key still works normally.

  ponytail: near-identical plumbing to input/mouse.py (message-loop thread,
  SetWindowsHookEx, GetMessage). Two low-level hooks don't yet justify a shared
  base; unify them if a third arrives.
"""

from __future__ import annotations

import ctypes
import threading
from collections.abc import Callable
from ctypes import wintypes

from clio.core.logging import get_logger
from clio.input.hotkey import _vk_for  # same letter/digit/F-key/space VK mapping

log = get_logger("clio.keyboard")

_WH_KEYBOARD_LL = 13
_WM_QUIT = 0x0012
_DOWN = {0x0100, 0x0104}  # WM_KEYDOWN, WM_SYSKEYDOWN
_UP = {0x0101, 0x0105}    # WM_KEYUP, WM_SYSKEYUP

_LRESULT = ctypes.c_ssize_t
_HOOKPROC = ctypes.CFUNCTYPE(_LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD), ("dwExtraInfo", wintypes.WPARAM),
    ]


def parse_key(key: str) -> tuple[int, str]:
    """"f8" -> (virtual-key code, normalised name). A single key, no modifier —
    push-to-talk is one key you hold."""
    return _vk_for(key), key.strip().lower()


class KeyListener:
    """Installs one low-level keyboard hook and calls `on_press`/`on_release` on
    the asyncio loop when the configured key goes down and up. Both callbacks
    must be quick and non-blocking."""

    def __init__(self, key: str, on_press: Callable[[], None],
                 on_release: Callable[[], None], loop) -> None:
        self._vk, self.name = parse_key(key)
        self._on_press = on_press
        self._on_release = on_release
        self._loop = loop
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._hook = None
        self._proc = None  # kept alive; a GC'd callback crashes the hook
        self.installed = False

    def start(self) -> bool:
        self._thread = threading.Thread(target=self._run, name="clio-ptt", daemon=True)
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
            if n_code == 0:
                vk = ctypes.cast(l_param, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents.vkCode
                if vk == self._vk:
                    if w_param in _DOWN:
                        self._loop.call_soon_threadsafe(self._on_press)
                    elif w_param in _UP:
                        self._loop.call_soon_threadsafe(self._on_release)
            return user32.CallNextHookEx(None, n_code, w_param, l_param)

        self._proc = _HOOKPROC(_proc)
        self._hook = user32.SetWindowsHookExW(
            _WH_KEYBOARD_LL, self._proc, kernel32.GetModuleHandleW(None), 0
        )
        self.installed = bool(self._hook)
        self._ready.set()
        if not self.installed:
            log.warning("Key hook not installed", extra={"extra_fields": {"key": self.name}})
            return

        log.info("Push-to-talk armed", extra={"extra_fields": {"key": self.name}})
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                pass  # the hook fires on its own; this loop only keeps the thread pumping
        finally:
            user32.UnhookWindowsHookEx(self._hook)


if __name__ == "__main__":
    assert parse_key("f8") == (0x77, "f8")
    assert parse_key("Space")[0] == 0x20
    for bad in ["", "ctrl+a", "enter"]:
        try:
            parse_key(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{bad!r} should have been rejected")
    print("keyboard parse_key OK")

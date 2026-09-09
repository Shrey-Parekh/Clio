"""A global keyboard hotkey - the wake word, triggered by a key instead.

Windows' own `RegisterHotKey` is the native answer: a system-wide combo that
fires no matter which app has focus, including windowed-fullscreen apps. No
driver, no polling, no keyboard hook that sees every keystroke - the OS routes
just the one combo to us and leaves the rest alone.

It has to run on a thread with a Win32 message loop, because WM_HOTKEY is
posted to a thread's message queue and only `GetMessage` drains it. That thread
does nothing but wait for the key and hand the press back to the asyncio loop,
which is the sole owner of everything that happens next.

  ponytail: exclusive-fullscreen games can swallow RegisterHotKey (they own the
  input queue). Windowed and borderless-fullscreen are fine, which covers the
  real case. A low-level WH_KEYBOARD_LL hook would reach even those, but it sees
  every keystroke system-wide - a much bigger blast radius for a marginal gain.
"""

from __future__ import annotations

import ctypes
import threading
from collections.abc import Callable
from ctypes import wintypes

from clio.core.logging import get_logger

log = get_logger("clio.hotkey")

# RegisterHotKey modifier flags.
_MOD = {"ctrl": 0x0002, "control": 0x0002, "alt": 0x0001, "shift": 0x0004,
        "win": 0x0008, "super": 0x0008, "cmd": 0x0008}
_MOD_NOREPEAT = 0x4000  # one press = one fire, not a stream while held

_WM_HOTKEY = 0x0312
_WM_QUIT = 0x0012
_HOTKEY_ID = 1


def _vk_for(key: str) -> int:
    """Virtual-key code for the one non-modifier key in the combo.

    Letters and digits map to their ASCII uppercase, which is exactly their VK
    code on Windows; F1-F24 and space are spelled out. Anything else is
    rejected rather than guessed - a wrong VK is a hotkey that silently never
    fires."""
    key = key.strip().lower()
    if len(key) == 1 and (key.isalpha() or key.isdigit()):
        return ord(key.upper())
    if key == "space":
        return 0x20
    if key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        return 0x70 + int(key[1:]) - 1
    raise ValueError(
        f"unusable hotkey key {key!r} - use a letter, digit, F1-F24 or 'space'"
    )


def parse_combo(combo: str) -> tuple[int, int, str]:
    """"ctrl+alt+c" -> (modifier bitmask, vk, normalised name).

    At least one modifier is required: a bare key would be stolen from every
    other app the moment Clio starts."""
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    if len(parts) < 2:
        raise ValueError(f"hotkey {combo!r} needs a modifier and a key, e.g. ctrl+alt+c")

    mods = 0
    names = []
    key = None
    for part in parts:
        if part in _MOD:
            bit = _MOD[part]
            if not mods & bit:
                mods |= bit
                names.append(part)
        elif key is None:
            key = part
        else:
            raise ValueError(f"hotkey {combo!r} has more than one non-modifier key")
    if not mods:
        raise ValueError(f"hotkey {combo!r} needs at least one modifier (ctrl/alt/shift/win)")
    if key is None:
        raise ValueError(f"hotkey {combo!r} is all modifiers and no key")

    vk = _vk_for(key)
    return mods | _MOD_NOREPEAT, vk, "+".join([*names, key])


class HotkeyListener:
    """Registers one global hotkey and calls `on_press` on the asyncio loop
    each time it fires. `on_press` runs on the loop thread, so it must be quick
    and non-blocking - set an event, no more."""

    def __init__(self, combo: str, on_press: Callable[[], None], loop) -> None:
        self._mods, self._vk, self.name = parse_combo(combo)
        self._on_press = on_press
        self._loop = loop
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self.registered = False

    def start(self) -> bool:
        """Starts the listener thread and waits for it to report whether the
        OS accepted the hotkey. Returns False if it did not (usually because
        another app already owns that combo) - Clio still runs on the wake word."""
        self._thread = threading.Thread(target=self._run, name="clio-hotkey", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=2.0)
        return self.registered

    def stop(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            return
        ctypes.windll.user32.PostThreadMessageW(self._thread_id, _WM_QUIT, 0, 0)
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        user32 = ctypes.windll.user32
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        self.registered = bool(user32.RegisterHotKey(None, _HOTKEY_ID, self._mods, self._vk))
        self._ready.set()
        if not self.registered:
            log.warning("Hotkey not registered - already taken?",
                        extra={"extra_fields": {"combo": self.name}})
            return

        log.info("Hotkey armed", extra={"extra_fields": {"combo": self.name}})
        msg = wintypes.MSG()
        try:
            # GetMessage returns 0 on WM_QUIT, -1 on error, >0 otherwise.
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == _WM_HOTKEY and msg.wParam == _HOTKEY_ID:
                    self._loop.call_soon_threadsafe(self._on_press)
        finally:
            user32.UnregisterHotKey(None, _HOTKEY_ID)


if __name__ == "__main__":
    # Parser is the only piece testable without a live message loop.
    assert parse_combo("ctrl+alt+c") == (0x0002 | 0x0001 | _MOD_NOREPEAT, ord("C"), "ctrl+alt+c")
    assert parse_combo("win+space")[1] == 0x20
    assert parse_combo("ctrl+shift+f5")[1] == 0x74
    for bad in ["c", "ctrl", "ctrl+a+b", "ctrl+enter", ""]:
        try:
            parse_combo(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{bad!r} should have been rejected")
    print("hotkey parse_combo OK")

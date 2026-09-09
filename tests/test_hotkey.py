"""The global hotkey: parse a combo into a Win32 modifier+key, and turn a key
press into the same interrupt the wake word raises.

The RegisterHotKey / message-loop half only works against a live Windows
message queue and would register a real system hotkey, so it is verified in a
live session, not here. What is testable is the parser (a wrong VK is a hotkey
that silently never fires) and the flag contract the run loop depends on.
Run: python tests/test_hotkey.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clio.core.config import HotkeyConfig  # noqa: E402
from clio.input.hotkey import _MOD_NOREPEAT, parse_combo  # noqa: E402
from clio.orchestrator import Orchestrator  # noqa: E402


def build(hotkey=None):
    return Orchestrator(
        wake_detector=None, turn_detector=None, stt=None, llm=None, speaker=None,
        persona_system_prompt="p", follow_up_window_s=1.0, hotkey=hotkey,
    )


def main():
    # --- a combo becomes modifiers + a virtual key ---

    mods, vk, name = parse_combo("ctrl+alt+c")
    assert mods == (0x0002 | 0x0001 | _MOD_NOREPEAT), hex(mods)
    assert vk == ord("C") and name == "ctrl+alt+c"
    # No-repeat is always on: holding the key must fire once, not stream.
    assert mods & _MOD_NOREPEAT
    assert parse_combo("win+space")[1] == 0x20
    assert parse_combo("ctrl+shift+f5")[1] == 0x74  # VK_F5
    assert parse_combo("CTRL + ALT + K")[2] == "ctrl+alt+k", "case and spaces don't matter"
    print("OK  a combo parses to the right modifiers and key")

    # --- what must be rejected before it becomes a silent dead key ---

    for bad, why in [
        ("c", "a bare key would be stolen from every other app"),
        ("ctrl", "all modifiers, no key"),
        ("ctrl+a+b", "two non-modifier keys"),
        ("ctrl+enter", "enter has no VK in the supported set"),
        ("", "empty"),
    ]:
        try:
            parse_combo(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} should have been rejected: {why}")
    print("OK  modifier-less, over-full and unknown combos are refused")

    # --- a press raises the same interrupt the wake word does ---

    orchestrator = build(HotkeyConfig(enabled=True, combo="ctrl+alt+c"))
    assert not orchestrator._triggered and not orchestrator._announcement_ready.is_set()
    orchestrator._fire_trigger("hotkey")
    assert orchestrator._triggered, "the loop reads this to know it was a trigger, not a timer"
    assert orchestrator._trigger_source == "hotkey", "the source is logged"
    assert orchestrator._announcement_ready.is_set(), "the wake wait breaks on this event"
    print("OK  a press sets the flag and the interrupt the wake wait watches")

    # --- disabled or absent means no listener at all ---

    for cfg in [None, HotkeyConfig(enabled=False, combo="ctrl+alt+c")]:
        orch = build(cfg)
        orch._start_triggers()
        assert orch._hotkey_listener is None, f"no listener expected for {cfg}"
    print("OK  a disabled or missing hotkey config starts no listener")

    print("\nAll hotkey checks passed.")


if __name__ == "__main__":
    main()

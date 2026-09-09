"""The mouse-button trigger: parse a button into a Win32 message, and feed the
same manual-trigger path the hotkey uses.

The hook / message-loop half only works against a live Windows message queue
and installs a real system hook, so it is verified in a live session. What is
testable is the parser and the flag contract the run loop depends on.
Run: python tests/test_mouse.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clio.core.config import MouseConfig  # noqa: E402
from clio.input.mouse import _WM_MBUTTONDOWN, _WM_XBUTTONDOWN, parse_button  # noqa: E402
from clio.orchestrator import Orchestrator  # noqa: E402


def build(mouse=None):
    return Orchestrator(
        wake_detector=None, turn_detector=None, stt=None, llm=None, speaker=None,
        persona_system_prompt="p", follow_up_window_s=1.0, mouse=mouse,
    )


def main():
    # --- a button name becomes a window message + x-button ---

    assert parse_button("x2") == (_WM_XBUTTONDOWN, 2, "x2")
    assert parse_button("forward") == (_WM_XBUTTONDOWN, 2, "forward")
    assert parse_button("x1") == (_WM_XBUTTONDOWN, 1, "x1")
    assert parse_button("back") == (_WM_XBUTTONDOWN, 1, "back")
    assert parse_button("MIDDLE") == (_WM_MBUTTONDOWN, 0, "middle")
    print("OK  a spare button parses to its message and x-button")

    # --- left and right are refused, so ordinary clicking is never hooked ---

    for bad in ["left", "right", "", "scroll", "wheel"]:
        try:
            parse_button(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} should have been rejected")
    print("OK  left/right and unknown buttons are refused")

    # --- a press feeds the same trigger the hotkey does ---

    orchestrator = build(MouseConfig(enabled=True, button="x2"))
    assert not orchestrator._triggered and not orchestrator._announcement_ready.is_set()
    orchestrator._fire_trigger("mouse")
    assert orchestrator._triggered and orchestrator._trigger_source == "mouse"
    assert orchestrator._announcement_ready.is_set(), "the wake wait breaks on this event"
    print("OK  a press sets the shared trigger and names its source")

    # --- disabled or absent means no hook at all ---

    for cfg in [None, MouseConfig(enabled=False, button="x2")]:
        orch = build(cfg)
        orch._start_triggers()
        assert orch._mouse_trigger is None, f"no trigger expected for {cfg}"
    print("OK  a disabled or missing mouse config installs no hook")

    print("\nAll mouse checks passed.")


if __name__ == "__main__":
    main()

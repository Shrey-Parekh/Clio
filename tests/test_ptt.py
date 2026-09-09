"""Push-to-talk: parse the hold key, and capture from key-down to key-up
instead of waiting for VAD to find the end of speech.

The keyboard hook itself installs a real system hook, so it is verified live.
What is testable is the parser, the press/release flag contract, and that
capture stops the moment the key is released.
Run: python tests/test_ptt.py
"""

import asyncio
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clio.core.config import PushToTalkConfig  # noqa: E402
from clio.input.keyboard import parse_key  # noqa: E402
from clio.orchestrator import Orchestrator  # noqa: E402


def build():
    return Orchestrator(
        wake_detector=None, turn_detector=None, stt=None, llm=None, speaker=None,
        persona_system_prompt="p", follow_up_window_s=1.0,
        push_to_talk=PushToTalkConfig(enabled=True, key="f8"),
    )


async def main():
    # --- the hold key parses to a virtual-key code ---

    assert parse_key("f8") == (0x77, "f8")
    assert parse_key("SPACE")[0] == 0x20
    for bad in ["", "ctrl+a", "enter", "escape"]:
        try:
            parse_key(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} should have been rejected")
    print("OK  a single hold key parses, combos and unknowns are refused")

    # --- press starts a turn, release ends it, autorepeat doesn't re-fire ---

    orchestrator = build()
    orchestrator._ptt_pressed()
    assert orchestrator._ptt_down and orchestrator._triggered
    assert orchestrator._trigger_source == "ptt"

    orchestrator._triggered = False  # the loop consumes it
    orchestrator._ptt_pressed()  # key-down repeats while held
    assert not orchestrator._triggered, "autorepeat must not start a second turn"

    orchestrator._ptt_released()
    assert not orchestrator._ptt_down
    print("OK  press fires once, release clears, autorepeat is ignored")

    # --- capture runs from key-down to key-up, not to end-of-speech ---

    orchestrator._ptt_down = True

    async def frames():
        for i in range(100):
            if i == 3:
                orchestrator._ptt_down = False  # "key released" after 4 frames
            yield np.ones(512, dtype=np.float32)

    orchestrator._frames = frames()
    audio = await orchestrator._ptt_capture()
    assert audio.size == 4 * 512, f"captured {audio.size} samples, expected 4 frames"
    print("OK  capture ends on release, not on silence")

    print("\nAll push-to-talk checks passed.")


if __name__ == "__main__":
    asyncio.run(main())

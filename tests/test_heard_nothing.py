"""Waking and then hearing nothing (found live, 2026-10-01).

His USB mic was at 27% in Windows. Audio kept arriving, so the HUD said MIC
LIVE - but his voice peaked around 0.01, under what the speech detector needs.
After the hotkey she waited for speech with no limit: no reply, no error, and
a log that stopped at "Woke". Amplified twenty times, the same audio scored
0.81 on the wake word.

So: a wake that hears nothing ends, she says why, and when the level was very
low she says it is the microphone's volume - and the HUD stops claiming the
mic is live.

Run: python tests/test_heard_nothing.py
"""

import asyncio
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import clio.orchestrator as orchestrator_module  # noqa: E402
from clio.orchestrator import Orchestrator  # noqa: E402
from clio.speech.audio_input import FRAME_SAMPLES, TurnDetector  # noqa: E402


class DeafTurnDetector:
    """A detector that never finds speech, like a mic too quiet to trip it.
    It honours `stop`, as the real one does, so nobody has to cancel it."""

    def __init__(self, peak):
        self.last_peak = peak

    async def wait_for_onset(self, frames, stop=None):
        while stop is None or not stop.is_set():
            await asyncio.sleep(0.01)
        return None

    async def capture_until_silence(self, frames, onset_frames):
        raise AssertionError("nothing was heard, so there is no turn to capture")


class FakeSpeaker:
    def __init__(self):
        self.said = []

    async def speak(self, text, frames, listen_after_s=0.0):
        self.said.append(text)


class FakeVAD:
    def reset(self):
        pass

    def process(self, frame):
        return 0.0


def build(peak):
    speaker = FakeSpeaker()
    o = Orchestrator(
        wake_detector=None, turn_detector=DeafTurnDetector(peak), stt=None, llm=None,
        speaker=speaker, persona_system_prompt="p", follow_up_window_s=1.0)
    shown = []

    async def emit(name, payload=None):
        shown.append((name, payload or {}))
    o._emit = emit
    o._frames = None
    return o, speaker, shown


async def main():
    orchestrator_module._FIRST_TURN_S = 0.2

    # --- a very quiet mic: she says it is the volume, and the HUD says quiet ---

    o, speaker, shown = build(peak=0.009)
    await asyncio.wait_for(o._conversation_loop(), timeout=3)   # used to wait forever
    assert len(speaker.said) == 1 and "microphone" in speaker.said[0].lower(), speaker.said
    assert "volume" in speaker.said[0].lower(), speaker.said
    assert ("clio.mic", {"quiet": True}) in shown, shown
    assert any(n == "clio.transcript" and "microphone" in p["text"].lower() for n, p in shown), \
        "said in the window too - he may not be able to hear her either"
    print("OK  woke, heard nothing from a faint mic: says it's the volume, HUD shows quiet")

    # --- a healthy level and nobody spoke: no blame on the mic ---

    o, speaker, shown = build(peak=0.4)
    await asyncio.wait_for(o._conversation_loop(), timeout=3)
    assert len(speaker.said) == 1 and "microphone" not in speaker.said[0].lower(), speaker.said
    assert ("clio.mic", {"quiet": True}) not in shown, shown
    print("OK  a normal level with nobody speaking is just 'didn't catch anything'")

    # --- the real detector reports how loud what it listened to was ---

    async def frames(level, count):
        for _ in range(count):
            yield np.full(FRAME_SAMPLES, level, dtype=np.float32)

    detector = TurnDetector(FakeVAD(), threshold=0.5, min_speech_ms=250, end_silence_ms=700)
    assert await detector.wait_for_onset(frames(0.009, 20)) is None
    assert abs(detector.last_peak - 0.009) < 1e-6, detector.last_peak
    assert await detector.wait_for_onset(frames(0.3, 5)) is None
    assert abs(detector.last_peak - 0.3) < 1e-6, "measured per listen, not remembered"
    print("OK  the turn detector reports the loudest thing it heard")

    print("\nAll heard-nothing checks passed.")


if __name__ == "__main__":
    asyncio.run(main())

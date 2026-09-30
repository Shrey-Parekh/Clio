"""Mic capture heals a stalled stream by reopening it, and leaves a healthy one
alone. Run: python tests/test_audio_capture.py
"""

import asyncio
import sys
import threading
import time
import types
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clio.speech import audio_input  # noqa: E402
from clio.speech.audio_input import FRAME_SAMPLES, AudioCapture  # noqa: E402


class FakeStream:
    """Stands in for sounddevice.InputStream. The first stream sends
    `first_blocks` blocks and then goes silent, like the stalled USB mic; any
    later stream keeps sending. Every block carries its stream's number."""

    opened: list = []
    first_blocks = 3

    def __init__(self, *, callback, **_settings):
        self.callback = callback
        self.number = len(FakeStream.opened)
        self.closed = False
        FakeStream.opened.append(self)

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        blocks = FakeStream.first_blocks if self.number == 0 else 100_000
        for _ in range(blocks):
            if self.closed:
                return
            self.callback(np.full((FRAME_SAMPLES, 1), self.number, dtype=np.float32), FRAME_SAMPLES, None, None)
            time.sleep(0.005)

    def close(self):
        self.closed = True


async def read(frames, n):
    return [await frames.__anext__() for _ in range(n)]


async def main():
    sys.modules["sounddevice"] = types.SimpleNamespace(InputStream=FakeStream)
    audio_input._STALL_S = 0.2

    # Goes silent after 3 blocks: reopened, and reading carries on from the new stream.
    frames = AudioCapture().frames()
    got = await read(frames, 8)
    await asyncio.sleep(0.05)  # close runs off the loop
    assert len(FakeStream.opened) == 2, len(FakeStream.opened)
    assert FakeStream.opened[0].closed, "the dead stream must be closed"
    assert [int(f[0]) for f in got] == [0, 0, 0, 1, 1, 1, 1, 1], [int(f[0]) for f in got]
    assert all(f.shape == (FRAME_SAMPLES,) for f in got)
    await frames.aclose()
    await asyncio.sleep(0.05)
    assert FakeStream.opened[1].closed, "closing the reader closes the stream"
    print("OK  stalled stream reopened, frames keep coming, dead stream closed")

    # A healthy stream is never reopened, however long it is read.
    FakeStream.opened.clear()
    FakeStream.first_blocks = 100_000
    frames = AudioCapture().frames()
    await read(frames, 120)  # well past the stall window
    assert len(FakeStream.opened) == 1, len(FakeStream.opened)
    await frames.aclose()
    print("OK  a stream that keeps sending is left alone")

    # Found live, 2026-09-30: after a boot the Windows Audio service stopped
    # answering and opening the mic never returned - on the event loop, so the
    # wake word, the hotkey and the chat window all froze, silently. Opening
    # now happens off the loop: the loop keeps running, he is told once, and a
    # later attempt that works carries on as if nothing happened.
    hang = threading.Event()

    class StuckThenFine(FakeStream):
        attempts = 0

        def __init__(self, **settings):
            StuckThenFine.attempts += 1
            if StuckThenFine.attempts <= 2:
                hang.wait()                     # what Windows did
            super().__init__(**settings)

    class Bus:
        told: list = []

        async def publish(self, name, payload, source=""):
            Bus.told.append(payload["text"])

    sys.modules["sounddevice"] = types.SimpleNamespace(InputStream=StuckThenFine)
    audio_input._OPEN_S, audio_input._RETRY_S = 0.2, 0.05
    FakeStream.opened.clear()
    frames = AudioCapture(bus=Bus()).frames()
    reading = asyncio.ensure_future(read(frames, 3))
    ticks = 0
    while not reading.done() and ticks < 100:
        await asyncio.sleep(0.01)               # the loop is free while the mic hangs
        ticks += 1
    assert reading.done(), "the third attempt opens, and frames flow"
    assert ticks > 20, "the loop kept running through two stuck opens"
    assert StuckThenFine.attempts == 3, StuckThenFine.attempts
    assert Bus.told[0].startswith("My microphone isn't answering"), Bus.told
    assert Bus.told.count(Bus.told[0]) == 1, "told once, not every retry"
    assert Bus.told[-1] == "My microphone is working again.", Bus.told
    await frames.aclose()
    hang.set()                                  # Windows finally lets go of the stuck two
    await asyncio.sleep(0.05)
    assert all(s.closed for s in FakeStream.opened if s is not FakeStream.opened[0]), \
        "an open that returns too late is closed, not left holding the mic"
    print("OK  a mic that won't open no longer freezes her; she says so and keeps trying")

    print("\nAll audio capture checks passed.")


if __name__ == "__main__":
    asyncio.run(main())

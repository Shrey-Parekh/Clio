"""Barge-in: speaking over Clio stops her and starts listening.

Races TTS playback against the mic: the instant speech onset is detected,
speech is cancelled and the interrupting turn is captured, using the same
VAD onset logic TurnDetector already uses for normal turns - no separate
detection path to keep in sync.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import numpy as np

from clio.core.logging import get_logger
from clio.speech.audio_input import TurnDetector
from clio.speech.tts import SpeechEngine

log = get_logger("clio.speech.barge_in")


class BargeInSpeaker:
    def __init__(self, engine: SpeechEngine, turn_detector: TurnDetector):
        self._engine = engine
        self._turn_detector = turn_detector

    async def speak(
        self, text: str, frames: AsyncIterator[np.ndarray], listen_after_s: float = 0.0
    ) -> np.ndarray | None:
        """Speak `text` while listening on `frames` for the user to start
        talking over it. If speech finishes before any onset arrives, keeps
        listening on the same stream for up to `listen_after_s` more before
        giving up - a single onset watch spans both phases so a "no
        interruption yet" outcome never closes `frames` out from under a
        caller that wants to keep listening (conversation mode's case).
        Returns the captured turn if the user spoke - whether mid-response or
        within the extra window - or None if nothing was said.
        """
        speak_task = asyncio.ensure_future(self._engine.speak(text))
        onset_task = asyncio.ensure_future(self._turn_detector.wait_for_onset(frames))

        done, _pending = await asyncio.wait({speak_task, onset_task}, return_when=asyncio.FIRST_COMPLETED)

        if onset_task not in done:
            try:
                onset_frames = await asyncio.wait_for(onset_task, timeout=listen_after_s)
            except asyncio.TimeoutError:
                return None
        else:
            onset_frames = onset_task.result()

        if not speak_task.done():
            log.info("Barge-in: speech interrupted")
            self._engine.cancel()
            await speak_task

        if onset_frames is None:
            return None

        return await self._turn_detector.capture_until_silence(frames, onset_frames)

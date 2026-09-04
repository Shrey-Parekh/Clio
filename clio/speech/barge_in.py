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

    async def speak(self, text: str, frames: AsyncIterator[np.ndarray]) -> np.ndarray | None:
        """Speak `text` while listening on `frames` for the user to start
        talking over it. Returns the captured interrupting turn if barged in
        on, or None if the speech finished without interruption.
        """
        speak_task = asyncio.ensure_future(self._engine.speak(text))
        onset_task = asyncio.ensure_future(self._turn_detector.wait_for_onset(frames))

        done, _pending = await asyncio.wait({speak_task, onset_task}, return_when=asyncio.FIRST_COMPLETED)

        if onset_task not in done:
            onset_task.cancel()
            try:
                await onset_task
            except asyncio.CancelledError:
                pass
            return None

        onset_frames = onset_task.result()

        if not speak_task.done():
            log.info("Barge-in: speech interrupted")
            self._engine.cancel()
            await speak_task

        if onset_frames is None:
            return None

        return await self._turn_detector.capture_until_silence(frames, onset_frames)

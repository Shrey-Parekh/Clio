"""Barge-in: speaking over Clio stops her and starts listening.

Races TTS playback against the mic: the instant speech onset is detected,
speech is cancelled and the interrupting turn is captured, using the same
VAD onset logic TurnDetector already uses for normal turns - no separate
detection path to keep in sync.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

import numpy as np

from clio.core.logging import get_logger
from clio.speech.audio_input import TurnDetector
from clio.speech.tts import SpeechEngine

log = get_logger("clio.speech.barge_in")


@dataclass(frozen=True)
class SpeechOutcome:
    """What happened when Clio spoke. `spoken_text` is what the user actually
    heard, which differs from the input once barge-in cuts speech off — memory
    records the heard version."""

    spoken_text: str
    interrupted: bool
    next_turn: np.ndarray | None


class BargeInSpeaker:
    def __init__(self, engine: SpeechEngine, turn_detector: TurnDetector):
        self._engine = engine
        self._turn_detector = turn_detector

    @property
    def engine(self) -> SpeechEngine:
        """Exposed so the orchestrator can pre-warm the model it wraps."""
        return self._engine

    async def speak(
        self, text: str, frames: AsyncIterator[np.ndarray], listen_after_s: float = 0.0
    ) -> SpeechOutcome:
        """Speak `text` while watching `frames` for the user talking over it. If
        speech finishes first, keep listening up to `listen_after_s` more. One
        onset watch spans both phases, so the returned SpeechOutcome (what was
        spoken, whether it was cut off, and any captured turn) never closes
        `frames` out from under a caller still listening."""
        stop_listening = asyncio.Event()
        speak_task = asyncio.ensure_future(self._engine.speak(text))
        onset_task = asyncio.ensure_future(self._turn_detector.wait_for_onset(frames, stop=stop_listening))

        done, _pending = await asyncio.wait({speak_task, onset_task}, return_when=asyncio.FIRST_COMPLETED)

        if onset_task not in done:
            # Speech finished, nothing said yet: wait listen_after_s more, then stop
            # by signal. Never cancel — cancelling mid-`async for` closes `frames`
            # for every later reader. shield() stops wait_for's timeout cancelling it.
            try:
                onset_frames = await asyncio.wait_for(asyncio.shield(onset_task), timeout=listen_after_s)
            except asyncio.TimeoutError:
                stop_listening.set()
                onset_frames = await onset_task
        else:
            onset_frames = onset_task.result()

        interrupted = not speak_task.done()
        if interrupted:
            log.info("Barge-in: speech interrupted")
            self._engine.cancel()

        spoken_text = await speak_task

        if onset_frames is None:
            return SpeechOutcome(spoken_text=spoken_text, interrupted=interrupted, next_turn=None)

        next_turn = await self._turn_detector.capture_until_silence(frames, onset_frames)
        return SpeechOutcome(spoken_text=spoken_text, interrupted=interrupted, next_turn=next_turn)

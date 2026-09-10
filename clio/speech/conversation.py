"""Conversation mode: keep listening for a bounded window after answering, so a
follow-up doesn't need the wake word again."""

from __future__ import annotations

from collections.abc import AsyncIterator

import numpy as np

from clio.core.logging import get_logger
from clio.speech.barge_in import BargeInSpeaker, SpeechOutcome

log = get_logger("clio.speech.conversation")


class ConversationSession:
    def __init__(self, speaker: BargeInSpeaker, follow_up_window_s: float = 6.0):
        self._speaker = speaker
        self._follow_up_window_s = follow_up_window_s

    async def respond(self, text: str | AsyncIterator[str], frames: AsyncIterator[np.ndarray]) -> SpeechOutcome:
        """Speak `text` (barge-in throughout), then listen for a follow-up window.
        `outcome.next_turn` carries the next turn's audio, or is None if the window
        elapsed silently and the wake word is needed again."""
        outcome = await self._speaker.speak(text, frames, listen_after_s=self._follow_up_window_s)
        log.info("Conversation mode: %s",
                 "no follow-up" if outcome.next_turn is None else "continuing")
        return outcome

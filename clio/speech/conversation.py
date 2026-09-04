"""Conversation mode: after Clio answers, she keeps listening for a bounded
window so a follow-up doesn't need the wake word again. A thin wrapper over
BargeInSpeaker's listen_after_s - a follow-up said mid-response is still
barge-in; this names and configures the extra listening window that applies
once a response finishes cleanly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import numpy as np

from clio.core.logging import get_logger
from clio.speech.barge_in import BargeInSpeaker

log = get_logger("clio.speech.conversation")


class ConversationSession:
    def __init__(self, speaker: BargeInSpeaker, follow_up_window_s: float = 6.0):
        self._speaker = speaker
        self._follow_up_window_s = follow_up_window_s

    async def respond(self, text: str, frames: AsyncIterator[np.ndarray]) -> np.ndarray | None:
        """Speak `text` (with barge-in throughout), then keep listening for a
        bounded follow-up window. Returns the next turn's audio if the user
        said anything - whether by barging in mid-response or following up
        within the window - or None if the window elapsed with nothing said,
        meaning conversation mode ends and the wake word is needed again.
        """
        turn = await self._speaker.speak(text, frames, listen_after_s=self._follow_up_window_s)
        if turn is None:
            log.info("Conversation mode: no follow-up, wake word required again")
        else:
            log.info("Conversation mode: continuing without wake word")
        return turn

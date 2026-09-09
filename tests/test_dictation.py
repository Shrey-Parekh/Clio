"""Dictation: a trigger captures one turn and types it into the focused window,
with no model call and no spoken reply.

type_text is patched out so the test doesn't fire real keystrokes into whatever
has focus; the SendInput path itself is verified live.
Run: python tests/test_dictation.py
"""

import asyncio
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import clio.orchestrator as orchestrator_module  # noqa: E402
from clio.core.config import DictationConfig  # noqa: E402
from clio.llm.memory import ConversationMemory  # noqa: E402
from clio.orchestrator import Orchestrator  # noqa: E402


class FakeLLM:
    def __init__(self):
        self.calls = 0

    async def complete(self, messages, tier="default"):
        self.calls += 1
        return "should never be called"


class FakeSTT:
    def __init__(self, text):
        self._text = text

    async def transcribe(self, audio, sample_rate=16000):
        return self._text


class FakeTurnDetector:
    """Hands back one turn's audio, then silence."""

    def __init__(self):
        self._given = False

    async def listen_for_turn(self, frames):
        if self._given:
            return np.array([], dtype=np.float32)
        self._given = True
        return np.ones(1600, dtype=np.float32)


async def main():
    typed = []
    orchestrator_module.type_text = lambda text: typed.append(text) or True

    llm = FakeLLM()
    orchestrator = Orchestrator(
        wake_detector=None, turn_detector=FakeTurnDetector(), stt=FakeSTT("print hello world"),
        llm=llm, speaker=None, persona_system_prompt="p", follow_up_window_s=1.0,
        dictation=DictationConfig(enabled=True, combo="ctrl+alt+d"),
    )
    orchestrator._memory = ConversationMemory(provider=llm, system_prompt="p")

    await orchestrator._dictate_once()
    assert typed == ["print hello world"], typed
    assert llm.calls == 0, "dictation must never reach the model"
    print(f"OK  one turn captured and typed verbatim: {typed[0]!r}")

    # Nothing said -> nothing typed.
    typed.clear()
    await orchestrator._dictate_once()
    assert typed == [], "a silent turn types nothing"
    print("OK  a silent turn types nothing")

    # A dictation trigger names itself so the loop routes to dictation, not a turn.
    orchestrator._fire_trigger("dictation")
    assert orchestrator._triggered and orchestrator._trigger_source == "dictation"
    print("OK  a dictation trigger is tagged for the dictation path")

    print("\nAll dictation checks passed.")


if __name__ == "__main__":
    asyncio.run(main())

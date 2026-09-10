"""Streaming speech: each sentence is spoken as soon as the model has written
it, not after the whole reply. Run: python tests/test_streaming.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clio.llm.memory import ConversationMemory  # noqa: E402
from clio.llm.provider import FallbackLLMProvider, LLMError  # noqa: E402
from clio.orchestrator import Orchestrator  # noqa: E402
from clio.speech.tts import KokoroSpeechEngine  # noqa: E402


def fake_engine(on_play=None):
    """The real sentence and cancel logic with synthesis and playback faked:
    a sentence's 'audio' is the sentence itself."""
    engine = KokoroSpeechEngine("unused", "unused", "unused")
    engine.played = []
    engine._synthesize = lambda sentence: (sentence, 24000)

    async def play(samples, sample_rate):
        engine.played.append(samples)
        if on_play is not None:
            await on_play(engine, samples)
        return not engine._cancelled.is_set()

    engine._play = play
    return engine


async def stream(*parts):
    for part in parts:
        yield part


class Dead:
    async def stream(self, messages, tier="default"):
        raise LLMError("network down")
        yield  # pragma: no cover


class Local:
    async def stream(self, messages, tier="default"):
        yield "local answer"


def build(llm):
    o = Orchestrator(
        wake_detector=None, turn_detector=None, stt=None, llm=llm, speaker=None,
        persona_system_prompt="p", follow_up_window_s=1.0,
    )
    o._memory = ConversationMemory(provider=llm, system_prompt="p")
    return o


async def collect(reply):
    return "".join([chunk async for chunk in reply])


async def main():
    # Sentences split correctly across chunk boundaries, abbreviations included.
    engine = fake_engine()
    said = await engine.speak(stream("Hello there. How are", " you today? Dr. Smith", " called."))
    assert engine.played == ["Hello there.", "How are you today?", "Dr. Smith called."], engine.played
    assert said == "Hello there. How are you today? Dr. Smith called.", said
    print("OK  sentences split across chunks, 'Dr.' does not end one")

    # The first sentence plays while the model is still writing the second. If
    # speech waited for the whole reply, this would never finish and time out.
    written = asyncio.Event()

    async def slow_model():
        yield "First one. Second"
        await written.wait()
        yield " one."

    async def finish_writing(engine, sentence):
        written.set()

    engine = fake_engine(on_play=finish_writing)
    await asyncio.wait_for(engine.speak(slow_model()), timeout=5)
    assert engine.played == ["First one.", "Second one."], engine.played
    print("OK  first sentence spoken before the model finished")

    # Cut off (barge-in): playback stops and the model stops being read.
    pulled = []

    async def long_model():
        for i in range(50):
            pulled.append(i)
            yield f"Sentence number {i}. "
            await asyncio.sleep(0.01)

    async def interrupt(engine, sentence):
        engine._cancelled.set()

    engine = fake_engine(on_play=interrupt)
    said = await engine.speak(long_model())
    read_at_cancel = len(pulled)
    await asyncio.sleep(0.1)
    assert engine.played == ["Sentence number 0."] and said == "", (engine.played, said)
    assert len(pulled) == read_at_cancel < 50, (read_at_cancel, len(pulled))
    print("OK  interrupting stops playback and stops reading the model")

    # A finished string still works the way it always did.
    engine = fake_engine()
    assert await engine.speak("One. Two.") == "One. Two." and engine.played == ["One.", "Two."]
    print("OK  plain text unchanged")

    # Orchestrator: an intent is still plain text, an LLM reply is a stream that
    # carries the downgrade notice, and a failure is spoken rather than raised.
    o = build(FallbackLLMProvider(Dead(), Local()))
    reply, used = await o._stream_utterance("set a timer for two minutes")
    assert reply == "Okay, timer set for 2 minutes." and used is False, reply
    reply, used = await o._stream_utterance("what is the capital of France")
    text = await collect(reply)
    assert used and text.startswith("Heads up") and text.endswith("local answer"), text
    print("OK  intents stay text, LLM replies stream with the downgrade notice")

    o = build(FallbackLLMProvider(Dead(), Dead()))
    reply, _ = await o._stream_utterance("anything")
    assert await collect(reply), "the failure should be spoken"
    print("OK  both models down: the failure is spoken, not raised")

    print("\nAll streaming checks passed.")


if __name__ == "__main__":
    asyncio.run(main())

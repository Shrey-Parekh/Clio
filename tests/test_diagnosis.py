"""Self-diagnosis and feedback learning: she explains the failure she actually
had, and a correction becomes a standing rule.
Run: python tests/test_diagnosis.py
"""

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clio.capabilities.correction import parse_correction  # noqa: E402
from clio.capabilities.diagnose import explain_failure, is_diagnosis_query  # noqa: E402
from clio.core.errors import report_error  # noqa: E402
from clio.core.events import EventBus  # noqa: E402
from clio.llm.memory import ConversationMemory  # noqa: E402
from clio.llm.provider import LLMError  # noqa: E402
from clio.memory.store import MemoryStore  # noqa: E402
from clio.orchestrator import Orchestrator  # noqa: E402


class FakeLLM:
    async def complete(self, messages, tier="default"):
        return "llm reply"


def build(root=None):
    llm = FakeLLM()
    bus = EventBus()
    o = Orchestrator(
        wake_detector=None, turn_detector=None, stt=None, llm=llm, speaker=None,
        persona_system_prompt="p", follow_up_window_s=1.0, bus=bus,
        store=MemoryStore(root) if root else None,
        recent_turns_on_start=0, recall_hits=0, consolidate=False, prewarm=False,
    )
    o._memory = ConversationMemory(provider=llm, system_prompt="p")
    return o, bus


async def main():
    # --- what actually went wrong ---

    o, bus = build()
    reply, used = await o._handle_utterance("what went wrong")
    assert "Nothing has failed" in reply and used is False, reply
    print("OK  nothing failed yet says so, with no LLM call")

    await report_error(bus, LLMError("groq: 503 upstream unavailable"), context="LLM response")
    reply, used = await o._handle_utterance("what went wrong")
    assert used is False
    for expected in ("LLM response", "a language model problem", "503 upstream unavailable", "worth another try"):
        assert expected in reply, (expected, reply)
    print(f"OK  names the stage, category, real detail and outlook: {reply!r}")

    # A failure raised anywhere - not just in the orchestrator - is the one she
    # reports, because every path publishes through the same bus.
    await report_error(bus, OSError("device 3 is in use"), context="TTS synthesis")
    reply, _ = await o._handle_utterance("what happened")
    assert "TTS synthesis" in reply and "device 3 is in use" in reply, reply
    assert "fail the same way" in reply, "an OSError is not retryable"
    print("OK  reports the most recent failure from any source, retry advice included")

    assert not is_diagnosis_query("what happened at work today"), "must not swallow conversation"
    assert is_diagnosis_query("Why did that fail?")
    assert explain_failure(None).startswith("Nothing has failed")
    print("OK  diagnosis matcher stays narrow")

    # --- corrections become standing rules ---

    for text, want in [
        ("From now on, call me boss", "From now on, call me boss"),
        ("no I said the second one", "no I said the second one"),
        ("stop calling me buddy every time", "stop calling me buddy every time"),
        ("that's not what I asked for", "that's not what I asked for"),
        ("remember that I use Windows", "remember that I use Windows"),
        ("no", None),
        ("next time", None),
        ("what happened next in the story", None),
        ("I have no idea what you mean", None),
    ]:
        assert parse_correction(text) == want, (text, parse_correction(text))
    print("OK  corrections detected verbatim, ordinary speech left alone")

    with tempfile.TemporaryDirectory() as tmp:
        o, _ = build(root=tmp)
        await o._handle_utterance("From now on, call me boss")
        await o._handle_utterance("From now on, call me boss")
        await o._handle_utterance("what is the capital of France")

        facts = o._store.facts()
        assert facts == ["From now on, call me boss"], facts
        assert "call me boss" in o._store.recall("what should you call me"), "must reach later sessions"
        o._store.close()
    print("OK  the rule is stored once, in his words, and recalled later")

    print("\nAll diagnosis and correction checks passed.")


if __name__ == "__main__":
    asyncio.run(main())

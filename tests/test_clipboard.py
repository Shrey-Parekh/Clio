"""Clipboard: read locally, transform through the model, and always be one
sentence from undoing it.

This one touches the real Windows clipboard, because a mocked clipboard would
not have caught the bug that actually happened - an undeclared argtype
overflowing a 64-bit handle. Whatever was on it is put back at the end.
Run: python tests/test_clipboard.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clio.capabilities.clipboard import (  # noqa: E402
    Clipboard, looks_like_a_secret, parse_clipboard_request, read_text, summarise_for_speech,
    write_text,
)
from clio.llm.memory import ConversationMemory  # noqa: E402
from clio.orchestrator import Orchestrator  # noqa: E402

SLOPPY = "teh quick brown fox jumpd over teh lazy dog"
FIXED = "The quick brown fox jumped over the lazy dog."


class FakeLLM:
    """Stands in for the cloud model. `local` is the machine's own, exactly as
    FallbackLLMProvider exposes it."""

    def __init__(self, with_local=True):
        self.calls = 0
        self.saw = ""
        self.local = FakeLocal() if with_local else None

    async def complete(self, messages, tier="default"):
        self.calls += 1
        self.saw = messages[-1]["content"]
        return FIXED


class FakeLocal:
    def __init__(self):
        self.calls = 0
        self.saw = ""

    async def complete(self, messages, tier="default"):
        self.calls += 1
        self.saw = messages[-1]["content"]
        return "AKIA_REDACTED_BY_LOCAL"


async def main():
    original = read_text()
    try:
        # --- it only claims sentences that name the clipboard ---

        for text, kind in [
            ("what's in my clipboard", "read"),
            ("what did i just copy", "read"),
            ("fix the grammar in what i just copied", "transform"),
            ("translate my clipboard to french", "transform"),
            ("put it back", "restore"),
        ]:
            request = parse_clipboard_request(text)
            assert request is not None and request.kind == kind, (text, request)

        for text in ["make it shorter", "fix the grammar", "tell me a joke",
                     "what time is it", "summarise the roadmap"]:
            assert parse_clipboard_request(text) is None, (text, parse_clipboard_request(text))
        print("OK  only sentences that name the clipboard are claimed")

        # --- the real Windows clipboard, round trip ---

        assert write_text(SLOPPY) and read_text() == SLOPPY
        unicode_text = "line one\nline two - em dash, e, 日本語"
        assert write_text(unicode_text) and read_text() == unicode_text, "unicode must survive"
        print("OK  reads and writes the real clipboard, unicode intact")

        # --- credentials never leave the machine ---

        for secret in ["sk-abc123def456ghi789jkl", "password: hunter2",
                       "-----BEGIN RSA PRIVATE KEY-----", "ghp_aaaabbbbccccddddeeeeffff1111"]:
            assert looks_like_a_secret(secret), secret
        for safe in ["just some normal text about cats", "the secret to good bread is time",
                     SLOPPY]:
            assert not looks_like_a_secret(safe), safe

        clipboard = Clipboard()
        write_text("AKIAIOSFODNN7EXAMPLE")
        text, refusal, sensitive = clipboard.take()
        assert text and not refusal and sensitive, "a key is transformable, but not by the cloud"
        write_text(SLOPPY)
        _, _, sensitive = clipboard.take()
        assert not sensitive, "ordinary prose must not be forced onto the local model"
        print("OK  a key is flagged as local-only rather than refused")

        # --- long clipboards are described, not recited ---

        assert summarise_for_speech("x " * 400).startswith("400 words, starting:")
        assert summarise_for_speech("short one") == "short one"

        # --- transform, replace, undo ---

        llm = FakeLLM()
        orchestrator = Orchestrator(
            wake_detector=None, turn_detector=None, stt=None, llm=llm, speaker=None,
            persona_system_prompt="p", follow_up_window_s=1.0,
        )
        orchestrator._memory = ConversationMemory(provider=llm, system_prompt="p")

        write_text(SLOPPY)
        spoken, used = await orchestrator._handle_utterance("what's in my clipboard")
        assert spoken == SLOPPY and used is False and llm.calls == 0, "reading stays local"

        spoken, used = await orchestrator._handle_utterance(
            "fix the grammar in what i just copied"
        )
        assert llm.calls == 1 and SLOPPY in llm.saw, llm.saw
        assert "fix the grammar" in llm.saw, "the instruction is passed through verbatim"
        assert used is True, "a transform costs a call and must say so"
        assert read_text() == FIXED, "the result has to land on the clipboard"
        assert "on your clipboard" in spoken, spoken
        print(f"OK  transformed and replaced: {read_text()!r}")

        spoken, _ = await orchestrator._handle_utterance("put it back")
        assert read_text() == SLOPPY, "undo has to restore exactly what was replaced"
        assert spoken == "Put it back."
        # Twice is not a second undo - there is nothing left to restore.
        spoken, _ = await orchestrator._handle_utterance("put it back")
        assert "nothing to put back" in spoken, spoken
        print("OK  undo restores exactly what was replaced, once")

        # --- a key is transformed, but never by the cloud model ---

        write_text("AKIAIOSFODNN7EXAMPLE")
        cloud_before = llm.calls
        spoken, _ = await orchestrator._handle_utterance("shorten what i copied")
        assert llm.calls == cloud_before, "a key must never reach the cloud model"
        assert llm.local.calls == 1 and "AKIAIOSFODNN7EXAMPLE" in llm.local.saw
        assert "locally" in spoken, spoken
        print(f"OK  the key went to the local model, and she said so: {spoken[-44:]!r}")

        # With no local model there is nowhere safe to send it, so it stops.
        stranded = FakeLLM(with_local=False)
        alone = Orchestrator(
            wake_detector=None, turn_detector=None, stt=None, llm=stranded, speaker=None,
            persona_system_prompt="p", follow_up_window_s=1.0,
        )
        alone._memory = ConversationMemory(provider=stranded, system_prompt="p")
        spoken, _ = await alone._handle_utterance("shorten what i copied")
        assert stranded.calls == 0 and "no local model" in spoken, spoken
        print("OK  no local model means it stops rather than falling back to the cloud")

        # --- an empty clipboard says so rather than sending nothing ---

        write_text("")
        llm_before = llm.calls
        spoken, _ = await orchestrator._handle_utterance("summarise what i copied")
        assert "nothing on your clipboard" in spoken and llm.calls == llm_before, spoken
        print("OK  an empty clipboard costs no call")

        caps = {c.name: c for c in orchestrator._router.capabilities()}
        assert caps["clipboard"].permission.value == "free" and caps["clipboard"].offline

        print("\nAll clipboard checks passed.")
    finally:
        write_text(original or "")


if __name__ == "__main__":
    asyncio.run(main())

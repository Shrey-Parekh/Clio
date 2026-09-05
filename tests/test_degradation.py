"""Offline behaviour: what still works, and whether it says so.
Run: python tests/test_degradation.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clio.llm.memory import ConversationMemory  # noqa: E402
from clio.llm.provider import FallbackLLMProvider, LLMError  # noqa: E402
from clio.orchestrator import Orchestrator  # noqa: E402


class Dead:
    async def stream(self, messages, tier="default"):
        raise LLMError("network down")
        yield  # pragma: no cover

    async def call_tool(self, messages, tools, tier="fast"):
        raise LLMError("network down")


class Local:
    async def stream(self, messages, tier="default"):
        yield "local answer"

    async def call_tool(self, messages, tools, tier="fast"):
        return None


def build(llm):
    o = Orchestrator(
        wake_detector=None, turn_detector=None, stt=None, llm=llm, speaker=None,
        persona_system_prompt="p", follow_up_window_s=1.0,
    )
    o._memory = ConversationMemory(provider=llm, system_prompt="p")
    return o


async def main():
    # Deterministic intents need no network at all.
    offline = build(FallbackLLMProvider(Dead(), Dead()))
    reply, used_llm = await offline._handle_utterance("set a timer for two minutes")
    assert reply == "Okay, timer set for 2 minutes." and used_llm is False
    print("OK  timers work with both providers dead, no LLM touched")

    reply, _ = await offline._handle_utterance("status")
    assert "timer" in reply and "no network" in reply, reply
    print(f"OK  status answers without network: {reply!r}")

    # Falling back to the local model is announced once, not every turn.
    degraded = build(FallbackLLMProvider(Dead(), Local()))
    first, _ = await degraded._handle_utterance("what is the capital of France")
    assert first.startswith("Heads up"), first
    second, _ = await degraded._handle_utterance("and Germany")
    assert not second.startswith("Heads up"), second
    print("OK  downgrade announced once, then stays quiet")

    # Status reflects the degraded state rather than claiming everything is fine.
    reply, _ = await degraded._handle_utterance("are you online")
    assert "local model" in reply, reply
    print(f"OK  status reports degradation: {reply!r}")

    # Recovery re-arms the notice for the next outage.
    degraded._llm = FallbackLLMProvider(Local(), Dead())
    degraded._memory = ConversationMemory(provider=degraded._llm, system_prompt="p")
    recovered, _ = await degraded._handle_utterance("anything")
    assert not recovered.startswith("Heads up")
    assert degraded._announced_degraded is False
    print("OK  recovery clears the flag, so the next outage is announced again")

    print("\nAll degradation checks passed.")


if __name__ == "__main__":
    asyncio.run(main())

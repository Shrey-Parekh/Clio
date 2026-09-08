"""Referential follow-ups on the deterministic path: "do that again" repeats
the last action, and repeating never skips a confirmation.
Run: python tests/test_referential.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clio.core.permissions import Permission, PermissionPolicy  # noqa: E402
from clio.core.router import IntentRouter  # noqa: E402
from clio.llm.memory import ConversationMemory  # noqa: E402
from clio.orchestrator import Orchestrator  # noqa: E402


class FakeLLM:
    def __init__(self):
        self.calls = 0

    async def complete(self, messages, tier="default"):
        self.calls += 1
        return "llm reply"


def build():
    llm = FakeLLM()
    o = Orchestrator(
        wake_detector=None, turn_detector=None, stt=None, llm=llm, speaker=None,
        persona_system_prompt="p", follow_up_window_s=1.0,
    )
    o._memory = ConversationMemory(provider=llm, system_prompt="p")
    return o, llm


async def main():
    o, llm = build()

    # Nothing done yet: says so rather than guessing.
    reply, used = await o._handle_utterance("do that again")
    assert "haven't asked me" in reply and used is False, reply
    print("OK  repeat with no prior action says so, no LLM call")

    # Repeat re-runs the last deterministic action.
    first, _ = await o._handle_utterance("set a timer for two minutes")
    again, used = await o._handle_utterance("do that again")
    assert again == first == "Okay, timer set for 2 minutes.", (first, again)
    assert used is False and llm.calls == 0
    print(f"OK  repeated the timer with zero LLM calls: {again!r}")

    # Confirm-tier behaviour: declined actions are not remembered, and an
    # accepted one still asks again when repeated.
    ran, asked = [], []

    async def do_it(payload):
        ran.append(payload)
        return "done"

    o2, _ = build()
    o2._policy = PermissionPolicy({"danger": Permission.CONFIRM, "repeat": Permission.FREE})
    o2._router = IntentRouter(o2._policy)
    o2._register_intents()
    o2._router.register("danger", lambda t: "x" if "danger" in t else None, do_it,
                        describe=lambda p: "Delete the thing")

    async def refuse(_prompt):
        return False

    o2._confirm = refuse
    await o2._handle_utterance("danger")
    assert ran == [] and o2._last_plan == []
    reply, _ = await o2._handle_utterance("do that again")
    assert "haven't asked me" in reply, reply
    print("OK  a declined action is not remembered, so 'again' cannot rerun it")

    async def accept(prompt):
        asked.append(prompt)
        return True

    o2._confirm = accept
    await o2._handle_utterance("danger")
    assert ran == ["x"] and len(asked) == 1
    await o2._handle_utterance("do that again")
    assert ran == ["x", "x"], ran
    assert len(asked) == 2, "repeating a confirm-tier action must ask again"
    print("OK  repeating a confirm-tier action asks again, never silently reruns")

    print("\nAll referential checks passed.")


if __name__ == "__main__":
    asyncio.run(main())

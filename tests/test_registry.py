"""The capability registry: what is registered, what tier it got, and what
still works with no network.
Run: python tests/test_registry.py
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
    async def complete(self, messages, tier="default"):
        return "llm reply"


async def noop(_payload):
    return "done"


async def main():
    router = IntentRouter(PermissionPolicy({"known": Permission.FREE}))
    router.register("known", lambda t: True, noop)
    router.register("needs_net", lambda t: True, noop, offline=False)
    router.register("unclassified", lambda t: True, noop)

    caps = router.capabilities()
    assert [c.name for c in caps] == ["known", "needs_net", "unclassified"], "match order"
    assert [c.offline for c in caps] == [True, False, True]
    print("OK  registry lists name, order and the offline claim")

    # A capability cannot grant itself authority: the tier comes from the
    # policy, and one it has never heard of asks before acting.
    assert caps[0].permission is Permission.FREE
    assert caps[2].permission is Permission.CONFIRM, "unclassified must fail safe"
    print("OK  tiers come from the policy, unknown capabilities default to confirm")

    # The offline claim is what "what still works" is built from, so a
    # network-dependent capability must never be listed as working without one.
    llm = FakeLLM()
    o = Orchestrator(
        wake_detector=None, turn_detector=None, stt=None, llm=llm, speaker=None,
        persona_system_prompt="p", follow_up_window_s=1.0,
    )
    o._memory = ConversationMemory(provider=llm, system_prompt="p")
    o._router.register("weather", lambda t: None, noop, offline=False)

    spoken, used = await o._handle_utterance("status")
    assert used is False, "answering a connectivity question must not call the network"
    assert "timer" in spoken and "diagnose" in spoken, spoken
    assert "weather" not in spoken, "a network-dependent capability was claimed offline-ready"
    print(f"OK  status reports only offline-capable ones: {spoken!r}")

    print("\nAll registry checks passed.")


if __name__ == "__main__":
    asyncio.run(main())

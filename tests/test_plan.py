"""Multi-step requests, and the checkpoint that stops them.

The interesting cases are all failures: a chain that keeps going after a step
broke leaves him with no idea which half of what he asked for happened, which
is the fire-and-forget problem 2.7 exists to prevent.
Run: python tests/test_plan.py
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


def build(confirm=True):
    llm = FakeLLM()
    orchestrator = Orchestrator(
        wake_detector=None, turn_detector=None, stt=None, llm=llm, speaker=None,
        persona_system_prompt="p", follow_up_window_s=1.0,
    )
    orchestrator._memory = ConversationMemory(provider=llm, system_prompt="p")

    async def answer(_prompt):
        return confirm

    orchestrator._confirm = answer
    return orchestrator, llm


def wire(orchestrator, ran, explode=()):
    """A router of toy capabilities, so what is under test is the plan logic
    rather than any real matcher."""
    policy = PermissionPolicy({
        "alpha": Permission.FREE, "beta": Permission.FREE,
        "gamma": Permission.FREE, "risky": Permission.CONFIRM,
    })
    router = IntentRouter(policy)

    def make(name):
        async def handler(_payload):
            if name in explode:
                raise RuntimeError(f"{name} blew up")
            ran.append(name)
            return f"did {name}"
        return handler

    for name in ("alpha", "beta", "gamma", "risky"):
        router.register(
            name,
            (lambda n: lambda t: n if n in t else None)(name),
            make(name),
            describe=(lambda n: lambda p: f"the {n} thing")(name),
        )
    orchestrator._router = router
    return router


async def main():
    # --- splitting is only accepted when every part stands alone ---

    orchestrator, _ = build()
    router = wire(orchestrator, [])

    assert [m.intent for m in router.plan("alpha and beta")] == ["alpha", "beta"]
    assert [m.intent for m in router.plan("alpha then beta")] == ["alpha", "beta"]
    assert [m.intent for m in router.plan("alpha, beta and gamma")] == ["alpha", "beta", "gamma"]
    # One half matching is not a chain - it stays a single request, which is
    # what it was before any of this existed.
    assert [m.intent for m in router.plan("alpha and something unknown")] == ["alpha"]
    assert router.plan("nothing at all") == []
    print("OK  a split is only taken when every part matches on its own")

    # Real sentences that must never be split, through the real router.
    real, _ = build()
    assert [m.intent for m in real._router.plan("pick between tea and coffee")] == ["chance"]
    # "one hour and thirty minutes" is one duration, not two commands.
    assert [m.intent for m in real._router.plan(
        "set a timer for one hour and thirty minutes")] == ["timer"]
    print("OK  'tea and coffee' and 'one hour and thirty minutes' stay one request")

    # From the 8 Sept session: saying her name mid-sentence threw the chain
    # away and only the time was answered.
    assert [m.intent for m in real._router.plan(
        "clio, what time is it and can you also flip a coin")] == ["clock", "chance"]
    assert [m.intent for m in real._router.plan(
        "hey, flip a coin and please tell me the time")] == ["chance", "clock"]
    print("OK  a vocative or a courtesy no longer kills the chain")

    # --- everything runs, in the order he said it ---

    ran = []
    orchestrator, llm = build()
    wire(orchestrator, ran)
    spoken, used = await orchestrator._handle_utterance("alpha and beta and gamma")
    assert ran == ["alpha", "beta", "gamma"], ran
    assert spoken == "did alpha did beta did gamma" and used is False
    assert llm.calls == 0, "a chain of deterministic steps must stay deterministic"
    print(f"OK  three steps, in order, no model: {spoken!r}")

    # --- a launch is given time to become a window before the next step ---

    import clio.orchestrator as orchestrator_module
    assert orchestrator_module._SETTLE_AFTER.get("open", 0) >= 1.0, (
        "os.startfile returns before the window exists; without a settle the next "
        "step acts on the desktop as it was, which looks like it never ran"
    )

    # --- a failure stops the chain and says where ---

    ran = []
    orchestrator, _ = build()
    wire(orchestrator, ran, explode=("beta",))
    spoken, _ = await orchestrator._handle_utterance("alpha and beta and gamma")
    assert ran == ["alpha"], "gamma must not run after beta failed"
    assert spoken.startswith("did alpha"), spoken
    assert "Stopped at the beta thing." in spoken, spoken
    assert "one more I haven't done" in spoken, spoken
    print(f"OK  stopped at the failure, and said what is left: {spoken!r}")

    # --- a refusal stops it just as hard ---

    ran = []
    orchestrator, _ = build(confirm=False)
    wire(orchestrator, ran)
    spoken, _ = await orchestrator._handle_utterance("alpha and risky and gamma")
    assert ran == ["alpha"], "a declined step must not let the rest through"
    assert "You said no." in spoken and "one more I haven't done" in spoken, spoken
    print(f"OK  a refusal ends the chain: {spoken!r}")

    # --- a chain is not a way past a confirmation ---

    ran = []
    asked = []
    orchestrator, _ = build()
    wire(orchestrator, ran)

    async def record(prompt):
        asked.append(prompt)
        return True

    orchestrator._confirm = record
    await orchestrator._handle_utterance("alpha and risky")
    assert asked == ["the risky thing. Should I go ahead?"], asked
    assert ran == ["alpha", "risky"]
    print("OK  each step is gated individually, so a chain grants no extra authority")

    # --- the chain is what gets remembered, so "again" repeats all of it ---

    assert [m.intent for m in orchestrator._last_plan] == ["alpha", "risky"]
    ran.clear()
    # The toy router has no repeat intent, so the recorded plan is re-run the
    # way the repeat handler would.
    await orchestrator._run_plan(orchestrator._last_plan)
    assert ran == ["alpha", "risky"], ran
    assert len(asked) == 2, "a confirm-tier step asks again every time, including on a repeat"

    # --- but never the part that was declined ---

    ran = []
    orchestrator, _ = build(confirm=False)
    wire(orchestrator, ran)
    await orchestrator._handle_utterance("alpha and risky")
    assert ran == ["alpha"]
    assert [m.intent for m in orchestrator._last_plan] == ["alpha"], (
        "the declined step must not be remembered as repeatable"
    )
    print("OK  'again' repeats what ran, never what was declined")

    # --- a single step behaves exactly as it did before ---

    ran = []
    orchestrator, _ = build()
    wire(orchestrator, ran, explode=("alpha",))
    try:
        await orchestrator._handle_utterance("alpha")
        raise AssertionError("a lone failing step must still raise, as it always did")
    except RuntimeError:
        pass
    print("OK  one step is unchanged - a lone failure still propagates")

    print("\nAll plan checks passed.")


if __name__ == "__main__":
    asyncio.run(main())

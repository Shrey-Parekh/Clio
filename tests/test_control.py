"""Machine control, and the first capability that has to ask.

Nothing here sleeps or locks anything: `perform` is replaced, so what is
checked is what would have been done, and whether it was asked about first.
Run: python tests/test_control.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clio.capabilities import control as control_mod  # noqa: E402
from clio.capabilities.control import Action, describe_action, parse_control, parse_power  # noqa: E402
from clio.core.permissions import Permission  # noqa: E402
from clio.llm.memory import ConversationMemory  # noqa: E402
from clio.orchestrator import Orchestrator  # noqa: E402


class FakeLLM:
    def __init__(self):
        self.calls = 0

    async def complete(self, messages, tier="default"):
        self.calls += 1
        return "llm reply"


def build(answer_confirm: bool):
    llm = FakeLLM()
    orchestrator = Orchestrator(
        wake_detector=None, turn_detector=None, stt=None, llm=llm, speaker=None,
        persona_system_prompt="p", follow_up_window_s=1.0,
    )
    orchestrator._memory = ConversationMemory(provider=llm, system_prompt="p")
    asked = []

    async def fake_confirm(prompt: str) -> bool:
        asked.append(prompt)
        return answer_confirm

    orchestrator._confirm = fake_confirm
    return orchestrator, llm, asked


async def main():
    done = []
    control_mod.perform = lambda action: done.append(action) or f"did {action.kind}"

    # --- the right action, and conversation left alone ---

    for text, kind in [
        ("set the volume to 20", "volume_set"),
        ("turn it down", "volume_down"),
        ("louder", "volume_up"),
        ("mute", "mute"),
        ("what is the volume", "volume_query"),
        ("lock the screen", "lock"),
        ("minimise everything", "minimise_all"),
        ("extend the displays", "display_extend"),
        ("set the brightness to 50", "brightness_set"),
    ]:
        action = parse_control(text)
        assert action is not None and action.kind == kind, (text, action)

    for text in ["what's the weather", "open clio", "go to the gym", "how much disk space is left",
                 "tell me a joke"]:
        assert parse_control(text) is None, (text, parse_control(text))
    print("OK  control actions matched, conversation and other capabilities untouched")

    # A window that isn't open falls through rather than being refused, so
    # "switch to Spotify" can still be handled as a request to open it.
    assert parse_control("switch to atlantis") is None

    # --- sleep is power, not control, and it is CONFIRM ---

    assert parse_control("go to sleep") is None, "sleep must not land in the free tier"
    assert parse_power("go to sleep").kind == "sleep"
    assert parse_power("set the volume to 20") is None
    assert describe_action(Action("sleep")) == "Putting the machine to sleep"
    print("OK  sleep is a separate intent, so its tier can differ from the rest")

    # --- declining actually stops it ---

    orchestrator, llm, asked = build(answer_confirm=False)
    spoken, used = await orchestrator._handle_utterance("go to sleep")
    assert asked and "Putting the machine to sleep. Should I go ahead?" in asked[0], asked
    assert done == [], "a declined action must not run"
    assert spoken == "Left it alone." and used is False
    print(f"OK  asked first, and no meant no: {asked[0]!r}")

    # --- agreeing runs it, once ---

    orchestrator, llm, asked = build(answer_confirm=True)
    await orchestrator._handle_utterance("go to sleep")
    assert [a.kind for a in done] == ["sleep"], done
    assert llm.calls == 0, "controlling the machine must never cost an LLM call"

    # --- and the free tier does not ask ---

    done.clear()
    orchestrator, llm, asked = build(answer_confirm=False)
    spoken, _ = await orchestrator._handle_utterance("set the volume to 20")
    assert asked == [], "a reversible action must not interrogate him every time"
    assert [a.kind for a in done] == ["volume_set"] and done[0].value == "20"

    caps = {c.name: c for c in orchestrator._router.capabilities()}
    assert caps["power"].permission is Permission.CONFIRM
    assert caps["control"].permission is Permission.FREE
    assert caps["control"].offline and caps["power"].offline
    print("OK  free tier acts immediately, confirm tier asks - both from one module")

    print("\nAll control checks passed.")


if __name__ == "__main__":
    asyncio.run(main())

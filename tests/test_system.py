"""System monitoring: the right topic, honest about what it cannot read, and
never an LLM call.
Run: python tests/test_system.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clio.capabilities.system import describe, parse_system_query, snapshot  # noqa: E402
from clio.llm.memory import ConversationMemory  # noqa: E402
from clio.orchestrator import Orchestrator  # noqa: E402


class FakeLLM:
    def __init__(self):
        self.calls = 0

    async def complete(self, messages, tier="default"):
        self.calls += 1
        return "llm reply"


async def main():
    # --- what he asked about ---

    for text, want in [
        ("what's my cpu usage", "cpu"),
        ("how much ram am i using", "memory"),
        ("how much disk space is left", "disk"),
        ("how's the gpu doing", "gpu"),
        ("what's the gpu temperature", "gpu"),
        ("what's eating my cpu", "hogs"),
        ("how's the machine", "overview"),
        # Must not be swallowed: these belong to other capabilities, or to
        # conversation, and a monitor that answers them is a monitor that
        # hijacks the weather.
        ("how hot is it outside", None),
        ("what's the weather", None),
        ("set a timer for five minutes", None),
        ("what's the capital of france", None),
    ]:
        assert parse_system_query(text) == want, (text, parse_system_query(text))
    print("OK  topics matched, other capabilities' phrasing left alone")

    # --- honest about the sensor Windows does not expose ---

    no_gpu = {
        "cpu": 4.0, "cores": 12, "memory_percent": 33.0,
        "memory_used": 21_474_836_480, "memory_total": 68_719_476_736,
        "disk_free": 467_810_504_704, "disk_total": 1_073_898_057_728,
        "gpu": None, "top": [], "uptime_s": 7200.0,
    }
    said = describe("temperature", no_gpu)
    assert "can't read temperatures" in said and "degrees" not in said, said
    print(f"OK  no sensor means saying so, not a number: {said!r}")

    hot = {**no_gpu, "gpu": {"name": "RTX 4060 Ti", "load": 26.0, "used_mb": 659.0,
                             "total_mb": 8188.0, "temperature": 40.0}}
    said = describe("temperature", hot)
    assert "40 degrees" in said and "can't read the CPU" in said, said
    print("OK  GPU temperature reported, CPU still refused rather than guessed")

    assert describe("hogs", no_gpu).startswith("Nothing much"), "an idle machine says so"

    # --- a real reading of this machine ---

    reading = snapshot(sample_s=0.2)
    assert 0.0 <= reading["cpu"] <= 100.0 and reading["memory_total"] > 0
    assert reading["disk_free"] <= reading["disk_total"]
    for topic in ("cpu", "memory", "disk", "gpu", "temperature", "hogs", "overview"):
        line = describe(topic, reading)
        assert line and line.endswith("."), (topic, line)
    print(f"OK  live: {describe('overview', reading)!r}")
    print(f"OK  live: {describe('hogs', reading)!r}")

    # --- routed without the model, and claimed offline because it is ---

    llm = FakeLLM()
    orchestrator = Orchestrator(
        wake_detector=None, turn_detector=None, stt=None, llm=llm, speaker=None,
        persona_system_prompt="p", follow_up_window_s=1.0,
    )
    orchestrator._memory = ConversationMemory(provider=llm, system_prompt="p")
    spoken, used = await orchestrator._handle_utterance("how much disk space is left")
    assert used is False and llm.calls == 0, "reading a gauge must never cost an LLM call"
    assert "gigs free" in spoken, spoken

    caps = {c.name: c for c in orchestrator._router.capabilities()}
    assert caps["system"].offline, "nothing here touches the network"
    assert caps["system"].permission.value == "free", "read-only, so no confirmation"
    print(f"OK  answered locally, zero LLM calls: {spoken!r}")

    print("\nAll system monitoring checks passed.")


if __name__ == "__main__":
    asyncio.run(main())

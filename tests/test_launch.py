"""Opening things: the right target, nothing launched from a path spoken into
the request, and conversation left alone.
Run: python tests/test_launch.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clio.capabilities import launch as launch_mod  # noqa: E402
from clio.capabilities.launch import installed_apps, open_target, resolve  # noqa: E402
from clio.llm.memory import ConversationMemory  # noqa: E402
from clio.orchestrator import Orchestrator  # noqa: E402

SHORTCUTS = {"clio": "C:/Users/Shrey/Documents/Clio", "the roadmap": "C:/x/ROADMAP.md"}


class FakeLLM:
    def __init__(self):
        self.calls = 0

    async def complete(self, messages, tier="default"):
        self.calls += 1
        return "llm reply"


async def main():
    # --- his own shortcuts win over everything else ---

    target = resolve("open clio", SHORTCUTS)
    assert target.kind == "shortcut" and target.path == SHORTCUTS["clio"], target
    assert resolve("open the roadmap", SHORTCUTS).kind == "shortcut"
    print("OK  configured shortcuts resolve first")

    # --- spoken URLs ---

    assert resolve("open github dot com", SHORTCUTS).path == "https://github.com"
    assert resolve("open news.ycombinator.com").path == "https://news.ycombinator.com"
    print("OK  a spoken domain becomes a URL, not a search for an app")

    # --- folders ---

    downloads = resolve("open my downloads")
    assert downloads.kind == "folder" and downloads.path.endswith("Downloads"), downloads

    # --- never a path out of the utterance ---

    for text in [
        "open c colon backslash windows backslash system32",
        "run C:/Windows/System32/cmd.exe",
        "open ../../etc/passwd",
    ]:
        target = resolve(text)
        assert target is None or target.kind == "unknown", (text, target)
        if target is not None:
            assert target.path == "", "an unknown target must carry nothing to run"
    print("OK  no path spoken into the request is ever resolved to something runnable")

    # --- conversation is not an app ---

    for text in [
        "open up about what is bothering you",
        "open the pod bay doors",
        "start a timer for five minutes",
        "what's the weather",
        "how much disk space is left",
    ]:
        assert resolve(text, SHORTCUTS) is None, (text, resolve(text, SHORTCUTS))
    print("OK  long unmatched phrases fall through to conversation instead of being refused")

    # A short name that genuinely is not installed is worth saying out loud,
    # rather than letting the model improvise having opened it.
    missing = resolve("open frobnicator")
    assert missing.kind == "unknown" and missing.path == ""
    assert "couldn't find" in open_target(missing)
    print("OK  a short unknown name is refused by name")

    # --- the real Start Menu on this machine ---

    apps = installed_apps()
    assert len(apps) > 10, f"only {len(apps)} shortcuts indexed"
    sample = sorted(apps)[0]
    found = resolve(f"open {sample}")
    assert found.kind == "app" and found.path.endswith(".lnk"), found
    print(f"OK  {len(apps)} installed apps indexed, e.g. {sample!r}")

    # --- routed, without launching anything and without the model ---

    launched = []
    launch_mod.os.startfile = lambda path: launched.append(path)

    llm = FakeLLM()
    orchestrator = Orchestrator(
        wake_detector=None, turn_detector=None, stt=None, llm=llm, speaker=None,
        persona_system_prompt="p", follow_up_window_s=1.0, shortcuts=SHORTCUTS,
    )
    orchestrator._memory = ConversationMemory(provider=llm, system_prompt="p")
    spoken, used = await orchestrator._handle_utterance("open clio")
    assert used is False and llm.calls == 0, "opening something must never cost an LLM call"
    assert launched == [SHORTCUTS["clio"]], launched
    assert spoken == "Opening clio.", spoken

    caps = {c.name: c for c in orchestrator._router.capabilities()}
    assert caps["open"].offline, "the Start Menu is not on the internet"
    assert caps["open"].permission.value == "free"
    print(f"OK  launched via the router, zero LLM calls: {spoken!r}")

    print("\nAll launch checks passed.")


if __name__ == "__main__":
    asyncio.run(main())

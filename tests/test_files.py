"""Read-only file access: bounded to configured roots, honest about what it
found, and silent when the sentence was never about a file.

Runs against a temporary tree, not his real folders.
Run: python tests/test_files.py
"""

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clio.capabilities import files as files_mod  # noqa: E402
from clio.capabilities.files import lookup, parse_file_request  # noqa: E402
from clio.llm.memory import ConversationMemory  # noqa: E402
from clio.orchestrator import Orchestrator  # noqa: E402


class FakeLLM:
    def __init__(self):
        self.calls = 0
        self.saw = ""

    async def complete(self, messages, tier="default"):
        self.calls += 1
        self.saw = messages[-1]["content"]
        return "It's a roadmap. Mostly phases."


def build_tree() -> Path:
    root = Path(tempfile.mkdtemp(prefix="clio-files-"))
    (root / "notes").mkdir()
    (root / "roadmap.md").write_text(
        "Phase 3 is capabilities. Kokoro is the voice.", encoding="utf-8"
    )
    (root / "notes" / "shopping.txt").write_text("milk, bread", encoding="utf-8")
    (root / "notes" / "holiday.png").write_bytes(b"\x89PNG not text")
    (root / "secrets.md").write_text("nothing interesting", encoding="utf-8")
    return root


async def main():
    root = build_tree()
    roots = (root,)
    files_mod._index_cache = (0.0, ())  # a fresh tree needs a fresh index

    try:
        # --- a sentence that isn't about a file stays out of here ---

        for text in ["read me a poem", "what's the weather", "open clio", "tell me a story",
                     "how much disk space is left"]:
            assert parse_file_request(text, roots) is None, (text, parse_file_request(text, roots))
        print("OK  an unmatched name falls through to conversation, never 'I can't find a poem'")

        # --- and a path spoken into it never reaches the disk ---

        for text in ["read C:/Windows/system.ini", "read ../../etc/passwd",
                     "read c colon backslash windows"]:
            assert parse_file_request(text, roots) is None, text
        print("OK  no path spoken into the request is resolved")

        # --- finding and reading ---

        request = parse_file_request("read the roadmap", roots)
        assert request.kind == "read" and request.matches[0].name == "roadmap.md"
        spoken, to_summarise = lookup(request, roots)
        assert "Kokoro is the voice" in spoken and to_summarise == ""

        request = parse_file_request("find shopping", roots)
        spoken, _ = lookup(request, roots)
        assert "shopping, in notes" in spoken, spoken
        assert str(root) not in spoken, "a full path read aloud is unusable"
        print(f"OK  found by name, spoken as a folder rather than a path: {spoken!r}")

        # --- content search says how far it actually got ---

        request = parse_file_request("which files mention kokoro", roots)
        spoken, _ = lookup(request, roots)
        assert "roadmap" in spoken, spoken
        request = parse_file_request("which files mention hovercraft", roots)
        spoken, _ = lookup(request, roots)
        assert "Nothing mentioning hovercraft" in spoken and "files I got through" in spoken
        print(f"OK  a miss reports its own scope: {spoken!r}")

        # --- listing ---

        request = parse_file_request("what's in notes", roots)
        spoken, _ = lookup(request, roots)
        assert "shopping" in spoken and "holiday" in spoken, spoken

        # --- something it cannot read out ---

        request = parse_file_request("read holiday", roots)
        spoken, _ = lookup(request, roots)
        assert "isn't something I can read out" in spoken and "png" in spoken, spoken
        print("OK  a binary is found but not read aloud")

        # --- nothing configured means saying so, not searching the drive ---

        request = parse_file_request("read the roadmap", ())
        assert request is not None, "it must still claim the request to explain itself"
        spoken, _ = lookup(request, ())
        assert "don't have anywhere to look" in spoken, spoken
        print("OK  no configured roots means saying so")

        # --- summarising is the one path that costs a call, and admits it ---

        llm = FakeLLM()
        orchestrator = Orchestrator(
            wake_detector=None, turn_detector=None, stt=None, llm=llm, speaker=None,
            persona_system_prompt="p", follow_up_window_s=1.0, file_roots=roots,
        )
        orchestrator._memory = ConversationMemory(provider=llm, system_prompt="p")

        spoken, used = await orchestrator._handle_utterance("find shopping")
        assert used is False and llm.calls == 0, "looking is free"

        spoken, used = await orchestrator._handle_utterance("summarise the roadmap")
        assert llm.calls == 1 and "Kokoro is the voice" in llm.saw, llm.saw
        assert used is True, "an intent that called the model must not report itself as free"
        assert spoken == "It's a roadmap. Mostly phases."
        print("OK  summarising costs one call and is accounted for as one")

        caps = {c.name: c for c in orchestrator._router.capabilities()}
        assert caps["files"].permission.value == "free" and caps["files"].offline
        print("OK  read-only, so free, and local, so offline")

        print("\nAll file checks passed.")
    finally:
        shutil.rmtree(root, ignore_errors=True)
        files_mod._index_cache = (0.0, ())


if __name__ == "__main__":
    asyncio.run(main())

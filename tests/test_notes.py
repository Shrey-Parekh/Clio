"""Quick capture: append to a plain file, resolve "note that down" from the
conversation, and never touch anything else.

Writes into a temporary directory, never his real notes.
Run: python tests/test_notes.py
"""

import asyncio
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clio.capabilities.notes import NoteBook, Request, parse_note_request  # noqa: E402
from clio.llm.memory import ConversationMemory  # noqa: E402
from clio.orchestrator import Orchestrator  # noqa: E402


class FakeLLM:
    def __init__(self):
        self.calls = 0

    async def complete(self, messages, tier="default"):
        self.calls += 1
        return "llm reply"


async def main():
    root = Path(tempfile.mkdtemp(prefix="clio-notes-"))
    path = root / "notes.md"
    try:
        # --- what counts as a note ---

        assert parse_note_request("note down that the bins go out on tuesday") == Request(
            "add", "the bins go out on tuesday"
        )
        assert parse_note_request("make a note to call the dentist") == Request(
            "add", "to call the dentist"
        )
        assert parse_note_request("add this to my notes: buy milk") == Request("add", "buy milk")
        # Bare forms carry no content - the caller fills it from the conversation.
        for bare in ["note that down", "note this down", "write that down", "jot that down"]:
            assert parse_note_request(bare) == Request("add", ""), bare
        # Exactly what Whisper produced in the 8 Sept session. Anchored
        # patterns rejected both, and "read my notes" fell through to the file
        # search, which read a random file out loud.
        assert parse_note_request("I read my notes.") == Request("read")
        assert parse_note_request("Take a note that on Wednesday I have my listening test") == (
            Request("add", "on wednesday i have my listening test")
        )
        assert parse_note_request("what's in my notes") == Request("read")
        assert parse_note_request("read my notes") == Request("read")

        for text in ["what time is it", "remember that i hate coriander",
                     "read the roadmap", "tell me a joke",
                     "i wrote a note to my friend yesterday", "he wrote it down",
                     "denote the value"]:
            assert parse_note_request(text) is None, (text, parse_note_request(text))
        print("OK  notes matched, and corrections left to the correction path")

        # --- the file itself ---

        book = NoteBook(path)
        assert book.recent() == "You haven't got any notes yet."
        assert book.add("buy milk", datetime(2026, 9, 7, 18, 42)) == "Noted: buy milk"
        book.add("the bins go out on tuesday", datetime(2026, 9, 7, 19, 3))
        book.add("call the dentist", datetime(2026, 9, 8, 9, 15))

        written = path.read_text(encoding="utf-8")
        assert written.startswith("# Notes"), written[:40]
        assert written.count("## 2026-09-07") == 1, "one heading per day, not one per note"
        assert written.count("## 2026-09-08") == 1
        assert "- 18:42 buy milk" in written and "- 09:15 call the dentist" in written
        print("OK  plain markdown, one heading per day:\n" + "\n".join(
            "      " + line for line in written.splitlines() if line.strip()
        ))

        # --- read back, newest first, without the times ---

        spoken = book.recent()
        assert spoken.startswith("Your last 3 notes: call the dentist."), spoken
        assert "09:15" not in spoken, "times are for reading, not for listening"

        # --- nothing is lost or rewritten ---

        before = path.read_text(encoding="utf-8")
        NoteBook(path).add("a fourth thing", datetime(2026, 9, 8, 10, 0))
        after = path.read_text(encoding="utf-8")
        assert after.startswith(before), "append-only: existing notes are never rewritten"
        print("OK  append-only, and read back newest first")

        # --- through the router, with no model involved ---

        llm = FakeLLM()
        orchestrator = Orchestrator(
            wake_detector=None, turn_detector=None, stt=None, llm=llm, speaker=None,
            persona_system_prompt="p", follow_up_window_s=1.0,
            notes_path=str(root / "routed.md"),
        )
        orchestrator._memory = ConversationMemory(provider=llm, system_prompt="p")

        spoken, used = await orchestrator._handle_utterance("note down that the wifi is flaky")
        assert used is False and llm.calls == 0, "writing a note must never cost a call"
        assert spoken == "Noted: the wifi is flaky", spoken

        # --- "note that down" means the thing she just said ---

        spoken, _ = await orchestrator._handle_utterance("note that down")
        assert "Note what down" in spoken, "nothing said yet, so there is nothing to note"

        orchestrator._memory.add_assistant("The GPU is at 41 degrees.")
        spoken, _ = await orchestrator._handle_utterance("note that down")
        assert spoken == "Noted: The GPU is at 41 degrees.", spoken
        assert "The GPU is at 41 degrees." in (root / "routed.md").read_text(encoding="utf-8")
        print(f"OK  bare capture takes her last reply: {spoken!r}")

        caps = {c.name: c for c in orchestrator._router.capabilities()}
        assert caps["notes"].permission.value == "free" and caps["notes"].offline
        # "read my notes" must not be read as a request to open a file.
        assert orchestrator._router.match("read my notes").intent == "notes"
        print("OK  free, offline, and it wins 'read my notes' over the file search")

        print("\nAll notes checks passed.")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())

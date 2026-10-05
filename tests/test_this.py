"""What "this" means (8.3).

The window in front is faked: no real screen, accessibility calls or network.
What is checked: only "this"-shaped sentences cost a look; the selection wins,
then the page, then the open file or folder, and otherwise the sentence is left
for the 8.2 screenshot; a selected password goes nowhere; "close this" is the
exact window in front, still CONFIRM; and through the real orchestrator, a
selection is answered from its text and "summarise this" reads the open file.

Run: python tests/test_this.py
"""

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clio.capabilities.this import resolve, wants_context  # noqa: E402
from clio.core import context  # noqa: E402
from clio.core.context import Context, app_name, file_from_title, folder_from_title  # noqa: E402
from clio.core.permissions import Permission  # noqa: E402
from clio.core.screen import Window  # noqa: E402
from clio.llm.memory import ConversationMemory  # noqa: E402
from clio.orchestrator import Orchestrator  # noqa: E402

EDGE = Context(app="Microsoft Edge", exe="msedge.exe", url="https://example.com/post",
               title="Example Domain - Profile 1 - Microsoft Edge")
WORD = Context(app="Word", exe="winword.exe", title="budget.docx - Word", file_name="budget.docx")
FOLDER = Context(app="File Explorer", exe="explorer.exe", folder="invoices",
                 title="invoices - File Explorer")
SELECTED = Context(app="Notepad", exe="notepad.exe", selection="Bonjour tout le monde")


class FakeLLM:
    def __init__(self):
        self.saw = []

    async def complete(self, messages, tier="default"):
        self.saw.append(messages[-1]["content"])
        return "It says hello everyone."

    async def stream(self, messages, tier="default"):
        yield await self.complete(messages, tier)


def win(title, exe, hwnd=7):
    return Window(hwnd, title, exe, (0, 0, 800, 600))


async def main():
    for said in ("summarise this", "Summarise this page.", "what's this page about", "look this up",
                 "translate this", "can you explain this", "move this to Desktop", "where is this saved",
                 "delete this file", "save this link", "what does this mean", "read this file"):
        assert wants_context(said), said
    for said in ("remind me this evening", "what's the weather this week", "explain that",
                 "is this a good idea", "this is great", "close this"):
        assert not wants_context(said), said
    print("OK  only 'this'-shaped requests cost a look; 'this evening' never does")

    assert app_name(win("notes.txt - Notepad", "notepad.exe")) == "Notepad"
    assert app_name(win("Example - Microsoft​ Edge", "msedge.exe")) == "Microsoft Edge"
    assert app_name(win("Steam", "steamwebhelper.exe")) == "Steamwebhelper"
    assert file_from_title(win("● screen.py - Clio - Visual Studio Code", "Code.exe")) == "screen.py"
    assert file_from_title(win("*notes.txt - Notepad", "notepad.exe")) == "notes.txt"
    assert file_from_title(win("Document1 - Word", "WINWORD.EXE")) == "", "unsaved: no file yet"
    assert file_from_title(win("Example Domain - Microsoft Edge", "msedge.exe")) == ""
    assert folder_from_title(win("invoices - File Explorer", "explorer.exe")) == "invoices"
    print("OK  app, file and folder names come out of real window titles")

    r = resolve("translate this", SELECTED)
    assert r.kind == "ask" and "Bonjour tout le monde" in r.text and "Notepad" in r.text
    assert resolve("look this up", SELECTED).text == "search for Bonjour tout le monde"
    both = Context(**{**EDGE.__dict__, "selection": "quantum tunnelling"})
    assert resolve("summarise this", both).kind == "ask", "the selection beats the page"
    secret = Context(selection="sk-proj-AbC123dEf456GhI789jKl012MnO345pQr678")
    assert resolve("look this up", secret).kind == "refuse"
    print("OK  the selection comes first, and a selected key is never sent")

    assert resolve("summarise this page", EDGE).text == "summarise https://example.com/post"
    assert resolve("what's this page about", EDGE).text == "summarise https://example.com/post"
    assert resolve("save this link", EDGE).text == "make a note Example Domain https://example.com/post"
    assert resolve("read this", EDGE) is None, "'read this' alone is what he sees: the screenshot"
    print("OK  a browser page is read by its address; 'save this link' becomes a note")

    assert resolve("summarise this", WORD).text == "summarise budget.docx"
    assert resolve("move this to Desktop", WORD).text == "move budget.docx to desktop"
    assert resolve("where is this saved", WORD).text == "where is budget.docx saved"
    assert resolve("delete this file", WORD).text == "delete budget.docx"
    assert resolve("read this", WORD) is None and resolve("read this file", WORD).text == "read budget.docx"
    assert resolve("what's in this folder", FOLDER).text == "what's in invoices"
    assert resolve("summarise this", Context()) is None, "nothing known: left as it was"
    print("OK  an open file or folder is named, for the file features to find")

    # --- through the real orchestrator ---

    base = Path(tempfile.mkdtemp())
    real_snapshot, real_front = context.snapshot, context.front_window
    try:
        (base / "docs").mkdir()
        (base / "docs" / "budget.txt").write_text("Rent is 900 a month. Food is 300.", encoding="utf-8")
        llm = FakeLLM()
        o = Orchestrator(
            wake_detector=None, turn_detector=None, stt=None, llm=llm, speaker=None,
            persona_system_prompt="p", follow_up_window_s=1.0, file_roots=(base / "docs",),
            memory_root=str(base / "m"))
        o._memory = ConversationMemory(provider=llm, system_prompt="p")

        context.snapshot = lambda: SELECTED
        spoken, used = await o._handle_utterance("what does this mean")
        assert spoken == "It says hello everyone." and used, spoken
        assert "Bonjour tout le monde" in llm.saw[-1], "answered from the selection, not a screenshot"

        context.snapshot = lambda: Context(app="Notepad", exe="notepad.exe", title="budget.txt - Notepad",
                                           file_name="budget.txt")
        spoken, used = await o._handle_utterance("summarise this")
        assert "Rent is 900" in llm.saw[-1], llm.saw[-1]
        print("OK  through the orchestrator: a selection is answered, the open file is summarised")

        looked = []
        context.snapshot = lambda: looked.append(1) or Context()
        await o._route("set a timer for 5 minutes")
        assert not looked, "an ordinary request never looks at the screen"

        notepad = Window(4242, "budget.txt - Notepad", "notepad.exe", (0, 0, 800, 600))
        context.front_window = lambda: notepad
        caps = {c.name: c for c in o._router.capabilities()}
        matched = o._router.match("close this")
        assert matched.intent == "close" and caps["close"].permission is Permission.CONFIRM
        assert matched._payload.handle == 4242 and matched._payload.value == "Notepad"
        assert "Notepad" in matched.description, matched.description
        assert o._router.match("minimise this window")._payload.handle == 4242
        context.front_window = lambda: None
        nothing = o._router.match("close this")
        assert nothing is None or nothing.intent != "close", "no window in front: nothing to close"
        print("OK  'close this' is the exact window in front, read back, still CONFIRM")

        print("\nAll 'this' checks passed.")
    finally:
        context.snapshot, context.front_window = real_snapshot, real_front
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())

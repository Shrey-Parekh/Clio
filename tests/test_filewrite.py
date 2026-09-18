"""Changing files (7.4) - create, copy, rename, move, bin, and undo.

Real files in a temp folder, with a fake Recycle Bin so a test run never fills
his real one. The live check does one genuine send-and-restore through Windows.

Most of this file is about refusal. An operation that works is the easy part; the
parts that matter are that nothing outside the roots is touched, nothing is ever
overwritten, a spoken path never becomes a real one, and a name that could mean
two files is asked about rather than guessed.

Run: python tests/test_filewrite.py
"""

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clio.capabilities import files  # noqa: E402
from clio.capabilities.filewrite import FileWriter, parse_write  # noqa: E402
from clio.core import fileops  # noqa: E402
from clio.core.fileops import FileOpError, FileOps, clean_name, free_name  # noqa: E402
from clio.core.permissions import Permission  # noqa: E402
from clio.llm.memory import ConversationMemory  # noqa: E402
from clio.orchestrator import Orchestrator  # noqa: E402


class FakeLLM:
    async def complete(self, messages, tier="default"):
        return "reply"


async def main():
    base = Path(tempfile.mkdtemp(prefix="clio-write-"))
    docs, downloads, desktop = base / "Documents", base / "Downloads", base / "Desktop"
    outside = base / "Outside"
    for folder in (docs, downloads, desktop, outside):
        folder.mkdir()
    roots = (docs, downloads, desktop)

    # A fake Recycle Bin: a folder, so undo can be tested without his real one.
    bin_dir = base / "bin"
    bin_dir.mkdir()

    def fake_recycle(path):
        shutil.move(str(path), str(bin_dir / Path(path).name))

    def fake_restore(path):
        held = bin_dir / Path(path).name
        if held.exists():
            shutil.move(str(held), str(path))
        return Path(path).exists()

    fileops._send_to_recycle_bin = fake_recycle
    fileops._restore_from_recycle_bin = fake_restore

    try:
        ops = FileOps(roots)

        # --- names are names, never paths ---

        assert clean_name("final report") == "final report"
        assert clean_name("..\\..\\Windows\\evil") == "Windowsevil", clean_name("..\\..\\Windows\\evil")
        assert clean_name('bad:name?"here"') == "badnamehere"
        for bad in ["", "   ", "..", "///", "con", "NUL.txt", "com1"]:
            try:
                clean_name(bad)
            except FileOpError:
                continue
            raise AssertionError(f"{bad!r} should have been refused")
        print("OK  spoken names are cleaned until they can only be names")

        # --- the fence ---

        assert ops.inside(docs / "a.txt") and ops.inside(desktop)
        assert not ops.inside(outside / "a.txt")
        assert not ops.inside(docs / ".." / "Outside" / "a.txt"), "resolved, not read literally"
        (outside / "precious.txt").write_text("keep me", encoding="utf-8")
        for attempt in (lambda: ops.recycle(outside / "precious.txt"),
                        lambda: ops.move(outside / "precious.txt", docs),
                        lambda: ops.copy(docs, outside),
                        lambda: ops.create_folder(outside, "x")):
            try:
                attempt()
            except FileOpError:
                continue
            raise AssertionError("an operation reached outside the roots")
        assert (outside / "precious.txt").read_text(encoding="utf-8") == "keep me"
        print("OK  nothing outside the configured folders can be touched, by any route")

        # --- creating, and never overwriting ---

        made = ops.create_folder(docs, "invoices")
        assert made.is_dir() and made.name == "invoices"
        again = ops.create_folder(docs, "invoices")
        assert again.name == "invoices (2)", again.name
        note = ops.create_file(docs, "ideas.md")
        assert note.exists() and note.read_text(encoding="utf-8") == ""
        (docs / "report.docx").write_text("the real report", encoding="utf-8")
        assert free_name(docs / "report.docx").name == "report (2).docx"
        print("OK  folders and files created, and a taken name becomes 'name (2)'")

        # --- copy, rename, move ---

        (desktop / "report.docx").write_text("a different report", encoding="utf-8")
        copied = ops.copy(docs / "report.docx", desktop)
        assert copied.name == "report (2).docx", "copying onto a name must not overwrite it"
        assert (desktop / "report.docx").read_text(encoding="utf-8") == "a different report"

        renamed = ops.rename(docs / "report.docx", "final report")
        assert renamed.name == "final report.docx", "the extension is kept unless he says one"

        moved = ops.move(renamed, downloads)
        assert moved.parent == downloads and moved.exists() and not renamed.exists()

        try:
            ops.move(made, made / "inside")
        except FileOpError as exc:
            assert "inside itself" in str(exc)
        else:
            raise AssertionError("a folder was moved inside itself")
        print("OK  copy, rename and move, none of them able to overwrite")

        # --- the bin, and undo all the way back ---

        ops.recycle(moved)
        assert not moved.exists() and (bin_dir / moved.name).exists()

        # One at a time: each undo is checked before the next one moves things on.
        said = ops.undo()
        assert "is back" in said and moved.exists(), said                     # the recycle
        said = ops.undo()
        assert "back where it was" in said and renamed.exists(), said         # the move
        said = ops.undo()
        assert (docs / "report.docx").exists() and not renamed.exists(), said  # the rename
        ops.undo()
        assert not copied.exists(), "undoing a copy bins the copy"            # the copy
        print("OK  undo walks back through bin, move, rename and copy in order")

        # Ten deep, then it stops.
        for i in range(15):
            ops.create_file(docs, f"n{i}.txt")
        assert len(ops.history()) == fileops.UNDO_DEPTH
        for _ in range(fileops.UNDO_DEPTH):
            ops.undo()
        assert ops.undo() == "There's nothing for me to undo."
        print(f"OK  undo reaches back {fileops.UNDO_DEPTH} actions and then says so")

        # --- what counts as a file request ---

        assert parse_write("make a folder called invoices in documents") == (
            "folder", {"name": "invoices", "where": "documents"})
        assert parse_write("move the budget to downloads")[0] == "move"
        assert parse_write("rename it to final report") == (
            "rename", {"what": "it", "name": "final report"})
        assert parse_write("undo that") == ("undo", {})
        for text in ["delete my reminders", "delete the timer", "what's the weather",
                     "delete c colon backslash windows", "move ..\\..\\secrets to desktop"]:
            assert parse_write(text) is None, (text, parse_write(text))
        print("OK  sentences parsed, and reminders, timers and spoken paths left alone")

        # --- resolving names to real files ---

        files.forget_index()
        (downloads / "budget 2026.xlsx").write_text("x", encoding="utf-8")
        (docs / "notes a.txt").write_text("x", encoding="utf-8")
        (desktop / "notes b.txt").write_text("x", encoding="utf-8")
        writer = FileWriter(roots)

        request = writer.resolve("move the budget to documents")
        assert request.source == downloads / "budget 2026.xlsx" and request.folder == docs, request
        assert FileWriter.describe(request) == "Moving budget 2026.xlsx from Downloads to Documents"

        ambiguous = writer.resolve("delete notes")
        assert ambiguous.refusal.startswith("That could be"), ambiguous
        assert "notes a.txt" in ambiguous.refusal and "notes b.txt" in ambiguous.refusal

        missing = writer.resolve("delete the dishwasher manual")
        assert "can't find anything called" in missing.refusal

        assert "not sure what you mean" in writer.resolve("delete it").refusal
        writer.note(downloads / "budget 2026.xlsx")
        assert writer.resolve("delete it").source == downloads / "budget 2026.xlsx"
        print("OK  names resolve to real files, ambiguity is asked about, 'it' is remembered")

        # A folder she has just made is findable at once, not a minute later.
        spoken = await writer.run(writer.resolve("make a folder called receipts in desktop"))
        assert spoken == "Made a folder called receipts in Desktop.", spoken
        assert writer.resolve("copy the budget to receipts").folder == desktop / "receipts"
        print("OK  a new folder is found straight away - the index is refreshed")

        # --- through the real router ---

        orchestrator = Orchestrator(
            wake_detector=None, turn_detector=None, stt=None, llm=FakeLLM(), speaker=None,
            persona_system_prompt="p", follow_up_window_s=1.0, file_roots=roots,
            memory_root=str(base / "memory"),
        )
        orchestrator._memory = ConversationMemory(provider=FakeLLM(), system_prompt="p")

        caps = {c.name: c for c in orchestrator._router.capabilities()}
        assert caps["file_write"].permission is Permission.FREE
        assert caps["file_move"].permission is Permission.CONFIRM
        assert caps["file_delete"].permission is Permission.CONFIRM

        matched = orchestrator._router.match("move the budget to documents")
        assert matched.intent == "file_move", matched
        assert matched.description == "Moving budget 2026.xlsx from Downloads to Documents"

        matched = orchestrator._router.match("delete the budget")
        assert matched.intent == "file_delete" and "Recycle Bin" in matched.description

        # A refusal is never taken to the gate: nothing to confirm.
        matched = orchestrator._router.match("delete notes")
        assert matched.intent == "file_write" and matched._payload.refusal, matched

        # "Undo that" follows whichever of files or the clipboard changed last.
        orchestrator._last_undoable = "clipboard"
        assert orchestrator._router.match("undo that").intent == "clipboard"
        orchestrator._last_undoable = "file"
        assert orchestrator._router.match("undo that").intent == "file_write"
        assert orchestrator._router.match("undo the clipboard").intent == "clipboard"

        # A typed "yes" runs a confirmed move; nothing moves before it.
        reply = await orchestrator.inject_text("move the budget to documents")
        assert "Say yes" in reply and (downloads / "budget 2026.xlsx").exists(), reply
        reply = await orchestrator.inject_text("yes")
        assert (docs / "budget 2026.xlsx").exists(), reply
        print("OK  free to create, confirm to move or bin, and 'undo that' follows the last change")

        print("\nAll file write checks passed.")
    finally:
        files.forget_index()
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())

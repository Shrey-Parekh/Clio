"""The task list: a plain checkbox file, ticked off by voice, never costing a
model call, and not stealing sentences that belong to notes, files or reminders.

Writes into a temporary directory, never his real list.
Run: python tests/test_tasks.py
"""

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clio.capabilities.tasks import TaskList, TaskRequest, parse_task_request  # noqa: E402
from clio.llm.memory import ConversationMemory  # noqa: E402
from clio.orchestrator import Orchestrator  # noqa: E402


class FakeLLM:
    def __init__(self):
        self.calls = 0

    async def complete(self, messages, tier="default"):
        self.calls += 1
        return "llm reply"


async def main():
    root = Path(tempfile.mkdtemp(prefix="clio-tasks-"))
    path = root / "tasks.md"
    try:
        # --- what counts as a task request ---

        for text, expected in [
            ("add buy printer ink to my list", TaskRequest("add", "buy printer ink")),
            ("put call the landlord on my to-do list", TaskRequest("add", "call the landlord")),
            ("Clio, add pay rent by friday at 5 to my to do list.", TaskRequest("add", "pay rent by friday at 5")),
            ("add a task to renew the passport", TaskRequest("add", "renew the passport")),
            ("new to-do: email the professor", TaskRequest("add", "email the professor")),
            ("add that to my list", TaskRequest("add", "")),
            ("what's on my list", TaskRequest("list")),
            ("I what's on my to do list", TaskRequest("list")),
            ("list my tasks", TaskRequest("list")),
            ("read me my tasks", TaskRequest("list")),
            ("what are my tasks", TaskRequest("list")),
            ("what do i still have to do today", TaskRequest("list")),
            ("tick off the invoice", TaskRequest("done", "the invoice")),
            ("cross buy printer ink off my list", TaskRequest("done", "buy printer ink")),
            ("mark renew the passport as done", TaskRequest("done", "renew the passport")),
        ]:
            assert parse_task_request(text) == expected, (text, parse_task_request(text))

        for text in ["add salt to the list of ingredients", "add milk to my shopping list",
                     "what do i have to do to reset the router", "put the kettle on",
                     "check the weather", "note down that the bins go out on tuesday",
                     "what's on tv tonight", "tick tock"]:
            assert parse_task_request(text) is None, (text, parse_task_request(text))
        print("OK  task requests matched, and named lists and near misses left alone")

        # --- the file ---

        tasks = TaskList(path)
        assert tasks.listing() == "Your list is empty."
        assert tasks.add("buy printer ink") == ("Added buy printer ink to your list.", True)
        tasks.add("send the invoice to Priya")
        tasks.add("renew the passport")
        assert tasks.add("Buy printer ink") == ("buy printer ink is already on your list.", False)

        written = path.read_text(encoding="utf-8")
        assert written.startswith("# Tasks"), written[:40]
        assert "- [ ] buy printer ink\n" in written and "- [ ] renew the passport\n" in written
        assert written.count("buy printer ink") == 1, "no duplicate for the same task said twice"
        assert not (root / "tasks.md.tmp").exists(), "the temp file is renamed into place, not left behind"

        spoken = tasks.listing()
        assert spoken == ("You've got 3 things on your list: buy printer ink, "
                          "send the invoice to Priya and renew the passport."), spoken
        print(f"OK  plain checkboxes, read back as a sentence: {spoken!r}")

        # --- ticking off, by part of the name ---

        assert tasks.tick("the invoice") == "Ticked off send the invoice to Priya."
        assert "- [x] send the invoice to Priya" in path.read_text(encoding="utf-8")
        assert tasks.tick("invoice") == "send the invoice to Priya is already ticked off."
        assert tasks.tick("the dishwasher") == "There's nothing on your list about the dishwasher."
        assert "invoice" not in tasks.listing(), "ticked tasks aren't read out as still to do"

        tasks.add("renew the car insurance")
        assert tasks.tick("renew") == "That could be renew the passport or renew the car insurance. Which one?"
        assert "- [x]" not in path.read_text(encoding="utf-8").split("Priya")[1], \
            "an ambiguous tick changes nothing"

        # Saying a ticked task again puts it back - that is the undo.
        assert tasks.add("send the invoice to Priya") == ("Put send the invoice to Priya back on your list.", False)
        assert "- [ ] send the invoice to Priya" in path.read_text(encoding="utf-8")
        print("OK  tick off by part of a name, ambiguity asks, saying it again undoes it")

        # --- his hand edits survive ---

        edited = path.read_text(encoding="utf-8") + "\nSome thought of his, not a task.\n* [X] done by hand\n"
        path.write_text(edited, encoding="utf-8")
        tasks.tick("passport")
        after = path.read_text(encoding="utf-8")
        assert "Some thought of his, not a task." in after and "* [X] done by hand" in after
        print("OK  lines he wrote by hand are kept as they are")

        # --- through the router, with no model involved ---

        llm = FakeLLM()
        orchestrator = Orchestrator(
            wake_detector=None, turn_detector=None, stt=None, llm=llm, speaker=None,
            persona_system_prompt="p", follow_up_window_s=1.0,
            notes_path=str(root / "notes.md"), tasks_path=str(root / "routed.md"),
        )
        orchestrator._memory = ConversationMemory(provider=llm, system_prompt="p")

        spoken, used = await orchestrator._handle_utterance("add buy milk to my list")
        assert used is False and spoken == "Added buy milk to your list.", spoken
        # A task with a time on it offers a reminder rather than setting one.
        spoken, _ = await orchestrator._handle_utterance("add pay rent by friday at 5 to my list")
        assert spoken.startswith("Added pay rent by friday at 5 to your list.") and "reminder" in spoken, spoken
        spoken, _ = await orchestrator._handle_utterance("tick off the milk")
        assert spoken == "Ticked off buy milk.", spoken
        spoken, _ = await orchestrator._handle_utterance("what's on my list")
        assert spoken == "Just one thing on your list: pay rent by friday at 5.", spoken
        assert llm.calls == 0, "a to-do list must never cost a call"
        print("OK  routed with zero model calls, and a timed task offers a reminder")

        caps = {c.name: c for c in orchestrator._router.capabilities()}
        assert caps["tasks"].permission.value == "free" and caps["tasks"].offline

        for text, intent in [
            ("what's on my list", "tasks"),
            ("list my tasks", "tasks"),
            ("write buy stamps on my list", "tasks"),
            ("read my notes", "notes"),
            ("write that down", "notes"),
            ("remind me to pay rent on friday at 5", "remind"),
            ("what reminders do i have", "remind_control"),
            ("stop the timer", "timer_control"),
        ]:
            matched = orchestrator._router.match(text)
            assert matched is not None and matched.intent == intent, (
                text, matched.intent if matched else None, intent)
        print("OK  free, offline, and no collisions with notes, reminders or timers")

        print("\nAll task list checks passed.")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())

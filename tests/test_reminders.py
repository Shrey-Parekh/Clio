"""Reminders and alarms (6.4): asked for in plain words, handed to Windows Task
Scheduler, and spoken - or at least shown - when they fire. Task Scheduler is
faked through an injected runner; the fire script is tested against a real local
WebSocket server, never the network.
Run: python tests/test_reminders.py
"""

import asyncio
import io
import csv
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clio.capabilities import schedule  # noqa: E402
from clio.capabilities.remind import (  # noqa: E402
    Reminder, ReminderCapability, parse_reminder_control, parse_reminder_request,
)

NOW = datetime(2026, 9, 11, 14, 30)  # a Friday afternoon


class FakeSchtasks:
    """Stands in for the schtasks runner: records argument lists, returns canned
    stdout, or fails."""

    def __init__(self, *, tasks=(), fail=False):
        self.calls = []
        self.tasks = list(tasks)  # (name, next_run, command)
        self.fail = fail

    def __call__(self, args):
        self.calls.append(args)
        if self.fail:
            raise schedule.ScheduleError("Windows said no")
        if args[0] == "/query":
            return self._csv()
        if args[0] == "/create":
            name = args[args.index("/tn") + 1]
            command = args[args.index("/tr") + 1]
            self.tasks.append((name, "12-Sep-26 07:00:00 AM", command))
        if args[0] == "/delete":
            name = args[args.index("/tn") + 1]
            self.tasks = [t for t in self.tasks if t[0] != name]
        return ""

    def _csv(self):
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\r\n")
        writer.writerow(["HostName", "TaskName", "Next Run Time", "Status", "Task To Run"])
        for name, next_run, command in self.tasks:
            writer.writerow(["PC", f"\\{name}", next_run, "Ready", command])
        # Anything else Windows happens to have scheduled must be ignored.
        writer.writerow(["PC", "\\Microsoft\\Windows\\Defrag\\ScheduledDefrag",
                         "N/A", "Ready", "defrag.exe"])
        return out.getvalue()


def build(tmp, fake, port=8765):
    schedule.run = fake
    return ReminderCapability(root=tmp, port=port)


async def main():
    tmp = Path(__file__).resolve().parent / "_reminders_tmp"

    # --- asked for, in the words he'd actually use ---

    for text, want in [
        ("remind me to take my meds at 7am",
         Reminder("take my meds", datetime(2026, 9, 12, 7, 0), None)),
        ("remind me to call mum in 20 minutes",
         Reminder("call mum", datetime(2026, 9, 11, 14, 50), None)),
        ("Hey Clio, remind me at 7:30 tomorrow to leave",
         Reminder("leave", datetime(2026, 9, 12, 7, 30), None)),
        ("remind me on Monday at 9 to send the invoice",
         Reminder("send the invoice", datetime(2026, 9, 14, 9, 0), None)),
        ("remind me to stretch at 4pm",  # later today, so today
         Reminder("stretch", datetime(2026, 9, 11, 16, 0), None)),
        ("every day at 7am remind me to stretch",
         Reminder("stretch", datetime(2026, 9, 12, 7, 0), "daily")),
        ("every weekday at 8 remind me to log in",  # said on a Friday afternoon
         Reminder("log in", datetime(2026, 9, 14, 8, 0), "MON,TUE,WED,THU,FRI")),
        ("every Monday at 6pm remind me to take the bins out",
         Reminder("take the bins out", datetime(2026, 9, 14, 18, 0), "MON")),
        ("wake me up at 6:15 tomorrow",
         Reminder("", datetime(2026, 9, 12, 6, 15), None)),
        ("set an alarm for 7am",
         Reminder("", datetime(2026, 9, 12, 7, 0), None)),
    ]:
        got = parse_reminder_request(text, now=NOW)
        assert got == want, (text, got, want)
    print("OK  reminders and alarms parsed: clock times, tomorrow, weekdays, every-day repeats")

    for text in [
        "set a timer for 10 minutes",
        "remind me to call mum",           # no time given
        "what's the time in Tokyo",
        "what's the news today",
        "remind me how the weather works",
        "",
    ]:
        assert parse_reminder_request(text, now=NOW) is None, text
    print("OK  timers, clock and news phrasings left alone, and no time means no reminder")

    for text, want in [
        ("what reminders do I have", ("list", "")),
        ("what's coming up", ("list", "")),
        ("cancel all my reminders", ("cancel", "")),
        ("cancel my 7am reminder", ("cancel", "7am")),
        ("forget the reminder about the bins", ("cancel", "the bins")),
        ("cancel the timer", None),
    ]:
        got = parse_reminder_control(text)
        got = (got.kind, got.which) if got is not None else None
        assert got == want, (text, got, want)
    print("OK  listing and cancelling asked for separately from setting")

    # --- what actually reaches schtasks ---

    fake = FakeSchtasks()
    cap = build(tmp, fake)
    spoken = cap.set(Reminder("take my meds", datetime(2026, 9, 12, 7, 0), None))
    create = next(c for c in fake.calls if c[0] == "/create")
    assert create[create.index("/sc") + 1] == "once"
    assert create[create.index("/st") + 1] == "07:00"
    assert create[create.index("/sd") + 1] == "12/09/2026"
    name = create[create.index("/tn") + 1]
    assert name.startswith(schedule.PREFIX), name
    reminder_id = name[len(schedule.PREFIX):]
    command = create[create.index("/tr") + 1]
    assert command.rstrip('"').endswith(reminder_id) and "remind.py" in command, command
    assert "take my meds" not in command, "the text goes in the sidecar, not the command line"
    assert (tmp / "reminders" / f"{reminder_id}.txt").read_text(encoding="utf-8") == "take my meds"
    assert "7" in spoken and "meds" in spoken, spoken
    print(f"OK  a one-off reminder becomes one scheduled task, text in a sidecar: {spoken!r}")

    fake = FakeSchtasks()
    cap = build(tmp, fake)
    cap.set(Reminder("stretch", datetime(2026, 9, 12, 7, 0), "daily"))
    create = next(c for c in fake.calls if c[0] == "/create")
    assert create[create.index("/sc") + 1] == "daily" and "/d" not in create
    assert "/sd" not in create, "a daily task doesn't need a start date"

    fake = FakeSchtasks()
    cap = build(tmp, fake)
    cap.set(Reminder("log in", datetime(2026, 9, 12, 8, 0), "MON,TUE,WED,THU,FRI"))
    create = next(c for c in fake.calls if c[0] == "/create")
    assert create[create.index("/sc") + 1] == "weekly"
    assert create[create.index("/d") + 1] == "MON,TUE,WED,THU,FRI"
    print("OK  daily and weekday repeats become Windows' own repeat schedules")

    # --- Windows is the list; nothing of ours is ---

    fake = FakeSchtasks()
    cap = build(tmp, fake)
    cap.set(Reminder("take my meds", datetime(2026, 9, 12, 7, 0), None))
    spoken = cap.listing()
    assert "meds" in spoken and "7" in spoken, spoken
    assert "defrag" not in spoken.lower(), "only Clio's own tasks are reminders"
    print(f"OK  the list is read back out of Task Scheduler: {spoken!r}")

    spoken = cap.cancel("")
    assert "cancel" in spoken.lower(), spoken
    assert any(c[0] == "/delete" for c in fake.calls)
    assert not fake.tasks
    assert cap.listing().startswith("You haven't"), cap.listing()
    print("OK  cancelling deletes the task, and then there's nothing to list")

    # --- a task that has already fired is not still a reminder ---
    # Found live: a one-off task stays in Task Scheduler after it runs, so
    # without this it would be read back out as though it were still coming.

    fake = FakeSchtasks(tasks=[("Clio-Reminder-spent", "N/A", "pythonw remind.py spent")])
    cap = build(tmp, fake)
    assert cap.listing() == "You haven't got any reminders set.", cap.listing()
    assert any(c[0] == "/delete" for c in fake.calls), "the spent task is swept away"
    assert not fake.tasks
    print("OK  a task that has already fired is deleted, not listed as upcoming")

    # --- a failure is said, never reported as set ---

    fake = FakeSchtasks(fail=True)
    cap = build(tmp, fake)
    spoken = cap.set(Reminder("take my meds", datetime(2026, 9, 12, 7, 0), None))
    assert "wouldn't let me" in spoken, spoken

    class Vanishes(FakeSchtasks):
        def __call__(self, args):
            out = super().__call__(args)
            self.tasks = []  # Windows accepted it and kept nothing
            return out

    fake = Vanishes()
    cap = build(tmp, fake)
    spoken = cap.set(Reminder("take my meds", datetime(2026, 9, 12, 7, 0), None))
    assert "didn't take" in spoken, spoken
    print("OK  a refused or vanishing task is said out loud, not reported as set")

    # --- the fire script: spoken when she's up, shown either way ---

    import websockets

    import clio.remind as fire

    toasts = []
    fire.toast = lambda title, text: toasts.append((title, text))

    received = []

    async def handler(connection):
        async for raw in connection:
            received.append(raw)

    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    fake = FakeSchtasks()
    cap = build(tmp, fake, port=port)
    cap.set(Reminder("take my meds", datetime(2026, 9, 12, 7, 0), None))
    create = next(c for c in fake.calls if c[0] == "/create")
    reminder_id = create[create.index("/tn") + 1][len(schedule.PREFIX):]

    assert await fire.fire(reminder_id, root=tmp, port=port) is True
    await asyncio.sleep(0.1)
    assert received and "take my meds" in received[0] and "announce" in received[0], received
    assert toasts and "take my meds" in toasts[0][1], toasts
    print("OK  Clio running: the reminder is sent to her to speak, and toasted as well")

    server.close()
    await server.wait_closed()

    toasts.clear()
    cap.set(Reminder("call mum", datetime(2026, 9, 12, 7, 0), None))
    create = [c for c in fake.calls if c[0] == "/create"][-1]
    reminder_id = create[create.index("/tn") + 1][len(schedule.PREFIX):]
    assert await fire.fire(reminder_id, root=tmp, port=port) is False, "nothing is listening now"
    assert toasts and "call mum" in toasts[0][1], toasts
    print("OK  Clio closed: the toast still shows, which is the whole point")

    toasts.clear()
    assert await fire.fire("nosuchid", root=tmp, port=port) is False
    assert toasts and "lost" in toasts[0][1].lower(), toasts
    print("OK  a lost sidecar still fires, saying the text is gone")

    # --- Windows' own CSV, parsed ---

    sample = (
        '"HostName","TaskName","Next Run Time","Status","Task To Run"\r\n'
        '"PC","\\Clio-Reminder-a3f","12-Sep-26 07:00:00 AM","Ready","pythonw remind.py a3f"\r\n'
        '"PC","\\Clio-Reminder-b7c","N/A","Disabled","pythonw remind.py b7c"\r\n'
        '"PC","\\OneDrive Reporting","01-Jan-27 09:00:00 AM","Ready","onedrive.exe"\r\n'
    )
    tasks = schedule.parse_query(sample)
    assert [t.id for t in tasks] == ["a3f", "b7c"], tasks
    assert tasks[0].next_run == datetime(2026, 9, 12, 7, 0)
    assert tasks[1].next_run is None
    print("OK  Task Scheduler's own CSV is parsed, and other people's tasks ignored")

    for path in sorted((tmp / "reminders").glob("*.txt")):
        path.unlink()
    (tmp / "reminders").rmdir()
    tmp.rmdir()

    print("\nAll reminder checks passed.")


if __name__ == "__main__":
    asyncio.run(main())

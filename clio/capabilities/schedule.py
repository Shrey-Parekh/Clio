"""The only module that knows `schtasks` exists.

Windows Task Scheduler is the whole store for reminders: one task per reminder,
named `Clio-Reminder-<id>`. It survives reboots, crashes and Clio not running,
which is exactly what an in-process timer cannot do - so nothing here keeps a
list of its own, and "what reminders are there" is answered by asking Windows.

The runner is a module-level name so tests can replace it; nothing here touches
the real Task Scheduler under test.
"""

from __future__ import annotations

import csv
import io
import subprocess
from dataclasses import dataclass
from datetime import datetime

from clio.core.logging import get_logger

log = get_logger("clio.capabilities.schedule")

PREFIX = "Clio-Reminder-"
_TIMEOUT_S = 15.0
# Windows prints "12-Sep-26 07:00:00 AM" here, and "N/A" for a task with no
# next run.
_NEXT_RUN = "%d-%b-%y %I:%M:%S %p"


class ScheduleError(RuntimeError):
    """schtasks refused, or isn't there. Said out loud, never swallowed: a
    reminder reported as set but not scheduled is the one unacceptable outcome."""


@dataclass(frozen=True)
class Task:
    name: str                 # Clio-Reminder-a3f
    id: str                   # a3f
    next_run: datetime | None
    command: str


def run(args: list[str]) -> str:
    """Every schtasks call goes through here. Replaced wholesale in tests."""
    try:
        result = subprocess.run(
            ["schtasks", *args],
            capture_output=True, text=True, timeout=_TIMEOUT_S,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError) as exc:
        raise ScheduleError(str(exc)) from exc
    if result.returncode != 0:
        raise ScheduleError((result.stderr or result.stdout).strip())
    return result.stdout


def create(name: str, command: str, when: datetime, repeat: str | None) -> None:
    """`repeat` is None for a one-off, "daily", or comma-separated day codes
    ("MON" / "MON,TUE,WED,THU,FRI") for a weekly one."""
    run(create_args(name, command, when, repeat))
    log.info(
        "Reminder scheduled",
        extra={"extra_fields": {"task": name, "when": when.isoformat(), "repeat": repeat}},
    )


def create_args(name: str, command: str, when: datetime, repeat: str | None) -> list[str]:
    args = ["/create", "/tn", name, "/tr", command, "/f", "/st", when.strftime("%H:%M")]
    if repeat is None:
        # ponytail: the date format follows the machine's locale, and this is
        # DD/MM/YYYY here. A wrong reading can't pass silently - the caller reads
        # the task back and compares the date before saying it's set.
        args += ["/sc", "once", "/sd", when.strftime("%d/%m/%Y")]
    elif repeat == "daily":
        args += ["/sc", "daily"]
    else:
        args += ["/sc", "weekly", "/d", repeat]
    return args


def query() -> list[Task]:
    """Clio's reminders, straight out of Task Scheduler. Anything else the
    machine has scheduled is not ours and is ignored."""
    return parse_query(run(["/query", "/fo", "csv", "/v"]))


def parse_query(output: str) -> list[Task]:
    tasks = []
    for row in csv.DictReader(io.StringIO(output)):
        name = (row.get("TaskName") or "").lstrip("\\")
        if not name.startswith(PREFIX):
            continue
        tasks.append(Task(
            name=name,
            id=name[len(PREFIX):],
            next_run=_parse_next_run(row.get("Next Run Time") or ""),
            command=row.get("Task To Run") or "",
        ))
    return tasks


def delete(name: str) -> None:
    run(["/delete", "/tn", name, "/f"])
    log.info("Reminder deleted", extra={"extra_fields": {"task": name}})


def _parse_next_run(value: str) -> datetime | None:
    try:
        return datetime.strptime(value.strip(), _NEXT_RUN)
    except ValueError:
        return None  # "N/A", or a locale that words it differently

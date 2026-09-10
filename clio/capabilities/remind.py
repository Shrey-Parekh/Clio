"""Reminders and alarms, on the deterministic path (6.4).

A timer lives inside Clio and dies with her. A reminder is for seven tomorrow
morning, so it is handed to Windows Task Scheduler instead: one task per
reminder, fired whether or not Clio is running. Nothing is stored here but the
words to say - see `schedule.py`, which owns the tasks, and `clio/remind.py`,
which is what Windows actually runs.

The parse either finds a clear time or returns None to fall through to
conversation. "Remind me to call mum" with no time is not a reminder.
"""

from __future__ import annotations

import re
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from clio.capabilities import schedule
# The same words and units timers already understand, so "in twenty minutes"
# means the same thing to both.
from clio.capabilities.timer import _DURATION_PATTERN, _NUMBER_WORDS, _UNIT_SECONDS
from clio.core.logging import get_logger

log = get_logger("clio.capabilities.remind")

# Windows schedules by the minute; anything shorter is a timer's job.
MIN_LEAD_S = 60
_MAX_SPOKEN = 4

_DAY_CODES = {
    "monday": "MON", "tuesday": "TUE", "wednesday": "WED", "thursday": "THU",
    "friday": "FRI", "saturday": "SAT", "sunday": "SUN",
}
_CODE_INDEX = {code: i for i, code in enumerate(["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"])}
_WEEKDAYS = "MON,TUE,WED,THU,FRI"
_WEEKEND = "SAT,SUN"


@dataclass(frozen=True)
class Reminder:
    text: str               # what to say; "" for a bare alarm
    when: datetime          # the next time it fires
    repeat: str | None      # None, "daily", or day codes ("MON" / "MON,TUE,...")


@dataclass(frozen=True)
class ReminderControl:
    kind: str   # "list" or "cancel"
    which: str  # "" means all of them


_PUNCT = re.compile(r"[.!?,;:]+$")
_LEAD = re.compile(
    r"^(?:(?:hey|hi|ok|okay|so|um|uh|clio|please|quickly|can you|could you|would you|"
    r"will you|i want you to|i need you to)\b[\s,]*)+"
)
_TRIGGER = re.compile(r"\bremind (?:me|us)\b|\bwake me(?: up)?\b|\bset (?:an? )?alarm\b")

_EVERY = re.compile(
    r"\bevery (?P<what>day|morning|evening|night|weekday|weekdays|weekend|"
    + "|".join(_DAY_CODES) + r")s?\b"
)
_AT = re.compile(
    r"\b(?:at|for|by)\s+(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*"
    r"(?P<half>a\.?m\.?|p\.?m\.?|o'clock)?"
)
_ON_DAY = re.compile(r"\b(?:on|next|this)\s+(?P<named>" + "|".join(_DAY_CODES) + r")\b")
_RELATIVE_DAY = re.compile(r"\b(?P<word>today|tonight|tomorrow)\b")
_IN = re.compile(r"\bin\s+" + _DURATION_PATTERN.pattern)
# What's left after the time is cut is the thing to say, minus the joining words.
_JOIN_HEAD = re.compile(r"^(?:to|that|about|please|and|,|-|:)\s+")
# Deliberately short: "in", "on" and "at" end real sentences ("log in", "put the
# bins on"), and losing the last word is worse than leaving a stray joining one.
_JOIN_TAIL = re.compile(r"\s+(?:to|that|about|please|and|,|-|:)$")


def parse_reminder_request(text: str, now: datetime | None = None) -> Reminder | None:
    now = now or datetime.now()
    spoken = _PUNCT.sub("", " ".join(text.lower().split()))
    spoken = _LEAD.sub("", spoken).strip()
    if not _TRIGGER.search(spoken):
        return None

    repeat, spoken = _take_repeat(spoken)
    when, spoken = _take_time(spoken, now, repeat)
    if when is None:
        return None

    return Reminder(text=_remaining(spoken), when=when, repeat=repeat)


def _take_repeat(spoken: str) -> tuple[str | None, str]:
    found = _EVERY.search(spoken)
    if found is None:
        return None, spoken
    what = found.group("what")
    if what.startswith("weekday"):
        repeat = _WEEKDAYS
    elif what == "weekend":
        repeat = _WEEKEND
    elif what in _DAY_CODES:
        repeat = _DAY_CODES[what]
    else:
        repeat = "daily"
    return repeat, _cut(spoken, found)


def _take_time(spoken: str, now: datetime, repeat: str | None) -> tuple[datetime | None, str]:
    relative = _IN.search(spoken)
    if relative is not None and repeat is None:
        number = relative.group("number")
        seconds = (float(number) if number[0].isdigit() else float(_NUMBER_WORDS[number])) \
            * _UNIT_SECONDS[relative.group("unit")]
        return now + timedelta(seconds=seconds), _cut(spoken, relative)

    clock = _AT.search(spoken)
    if clock is None:
        return None, spoken
    hour, minute = _hour_minute(clock)
    if hour is None:
        return None, spoken
    spoken = _cut(spoken, clock)

    named = _ON_DAY.search(spoken)
    day_word = _RELATIVE_DAY.search(spoken)
    if named is not None:
        spoken = _cut(spoken, named)
    if day_word is not None:
        spoken = _cut(spoken, day_word)

    if repeat is not None and repeat != "daily":
        when = _next_weekday(now, repeat.split(","), hour, minute)
    elif named is not None:
        when = _next_weekday(now, [_DAY_CODES[named.group("named")]], hour, minute)
    else:
        base = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if day_word is not None and day_word.group("word") == "tomorrow":
            base += timedelta(days=1)
        elif base <= now:
            base += timedelta(days=1)
        when = base
    return when, spoken


def _hour_minute(clock: re.Match) -> tuple[int | None, int]:
    hour, minute = int(clock.group("hour")), int(clock.group("minute") or 0)
    half = (clock.group("half") or "").replace(".", "")
    if half == "pm" and hour < 12:
        hour += 12
    elif half == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None, 0
    return hour, minute


def _next_weekday(now: datetime, codes: list[str], hour: int, minute: int) -> datetime:
    wanted = {_CODE_INDEX[c] for c in codes if c in _CODE_INDEX}
    for ahead in range(8):
        candidate = (now + timedelta(days=ahead)).replace(
            hour=hour, minute=minute, second=0, microsecond=0)
        if candidate.weekday() in wanted and candidate > now:
            return candidate
    return now + timedelta(days=1)  # unreachable with a non-empty day list


def _cut(spoken: str, found: re.Match) -> str:
    return " ".join((spoken[:found.start()] + " " + spoken[found.end():]).split())


def _remaining(spoken: str) -> str:
    spoken = " ".join(_TRIGGER.sub(" ", spoken).split())
    previous = None
    while spoken != previous:
        previous = spoken
        spoken = _JOIN_TAIL.sub("", _JOIN_HEAD.sub("", spoken)).strip()
    return spoken


_LIST = re.compile(
    r"^(?:what|which|any|list|do i have any)\b.*\breminders?\b|"
    r"^what(?:'?s| is) coming up|^what have i got coming up|^what'?s (?:next|scheduled)"
)
_CANCEL = re.compile(
    r"^(?:cancel|delete|remove|forget|clear|turn off)\s+(?:all\s+)?(?:my |the )?(?:all )?"
    r"(?P<pre>.*?)\breminders?\b(?:\s+(?:about|for|to|at)\s+(?P<post>.+))?$"
)


def parse_reminder_control(text: str) -> ReminderControl | None:
    spoken = _PUNCT.sub("", " ".join(text.strip().lower().split()))
    spoken = _LEAD.sub("", spoken).strip()
    cancel = _CANCEL.match(spoken)
    if cancel is not None:
        which = (cancel.group("post") or cancel.group("pre") or "").strip()
        return ReminderControl("cancel", which)
    if _LIST.search(spoken):
        return ReminderControl("list", "")
    return None


class ReminderCapability:
    """Sets, lists and cancels reminders. Holds no list: Task Scheduler has it.
    The only thing kept on disk is the words to say, keyed by task id."""

    def __init__(self, root: Path | str, port: int):
        self._root = Path(root) / "reminders"
        self._port = port

    def _live(self) -> list:
        """Every reminder Windows still intends to fire, sweeping away the ones
        it doesn't. A one-off task stays behind after it runs - `schtasks` can
        only mark a task for deletion with an end boundary it has no way to give
        - so a spent task is one with no next run, and it goes here along with
        its words. Anything Clio-named that he disabled by hand reads the same
        way and is cleared too; it is hers to keep or not."""
        live = []
        for task in schedule.query():
            if task.next_run is not None:
                live.append(task)
                continue
            try:
                schedule.delete(task.name)
            except schedule.ScheduleError:
                continue
            self._forget(task.id)
        return live

    def set(self, reminder: Reminder) -> str:
        reminder_id = uuid.uuid4().hex[:8]
        self._write(reminder_id, reminder.text)
        name = schedule.PREFIX + reminder_id
        try:
            command = _fire_command(self._root.parent, self._port, reminder_id)
            schedule.create(name, command, reminder.when, reminder.repeat)
            live = [t for t in self._live() if t.id == reminder_id]
        except schedule.ScheduleError as exc:
            log.warning("Scheduling a reminder failed",
                        extra={"extra_fields": {"task": name, "error": str(exc)}})
            self._forget(reminder_id)
            return "Windows wouldn't let me schedule that, so it isn't set."
        if not live:
            self._forget(reminder_id)
            return "Windows took that and then didn't keep it, so it didn't take. Nothing is set."
        said = say_when(reminder.when, reminder.repeat)
        return f"Okay, {said}: {reminder.text}." if reminder.text else f"Alarm set for {said}."

    def listing(self) -> str:
        try:
            tasks = self._live()
        except schedule.ScheduleError:
            return "I couldn't ask Windows what's scheduled just now."
        if not tasks:
            return "You haven't got any reminders set."
        tasks.sort(key=lambda t: t.next_run or datetime.max)
        lines = [f"{say_when(t.next_run, None)}, {self._read(t.id) or 'something I have no note for'}"
                 for t in tasks[:_MAX_SPOKEN]]
        rest = len(tasks) - len(lines)
        tail = f" And {rest} more after that." if rest > 0 else ""
        if len(lines) == 1:
            return f"One reminder: {lines[0]}.{tail}"
        return f"{len(tasks)} reminders. " + "; ".join(lines) + f".{tail}"

    def cancel(self, which: str) -> str:
        try:
            tasks = self._live()
        except schedule.ScheduleError:
            return "I couldn't ask Windows what's scheduled just now."
        if not tasks:
            return "You haven't got any reminders set."

        wanted = which.strip().lower()
        if wanted:
            tasks = [t for t in tasks if self._matches(t, wanted)]
            if not tasks:
                return f"I couldn't find a reminder about {which}."
        try:
            for task in tasks:
                schedule.delete(task.name)
                self._forget(task.id)
        except schedule.ScheduleError:
            return "Windows wouldn't let me delete that, so it's still set."
        if len(tasks) == 1:
            return "That's the reminder cancelled."
        return f"That's all {len(tasks)} reminders cancelled."

    def _matches(self, task, wanted: str) -> bool:
        said = say_when(task.next_run, None).lower()
        return (wanted in (self._read(task.id) or "").lower()
                or wanted in said
                or wanted.replace(" ", "") in said.replace(" ", ""))

    def _write(self, reminder_id: str, text: str) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        (self._root / f"{reminder_id}.txt").write_text(text, encoding="utf-8")

    def _read(self, reminder_id: str) -> str:
        try:
            return (self._root / f"{reminder_id}.txt").read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _forget(self, reminder_id: str) -> None:
        (self._root / f"{reminder_id}.txt").unlink(missing_ok=True)


def say_when(when: datetime | None, repeat: str | None, now: datetime | None = None) -> str:
    """Spoken, not printed: "tomorrow at 7am", "every weekday at 8am"."""
    if when is None:
        return "at some point"
    now = now or datetime.now()
    clock = when.strftime("%I:%M%p" if when.minute else "%I%p").lstrip("0").lower()
    if repeat == "daily":
        return f"every day at {clock}"
    if repeat == _WEEKDAYS:
        return f"every weekday at {clock}"
    if repeat == _WEEKEND:
        return f"every weekend at {clock}"
    if repeat:
        return f"every {when.strftime('%A')} at {clock}"
    days_off = (when.date() - now.date()).days
    if days_off == 0:
        return f"today at {clock}"
    if days_off == 1:
        return f"tomorrow at {clock}"
    if 2 <= days_off <= 6:
        return f"{when.strftime('%A')} at {clock}"
    return f"{when.strftime('%d %B')} at {clock}"


def _fire_command(root: Path, port: int, reminder_id: str) -> str:
    """What Windows runs. pythonw, so nothing flashes a console window. The root
    and port are baked in rather than read from the config, so the fire script
    imports nothing of Clio's."""
    executable = Path(sys.executable)
    windowless = executable.with_name("pythonw.exe")
    if windowless.exists():
        executable = windowless
    script = Path(__file__).resolve().parents[1] / "remind.py"
    return (f"{_quoted(executable)} {_quoted(script)} "
            f"{_quoted(root)} {port} {reminder_id}")


def _quoted(path: Path) -> str:
    # schtasks /tr fights nested quotes, so they're only used when a space forces it.
    return f'"{path}"' if " " in str(path) else str(path)

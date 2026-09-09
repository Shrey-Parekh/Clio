"""The time, the date, and how long until something.

Spoken, so formatting matters more than logic: a TTS engine reads "13:06" as
digits, so times come out as "six minutes past one" and dates as ordinals.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_STRIP = re.compile(r"[.!?,;:]+$")

# Spoken place name -> IANA zone. A full city database would be a lookup
# service, not a capability.
_PLACES = {
    "london": "Europe/London", "uk": "Europe/London", "england": "Europe/London",
    "new york": "America/New_York", "nyc": "America/New_York",
    "san francisco": "America/Los_Angeles", "california": "America/Los_Angeles",
    "los angeles": "America/Los_Angeles", "seattle": "America/Los_Angeles",
    "chicago": "America/Chicago", "texas": "America/Chicago",
    "toronto": "America/Toronto", "vancouver": "America/Vancouver",
    "india": "Asia/Kolkata", "mumbai": "Asia/Kolkata", "delhi": "Asia/Kolkata",
    "bangalore": "Asia/Kolkata", "bengaluru": "Asia/Kolkata", "pune": "Asia/Kolkata",
    "tokyo": "Asia/Tokyo", "japan": "Asia/Tokyo",
    "singapore": "Asia/Singapore", "hong kong": "Asia/Hong_Kong",
    "dubai": "Asia/Dubai", "uae": "Asia/Dubai",
    "sydney": "Australia/Sydney", "melbourne": "Australia/Melbourne",
    "berlin": "Europe/Berlin", "germany": "Europe/Berlin",
    "paris": "Europe/Paris", "france": "Europe/Paris",
    "amsterdam": "Europe/Amsterdam", "dublin": "Europe/Dublin",
    "moscow": "Europe/Moscow", "beijing": "Asia/Shanghai", "china": "Asia/Shanghai",
    "utc": "UTC", "gmt": "UTC",
}

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}

_PATTERNS: list[tuple[str, str]] = [
    # "is it" optional in the middle: covers both "what time is it in Tokyo"
    # and the terse "time in Tokyo".
    ("elsewhere", r"(?:what(?:'?s| is) the )?time (?:is it )?(?:in|over in) (?P<value>[\w ]+)"),
    ("days_until", r"how many days (?:until|till|til|to) (?P<value>.+)"),
    ("until", r"how (?:long|much longer) (?:until|till|til|to) (?P<value>.+)"),
    ("date", r"what(?:'?s| is) (?:today'?s? |the )?date|what(?:'?s| is) today"),
    ("day", r"what day is it|what day of the week"),
    ("time", r"what(?:'?s| is) the time|what time is it|do you have the time|"
             r"got the time|time please|tell me the time|the time right now"),
]

_COMPILED = [(kind, re.compile(p)) for kind, p in _PATTERNS]

# Digits only: a transcriber gives "6pm"/"18:30" reliably, "half six" not, so
# prose isn't accepted rather than guessed.
_CLOCK_TIME = re.compile(r"^(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<meridiem>am|pm)?$")
_DATE = re.compile(r"^(?:the )?(?P<day>\d{1,2})(?:st|nd|rd|th)? (?:of )?(?P<month>[a-z]+)$|"
                   r"^(?P<month2>[a-z]+) (?:the )?(?P<day2>\d{1,2})(?:st|nd|rd|th)?$")


def _normalise(text: str) -> str:
    return " ".join(_STRIP.sub("", text.strip().lower()).split())


def parse_clock_request(text: str) -> tuple[str, str] | None:
    lowered = _normalise(text)
    for kind, pattern in _COMPILED:
        found = pattern.search(lowered)
        if found is None:
            continue
        value = (found.groupdict().get("value") or "").strip()
        # Unknown place or unparseable target: fall through to conversation
        # rather than refuse with half a place name.
        if kind == "elsewhere" and value not in _PLACES:
            return None
        if kind in ("until", "days_until") and _target(kind, value) is None:
            return None
        return kind, value
    return None


def _ordinal(day: int) -> str:
    suffix = "th" if 10 <= day % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


def speak_time(moment: datetime) -> str:
    """Twelve-hour, in words — a TTS engine reads "13:06" as digits."""
    hour = moment.hour % 12 or 12
    minute = moment.minute
    part = "in the morning" if moment.hour < 12 else (
        "in the afternoon" if moment.hour < 18 else "in the evening"
    )
    if minute == 0:
        return f"{hour} o'clock {part}"
    if minute == 15:
        return f"quarter past {hour} {part}"
    if minute == 30:
        return f"half past {hour} {part}"
    if minute == 45:
        return f"quarter to {hour % 12 + 1} {part}"
    if minute < 30:
        return f"{minute} minutes past {hour} {part}"
    return f"{60 - minute} minutes to {hour % 12 + 1} {part}"


def _target(kind: str, value: str, now: datetime | None = None) -> datetime | None:
    """The moment being counted down to, or None if it wasn't clear."""
    now = now or datetime.now()

    if kind == "days_until":
        found = _DATE.match(value)
        if not found:
            return None
        parts = found.groupdict()
        month_name = parts["month"] or parts["month2"] or ""
        day = parts["day"] or parts["day2"]
        if month_name not in _MONTHS or day is None:
            return None
        try:
            target = now.replace(month=_MONTHS[month_name], day=int(day), hour=0,
                                 minute=0, second=0, microsecond=0)
        except ValueError:
            return None  # the 31st of February
        # A past date means next year, so December's "days until January" > 0.
        return target if target.date() >= now.date() else target.replace(year=now.year + 1)

    found = _CLOCK_TIME.match(value)
    if not found:
        return None
    hour = int(found.group("hour"))
    minute = int(found.group("minute") or 0)
    meridiem = found.group("meridiem")
    if hour > 23 or minute > 59:
        return None
    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    elif meridiem is None and hour < 8:
        # No am/pm, early hour: "how long until 6" means this evening.
        hour += 12

    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return target if target > now else target + timedelta(days=1)


def _spoken_gap(delta: timedelta) -> str:
    total = int(delta.total_seconds())
    hours, remainder = divmod(total, 3600)
    minutes = remainder // 60
    if hours and minutes:
        return f"{hours} hour{'s' if hours != 1 else ''} and {minutes} minutes"
    if hours:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{minutes} minute{'s' if minutes != 1 else ''}" if minutes else "less than a minute"


def answer(kind: str, value: str, now: datetime | None = None) -> str:
    now = now or datetime.now()

    if kind == "time":
        return f"It's {speak_time(now)}."

    if kind == "day":
        return f"It's {now.strftime('%A')}."

    if kind == "date":
        return f"It's {now.strftime('%A')} the {_ordinal(now.day)} of {now.strftime('%B')}."

    if kind == "elsewhere":
        try:
            there = datetime.now(ZoneInfo(_PLACES[value]))
        except (KeyError, ZoneInfoNotFoundError):
            return f"I don't know what time zone {value} is in."
        place = value.upper() if value in ("utc", "gmt", "uk", "uae", "nyc") else value.title()
        elsewhere_day = "" if there.date() == now.date() else f", {there.strftime('%A')} there"
        return f"It's {speak_time(there)} in {place}{elsewhere_day}."

    target = _target(kind, value, now)
    if target is None:
        return f"I couldn't work out when {value} is."

    if kind == "days_until":
        days = (target.date() - now.date()).days
        if days == 0:
            return "That's today."
        return (f"{days} day{'s' if days != 1 else ''}, "
                f"{target.strftime('%A')} the {_ordinal(target.day)}.")

    return f"{_spoken_gap(target - now)}, so {speak_time(target)}."

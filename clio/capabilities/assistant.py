""""What can you do" and "talk slower" — the two questions about Clio herself.

With no screen there's no menu or settings panel, so discovering a capability
or changing how she sounds means asking. The capability list is read from the
registry, never hand-written, so it can't go stale as capabilities are added.
"""

from __future__ import annotations

import re

_STRIP = re.compile(r"[.!?,;:]+$")

_PATTERNS: list[tuple[str, str]] = [
    ("help", r"what can you do|what are you (?:able|capable)|what can i ask|"
             r"what commands|list your (?:capabilities|abilities|skills)|"
             r"^help$|^what do you do$"),
    ("faster", r"(?:talk|speak|say it) faster|speed up|you'?re (?:too|talking too) slow"),
    ("slower", r"(?:talk|speak|say it) slower|slow down|you'?re (?:too fast|talking too fast)|"
               r"not so fast"),
    ("speed", r"how fast (?:are you|do you) (?:talking|speak|talk)|what(?:'?s| is) your speed"),
]

_COMPILED = [(kind, re.compile(p)) for kind, p in _PATTERNS]

# Spoken description per registered name. A capability with no line here still
# appears, as its own name, rather than vanishing from the answer.
_DESCRIPTIONS = {
    "timer": "set timers",
    "timer_control": "cancel them",
    "stopwatch": "run a stopwatch",
    "clock": "tell you the time anywhere in the world",
    "calculate": "do sums",
    "convert": "convert units",
    "currency": "convert currencies",
    "weather": "check the weather",
    "system": "check what your machine is doing",
    "network": "tell you what you're connected to",
    "control": "run the volume and your windows",
    "media": "skip and pause music",
    "power": "put the machine to sleep",
    "close": "close things",
    "open": "open apps and folders",
    "files": "find and summarise your files",
    "notes": "take notes",
    "web": "look things up online",
    "clipboard": "fix up whatever you've copied",
    "chance": "flip a coin or roll dice",
    "status": "tell you what's still working when something breaks",
    "diagnose": "explain what went wrong",
    "voice": "talk faster or slower",
}

# Not worth listing: he cannot usefully "ask for" these.
_UNLISTED = {"stop", "repeat", "help"}

# Spoken order (not registration order, which is by match precedence). Only the
# headline of each group — a spoken answer shouldn't recite a long list.
_HEADLINE = [
    "clock", "timer", "notes", "open", "files", "clipboard", "control", "system",
    "media", "weather", "calculate", "chance",
]
_MAX_SPOKEN = 8

_MIN_SPEED, _MAX_SPEED, _SPEED_STEP = 0.7, 1.5, 0.15


def parse_assistant_request(text: str) -> str | None:
    lowered = " ".join(_STRIP.sub("", text.strip().lower()).split())
    for kind, pattern in _COMPILED:
        if pattern.search(lowered):
            return kind
    return None


def parse_help_request(text: str) -> str | None:
    return "help" if parse_assistant_request(text) == "help" else None


def parse_voice_request(text: str) -> str | None:
    """Everything except help - registered as its own intent so "what can you
    do" and "slow down" stay separate answers."""
    kind = parse_assistant_request(text)
    return kind if kind and kind != "help" else None


def describe_capabilities(capabilities) -> str:
    """`capabilities` is whatever `IntentRouter.capabilities()` returned."""
    registered = {c.name for c in capabilities} - _UNLISTED
    if not registered:
        return "Nothing yet, apparently."

    spoken = [n for n in _HEADLINE if n in registered][:_MAX_SPOKEN]
    # Count everything registered, so "N other things" stays true.
    rest = len(registered) - len(spoken)
    phrases = [_DESCRIPTIONS.get(n, n) for n in spoken]
    if not phrases:
        phrases = [_DESCRIPTIONS.get(n, n) for n in sorted(registered)][:_MAX_SPOKEN]
        rest = len(registered) - len(phrases)

    head = ", ".join(phrases[:-1])
    tail = f" And {rest} other things." if rest > 0 else ""
    return f"I can {head}, and {phrases[-1]}.{tail} Nearly all of it without going online."


def adjust_speed(speaker, direction: str) -> str:
    """Changes speaking speed for this session only; not persisted."""
    current = getattr(speaker, "speed", None)
    if current is None:
        return "I can't change my speed right now."

    if direction == "speed":
        if abs(current - 1.0) < 0.01:
            return "Normal speed."
        return f"{'Faster' if current > 1.0 else 'Slower'} than normal."

    step = _SPEED_STEP if direction == "faster" else -_SPEED_STEP
    target = round(min(_MAX_SPEED, max(_MIN_SPEED, current + step)), 2)
    if abs(target - current) < 0.01:
        return "That's as fast as I go." if step > 0 else "That's as slow as I go."

    speaker.speed = target
    return "Better?" if direction == "faster" else "Like this?"

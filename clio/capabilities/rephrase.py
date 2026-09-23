"""When no phrase matches, the model rewords the request into one that does (7.6).

"Can you get the ewaste thing going" is plainly an action, and no pattern will
ever match it. Rather than giving the model tools with their own argument
schemas - a second set of rules beside the parsers, which is what 1.7 built
and nothing used - the model is asked for one thing: the same request, said the
way Clio already understands. That sentence then goes through the ordinary
router. So the parsers stay the argument validation, the permission table stays
the authority, and a made-up answer matches nothing and falls back to
conversation.

What the model picked is always read back before it runs, even for harmless
things: it was a guess, and a guess can be wrong (his choice, 7.6 design).

Deleting, sending and powering off are not offered at all. The confirmation
would catch a wrong guess, but those should only ever come from his own words.
"""

from __future__ import annotations

import re

from clio.capabilities.web import _LEAD
from clio.core.logging import get_logger

log = get_logger("clio.capabilities.rephrase")

# One sentence per capability, each checked by tests/test_rephrase.py to match
# its own intent. They show the model the phrasing, not the only phrasing.
EXAMPLES = {
    "timer": "set a timer for 10 minutes",
    "remind": "remind me to call mum at 6pm",
    "stopwatch": "start a stopwatch",
    "clock": "what time is it in Tokyo",
    "network": "what wifi am I on",
    "system": "how much memory is free",
    "calculate": "what is 15 percent of 240",
    "convert": "convert 5 miles to kilometres",
    "currency": "convert 100 dollars to rupees",
    "notes": "take a note buy milk",
    "tasks": "add finish the report to my tasks",
    "email": "do i have any new email",
    "projects": "what projects can you run",
    "project_run": "run the ewaste project",
    "project_stop": "stop the ewaste project",
    "file_write": "make a folder called invoices in documents",
    "shell_read": "what's the git status of clio",
    "shell": "run pip install requests in clio",
    "web": "search for the best budget mechanical keyboard",
    "open": "open spotify",
    "close": "close spotify",
    "media": "pause the music",
    "control": "turn the volume down",
    "chance": "flip a coin",
    "files": "find my resume",
}

# Openers of a request to do something. A question or a remark ("I'm tired",
# "what do you think of rust") goes straight to conversation, without the
# extra call.
_ACTION = re.compile(
    r"^(?:run|start|launch|open|close|quit|stop|kill|set|make|create|add|put|take|"
    r"write|note|remind|check|find|search|look|get|fetch|show|tell|give|pause|play|"
    r"skip|mute|unmute|turn|switch|install|update|pull|convert|work|figure|copy|"
    r"do|fire|kick|boot|spin|bring|save|flip|roll)\b",
    re.IGNORECASE,
)
_NONE = "NONE"
_MAX_CHARS = 160


def looks_like_action(text: str) -> bool:
    spoken = _LEAD.sub("", " ".join(text.lower().split())).strip()
    return bool(_ACTION.match(spoken))


def prompt(text: str, offered: list[str]) -> str:
    lines = "\n".join(f"- {EXAMPLES[name]}" for name in offered)
    return (
        "You turn a spoken request into one short command for a voice assistant "
        "that only understands commands shaped like these examples:\n"
        f"{lines}\n\n"
        "Rewrite the request as ONE command in the same shape, keeping his names, "
        "numbers and places exactly. Reply with the command only - no quotes, no "
        f"explanation. If it is not one of these kinds of command, reply {_NONE}.\n\n"
        f"Request: {text}"
    )


def clean(raw: str | None) -> str | None:
    """The model's answer as a sentence to route, or None. Only the first line
    counts, and anything long enough to be an explanation is thrown away."""
    text = (raw or "").strip()
    if not text:
        return None
    line = text.splitlines()[0].strip().strip(".\"'`").strip()
    if not line or line.upper().startswith(_NONE) or len(line) > _MAX_CHARS:
        return None
    return line


async def rephrase(llm, text: str, offered: list[str]) -> str | None:
    """The request, reworded into a command Clio knows, or None."""
    if not offered:
        return None
    raw = await llm.complete([{"role": "user", "content": prompt(text, offered)}], tier="fast")
    said = clean(raw)
    log.info("Reworded a request", extra={"extra_fields": {"heard": text, "as": said}})
    return said

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
# Found live: asked to "pull the latest ewaste code", the model said NONE - no
# example showed that git and pip commands are sayable at all.
_PROGRAMS = ("Programs (git, pip, python, npm, cargo) run as: run <command and its "
             "arguments> in <project or folder>, e.g. run git pull in ewaste. "
             "Never invent a script or file name he did not say; to start or train a "
             "project, say run the <name> project.")
# Also found live: "start training" became "run python train.py in ewaste" - a
# script name nobody said. Starting a project goes through its registered
# command (7.2), never one the model made up.


def looks_like_action(text: str) -> bool:
    spoken = _LEAD.sub("", " ".join(text.lower().split())).strip()
    return bool(_ACTION.match(spoken))


def prompt(text: str, offered: list[str]) -> str:
    lines = "\n".join(f"- {EXAMPLES[name]}" for name in offered)
    return (
        "You turn a spoken request into one short command for a voice assistant "
        "that only understands commands shaped like these examples:\n"
        f"{lines}\n{_PROGRAMS}\n\n"
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


# --- 7.7: several steps in one sentence ---

MAX_STEPS = 6
_JOINED = re.compile(r"\b(?:and|then)\b|,", re.IGNORECASE)
_NUMBERING = re.compile(r"^\s*(?:\d+[.):]|[-*•])\s*")


def looks_like_plan(text: str) -> bool:
    """An order with a join in it: "pull ewaste, install its requirements and
    start training". Whether it really is several steps is the model's call."""
    return looks_like_action(text) and bool(_JOINED.search(text))


def plan_prompt(text: str, offered: list[str]) -> str:
    lines = "\n".join(f"- {EXAMPLES[name]}" for name in offered)
    return (
        "You break a spoken request into the steps a voice assistant should run, "
        "in order. It only understands commands shaped like these examples:\n"
        f"{lines}\n{_PROGRAMS}\n\n"
        "Write one command per line, in the same shape as the examples, keeping his "
        "names, numbers, folders and projects exactly. Use the fewest steps that do "
        f"what he asked, at most {MAX_STEPS}. No numbering, no explanation. If any "
        f"part cannot be said as one of these kinds of command, reply {_NONE}.\n\n"
        f"Request: {text}"
    )


def parse_steps(raw: str | None) -> list[str] | None:
    """The model's plan as sentences to route, or None. A NONE anywhere means
    it could not say part of it - better nothing than a plan with a hole."""
    steps = []
    for line in (raw or "").splitlines():
        said = clean(_NUMBERING.sub("", line))
        if said is None:
            if line.strip().upper().startswith(_NONE):
                return None
            continue
        steps.append(said)
    return steps or None


async def plan(llm, text: str, offered: list[str]) -> list[str] | None:
    """The request as ordered sentences Clio knows, or None. The reasoning
    tier: ordering a messy sentence is where the small model slips."""
    if not offered:
        return None
    raw = await llm.complete([{"role": "user", "content": plan_prompt(text, offered)}])
    steps = parse_steps(raw)
    log.info("Planned a request", extra={"extra_fields": {"heard": text, "steps": steps}})
    return steps


async def rephrase(llm, text: str, offered: list[str]) -> str | None:
    """The request, reworded into a command Clio knows, or None."""
    if not offered:
        return None
    raw = await llm.complete([{"role": "user", "content": prompt(text, offered)}], tier="fast")
    said = clean(raw)
    log.info("Reworded a request", extra={"extra_fields": {"heard": text, "as": said}})
    return said

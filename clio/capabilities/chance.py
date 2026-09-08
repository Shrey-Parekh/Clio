"""Coins, dice, a number, or picking between things he can't decide on.

Small, but it is the one thing an assistant gets asked that a language model
answers badly: ask a model for a random number and it says 7, nearly every
time, because that is what people pick in its training data. This is on the
deterministic path precisely so the answer is actually random.
"""

from __future__ import annotations

import random
import re

_STRIP = re.compile(r"[.!?,;:]+$")

_COIN = re.compile(r"flip (?:a )?coin|toss (?:a )?coin|heads or tails|coin flip")
_DICE = re.compile(r"roll (?:(?P<count>\d+|a|an|two|three) )?(?:dice|die|d(?P<sides>\d{1,3})\b)")
_NUMBER = re.compile(r"(?:pick|give me|choose|random)(?: a)? number "
                     r"(?:between |from )?(?P<low>\d+)(?: and | to |-)(?P<high>\d+)")
_PICK = re.compile(r"(?:pick|choose|decide) (?:between |from )?(?P<options>.+)")

_WORD_COUNTS = {"a": 1, "an": 1, "two": 2, "three": 3}
_MAX_DICE = 10
_MAX_SIDES = 100

# "pick between the red one and the blue one" splits on these. Anything that
# does not split into at least two options is not a request to choose.
_SPLIT = re.compile(r"\s+or\s+|\s*,\s*")


def parse_chance_request(text: str) -> tuple[str, tuple] | None:
    lowered = " ".join(_STRIP.sub("", text.strip().lower()).split())

    if _COIN.search(lowered):
        return "coin", ()

    found = _NUMBER.search(lowered)
    if found:
        low, high = int(found.group("low")), int(found.group("high"))
        return ("number", (low, high)) if low < high else None

    found = _DICE.search(lowered)
    if found:
        raw = found.group("count")
        count = int(raw) if raw and raw.isdigit() else _WORD_COUNTS.get(raw or "a", 1)
        sides = int(found.group("sides") or 6)
        if not (1 <= count <= _MAX_DICE) or not (2 <= sides <= _MAX_SIDES):
            return None
        return "dice", (count, sides)

    found = _PICK.search(lowered)
    if found:
        options = [o.strip() for o in _SPLIT.split(found.group("options")) if o.strip()]
        # One option is not a choice - "pick a good restaurant" is a question
        # for the model, not a coin toss.
        return ("pick", tuple(options)) if len(options) >= 2 else None

    return None


def decide(kind: str, args: tuple, rng: random.Random | None = None) -> str:
    rng = rng or random.SystemRandom()

    if kind == "coin":
        return f"{rng.choice(('Heads', 'Tails'))}."

    if kind == "number":
        low, high = args
        return f"{rng.randint(low, high)}."

    if kind == "dice":
        count, sides = args
        rolls = [rng.randint(1, sides) for _ in range(count)]
        if count == 1:
            return f"{rolls[0]}."
        return f"{', '.join(str(r) for r in rolls)}. {sum(rolls)} altogether."

    return f"{rng.choice(args).capitalize()}."

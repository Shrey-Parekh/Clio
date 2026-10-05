"""What "this" means (8.3), worked out from what he is looking at.

A sentence that says "this" is checked against the window in front, then
either rewritten into a sentence the router already knows ("summarise this
page" becomes "summarise https://..."), or answered from the text he has
selected. In order: the selection, the page, the open file or folder, and in
the end the 8.2 screenshot, which needs nothing from here. Closing, minimising
and maximising "this" are the window itself, and live with the other window
commands in control.py.

Only these exact shapes are looked at, so "this evening" or "this week" never
costs a look at the screen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from clio.capabilities.clipboard import looks_like_a_secret
from clio.core.context import Context

_LEAD = re.compile(r"^(?:(?:hey |ok |okay )?clio,? )?(?:(?:can|could|would) you |please |just )*")
_TAIL = re.compile(r"(?: for me| please)+$")
_PUNCT = re.compile(r"[.!?,;:]+$")

_THING = r"(?:page|article|site|website|tab|link)"
_DOC = r"(?:file|document|doc|spreadsheet|sheet|pdf|presentation|deck|folder)"

_LOOK_UP = re.compile(r"^(?:look this up|search (?:for )?this|google this)$")
_ABOUT_SELECTION = re.compile(
    r"^(?:translate this(?: (?:in)?to \w+)?|what does this (?:mean|say)|explain this|define this|"
    r"what is this|what's this|summari[sz]e this|read this(?: out)?(?: to me)?(?: out loud)?|"
    r"rewrite this(?: .+)?|fix (?:the )?(?:grammar|spelling) (?:in|of) this)$")
_PAGE = re.compile(
    rf"^(?:summari[sz]e this(?: {_THING})?|(?:read(?: me)?|tl;?dr) this {_THING}|"
    rf"what(?:'s| is) this {_THING} about|explain this {_THING})$")
_SAVE_LINK = re.compile(rf"^(?:save|bookmark|note(?: down)?) this {_THING}$")
# File verbs the existing file features already understand by name. "Read
# this" alone stays with the screenshot: it means what he can see.
_FILE = re.compile(
    rf"^(?P<verb>summari[sz]e|read(?: me)?|where(?:'s| is)|move|copy|rename|delete|bin|"
    rf"what(?:'s| is) in) this(?P<doc> {_DOC})?(?P<rest>(?: (?:to|into|as) .+)?(?: saved)?)$")


@dataclass(frozen=True)
class Resolution:
    kind: str     # "rewrite": route `text`; "ask": answer `text` with the model; "refuse": say `text`
    text: str
    why: str      # for the log: which meaning of "this" was used


def _said(text: str) -> str:
    said = _PUNCT.sub("", text.strip().lower())
    return _TAIL.sub("", _LEAD.sub("", said)).strip()


def wants_context(text: str) -> bool:
    """Cheap, and checked first: only these sentences cost a look."""
    said = _said(text)
    return any(p.match(said) for p in (_LOOK_UP, _ABOUT_SELECTION, _PAGE, _SAVE_LINK, _FILE))


def resolve(text: str, context: Context) -> Resolution | None:
    """None: no meaning here; route the sentence as it was."""
    said = _said(text)
    selection = " ".join(context.selection.split())

    if selection:
        if looks_like_a_secret(selection):
            # Selected, maybe, to copy - never to send anywhere.
            return Resolution("refuse", "That looks like a password or a key, so I'm leaving it alone.",
                              "secret selected")
        if _LOOK_UP.match(said):
            return Resolution("rewrite", f"search for {selection[:200]}", "selection")
        if _ABOUT_SELECTION.match(said) or _PAGE.match(said):
            where = context.app or "the window in front"
            return Resolution("ask", f'{text.strip()}\n\n"This" is the text he has selected in '
                                     f"{where}:\n{selection}", "selection")

    if context.url:
        if _PAGE.match(said):
            return Resolution("rewrite", f"summarise {context.url}", "page")
        if _SAVE_LINK.match(said):
            title = re.split(r"\s+[-–—|]\s+", context.title)[0].strip() or "a link"
            return Resolution("rewrite", f"make a note {title} {context.url}", "page")

    name = context.file_name or context.folder
    found = _FILE.match(said)
    if name and found:
        verb = found.group("verb")
        if verb.startswith("read") and not found.group("doc"):
            return None
        if verb.startswith("what"):
            return Resolution("rewrite", f"what's in {name}", "folder" if context.folder else "file")
        return Resolution("rewrite", f"{verb} {name}{found.group('rest')}",
                          "folder" if context.folder else "file")
    return None

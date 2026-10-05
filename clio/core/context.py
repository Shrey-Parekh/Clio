"""What he is looking at (8.3), so "this" can mean something.

Read through Windows' own accessibility interface (UI Automation), the one
screen readers use: the page address from a browser's address bar, the text he
has selected, and the open file's name from the window title. Nothing presses
a key - copying a selection with Ctrl+C would stop whatever runs in a terminal -
and nothing is read unless he says "this".
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

from clio.core import screen
from clio.core.logging import get_logger

log = get_logger("clio.core.context")

BROWSERS = {"msedge.exe", "chrome.exe", "brave.exe"}
# Apps whose title starts with the open file's name: "budget.docx - Word",
# "* notes.txt - Notepad", "● screen.py - Clio - Visual Studio Code".
EDITORS = {"winword.exe", "excel.exe", "powerpnt.exe", "notepad.exe", "notepad++.exe",
           "code.exe", "acrobat.exe", "acrord32.exe", "sumatrapdf.exe", "wordpad.exe"}
EXPLORER = "explorer.exe"
MAX_SELECTION = 3000   # Groq's per-minute budget is ~8k tokens; a selection shares it

_INVISIBLE = re.compile(r"[​-‏⁠﻿]")   # Edge puts one in "Microsoft​ Edge"
_MARKERS = re.compile(r"^[\s*●•]+")                        # unsaved-changes marks


@dataclass(frozen=True)
class Context:
    app: str = ""          # "Notepad", "Microsoft Edge"
    exe: str = ""
    title: str = ""
    url: str = ""          # browsers only
    selection: str = ""    # selected text, where the app exposes it
    file_name: str = ""    # the open document, from an editor's title
    folder: str = ""       # the folder Explorer is showing


def _segments(title: str) -> list[str]:
    title = _INVISIBLE.sub("", title)
    return [s.strip() for s in re.split(r"\s+[-–—|]\s+", title) if s.strip()]


def app_name(window: screen.Window) -> str:
    """What to call the window out loud: the app part of its title."""
    parts = _segments(window.title)
    if len(parts) > 1:
        return parts[-1]
    return Path(window.exe).stem.capitalize() or (parts[0] if parts else "that window")


def file_from_title(window: screen.Window) -> str:
    if window.exe.lower() not in EDITORS:
        return ""
    parts = _segments(window.title)
    if len(parts) < 2:
        return ""
    name = _MARKERS.sub("", parts[0])
    # A new document has no file behind it yet.
    if re.fullmatch(r"(?:untitled|document|book|presentation)\s*\d*(?:\.\w+)?", name, re.I):
        return ""
    return name


def folder_from_title(window: screen.Window) -> str:
    if window.exe.lower() != EXPLORER:
        return ""
    parts = _segments(window.title)
    # ponytail: the title is the folder's name, not its path - two folders
    # with one name are asked about by the file features. Shell.Application's
    # LocationURL gives the exact path if that ever matters.
    return parts[0] if len(parts) > 1 and parts[-1] == "File Explorer" else ""


def front_window() -> screen.Window | None:
    """The window "this" means: the one in front, past Clio's own."""
    return screen.pick(screen._z_order())


# --- UI Automation ----------------------------------------------------------

_uia = None
_UIA = None


def _automation():
    global _uia, _UIA
    if _uia is None:
        import comtypes.client

        _UIA = comtypes.client.GetModule("UIAutomationCore.dll")
        _uia = comtypes.client.CreateObject(
            "{ff48dba4-60ef-4201-aa87-54103eef594e}", interface=_UIA.IUIAutomation)
    return _uia, _UIA


def _url(window: screen.Window) -> str:
    uia, UIA = _automation()
    root = uia.ElementFromHandle(window.hwnd)
    edit = root.FindFirst(UIA.TreeScope_Descendants, uia.CreatePropertyCondition(
        UIA.UIA_ControlTypePropertyId, UIA.UIA_EditControlTypeId))
    value = str(edit.GetCurrentPropertyValue(UIA.UIA_ValueValuePropertyId) or "") if edit else ""
    # ponytail: the first edit box is the address bar in Edge and Chrome today;
    # a browser that puts another one first would give a wrong or empty URL.
    value = value.strip()
    if not value or " " in value or "." not in value:
        return ""   # a search being typed, not an address
    return value if re.match(r"^\w+://", value) else "https://" + value


def _selection(window: screen.Window) -> str:
    """The selected text in the focused control, read rather than copied."""
    uia, UIA = _automation()
    # Chromium only builds its accessibility tree once something asks, so the
    # first question after a page loads can find nothing. One short retry.
    for attempt in range(2):
        element = uia.GetFocusedElement()
        # Typed into Clio's chat window, the focus is there, not in the window
        # he means - and what he typed is not a selection.
        # Nothing focused comes back as a NULL pointer, not None (found live).
        if not element or element.CurrentProcessId != screen.process_id(window.hwnd):
            return ""
        walker = uia.ControlViewWalker
        for _ in range(12):   # up from a caret inside a paragraph to its document
            if not element:
                break
            pattern = element.GetCurrentPattern(UIA.UIA_TextPatternId)
            if pattern:
                ranges = pattern.QueryInterface(UIA.IUIAutomationTextPattern).GetSelection()
                text = " ".join(
                    ranges.GetElement(i).GetText(MAX_SELECTION) for i in range(ranges.Length))
                return text.strip()
            element = walker.GetParentElement(element)
        if attempt == 0 and window.exe.lower() in BROWSERS:
            time.sleep(1.0)
    return ""


def snapshot() -> Context:
    """Everything "this" could mean, read now. Blocking - run it in a thread.
    A part that can't be read is left empty, never raised: the caller falls
    back to the next meaning, and in the end to a screenshot."""
    window = front_window()
    if window is None:
        return Context()
    exe = window.exe.lower()
    url = selection = ""
    try:
        if exe in BROWSERS:
            url = _url(window)
        selection = _selection(window)
    except Exception:
        log.exception("Couldn't read the window in front")
    context = Context(app=app_name(window), exe=exe, title=window.title, url=url,
                      selection=selection, file_name=file_from_title(window),
                      folder=folder_from_title(window))
    log.info("Looked at the window in front", extra={"extra_fields": {
        "app": context.app, "url": bool(url), "selection": len(selection),
        "file": context.file_name, "folder": context.folder}})
    return context

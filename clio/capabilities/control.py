"""Driving the machine itself - volume, windows, displays, locking, sleeping.

Split across two intents rather than one, because they are not the same risk.
`control` is everything reversible in a second: volume, focus, minimise,
display arrangement, locking the screen. `power` is sleeping the machine, which
is the first CONFIRM-tier capability in the codebase - it ends whatever he was
doing, and a misheard "go to sleep" that acted immediately would be the exact
failure the tier system was built for.

Brightness is attempted honestly and often refused. `WmiMonitorBrightness`
only exists for panels that expose it through the driver - laptops, mostly. An
external monitor on a desktop reports nothing, and this machine is one, so she
says the display doesn't support it rather than silently doing nothing.
"""

from __future__ import annotations

import asyncio
import ctypes
import re
import subprocess
from ctypes import wintypes
from dataclasses import dataclass

from clio.core.logging import get_logger

log = get_logger("clio.control")

_STEP = 10  # percent per "louder" / "quieter" - one notch he can hear
_PS_TIMEOUT_S = 6.0

_user32 = ctypes.windll.user32

_SW_RESTORE, _SW_MINIMIZE, _SW_MAXIMIZE = 9, 6, 3
_VK_MENU, _VK_LWIN, _VK_D = 0x12, 0x5B, 0x44
# The media keys a keyboard sends. Windows routes them to whatever is playing,
# so this works for Spotify, a browser tab or a video player without knowing
# which of them it is - far better than automating any one app's UI.
_VK_MEDIA = {
    "play_pause": 0xB3, "next_track": 0xB0, "previous_track": 0xB1, "stop_media": 0xB2,
}
_KEYUP = 0x0002


@dataclass(frozen=True)
class Action:
    kind: str
    value: str = ""


# Ordered, most specific first. Every pattern is anchored on a verb he'd
# actually say, because this is the capability with the most authority and the
# loosest pattern here would swallow half of normal conversation.
_CONTROL_PATTERNS: list[tuple[str, str]] = [
    ("volume_set", r"(?:set|put|turn)?\s*(?:the\s+)?volume\s+(?:to|at)\s+(?P<value>\d{1,3})"),
    ("volume_query", r"what(?:'?s| is) the (?:current )?volume|how loud is it"),
    ("mute", r"^(?:please\s+)?mute(?:\s+(?:the\s+)?(?:sound|audio|volume|speakers))?$"),
    ("unmute", r"^(?:please\s+)?unmute(?:\s+(?:the\s+)?(?:sound|audio|volume|speakers))?$"),
    ("volume_up", r"(?:volume|sound)\s+up|turn (?:it|the (?:volume|sound)) up|louder"),
    ("volume_down", r"(?:volume|sound)\s+down|turn (?:it|the (?:volume|sound)) down|quieter"),
    ("brightness_set", r"(?:set|turn)?\s*(?:the\s+)?brightness\s+(?:to|at)\s+(?P<value>\d{1,3})"),
    ("brightness_up", r"brightness up|brighter|turn up the brightness"),
    ("brightness_down", r"brightness down|dimmer|dim the (?:screen|display)|turn down the brightness"),
    # \b matters: without it "unmute spotify" matches the mute pattern,
    # because "mute" is a substring of "unmute".
    ("app_mute", r"\bmute (?P<value>[\w .-]+)"),
    ("app_unmute", r"unmute (?P<value>[\w .-]+)"),
    ("minimise_all", r"minimi[sz]e everything|show (?:me )?the desktop|hide everything"),
    ("minimise", r"minimi[sz]e (?:the\s+|my\s+)?(?P<value>.+)"),
    ("maximise", r"maximi[sz]e (?:the\s+|my\s+)?(?P<value>.+)"),
    ("display_extend", r"extend (?:the\s+)?(?:display|screen|monitor)s?|use both (?:screens|monitors)"),
    ("display_clone", r"(?:duplicate|mirror|clone) (?:the\s+)?(?:display|screen|monitor)s?"),
    ("display_single", r"(?:just|only) (?:the\s+)?(?:main|primary) (?:screen|monitor|display)"),
    ("lock", r"^lock (?:the |my )?(?:screen|pc|computer|machine|workstation|it)$|^lock up$"),
    ("focus", r"(?:switch to|focus(?: on)?|go to) (?:the\s+|my\s+)?(?P<value>.+)"),
]

# Separate list, separate intent, because Windows routes these globally and
# they must not be shadowed by the window patterns above - "pause" is not a
# request to pause a window.
_MEDIA_PATTERNS: list[tuple[str, str]] = [
    ("play_pause", r"^(?:play|pause|resume|unpause)$|"
                   r"(?:play|pause|resume) (?:the )?(?:music|song|track|video|it)"),
    ("next_track", r"^(?:next|skip)$|next (?:song|track|one)|skip (?:this|the|it|song|track)"),
    ("previous_track", r"^(?:previous|back)$|previous (?:song|track|one)|"
                       r"(?:go |play )?(?:back|previous) (?:a )?(?:song|track)|play that again"),
    ("stop_media", r"stop (?:the )?(?:music|song|track|playback|video)"),
]

# Closing can lose unsaved work, so it is its own intent and its own tier -
# the same split as power against control.
_CLOSE_PATTERNS: list[tuple[str, str]] = [
    ("close", r"(?:close|quit|exit|kill) (?:the |my )?(?P<value>[\w .-]+?)(?: window| app)?$"),
]

# Sleep only. Shutdown and restart are deliberately absent: the cost of a false
# positive is unsaved work, and neither is something worth saying out loud
# rather than pressing.
_POWER_PATTERNS: list[tuple[str, str]] = [
    ("sleep", r"^(?:go to sleep|sleep the (?:pc|computer|machine)|"
              r"put (?:the|my) (?:pc|computer|machine) to sleep|"
              r"suspend the (?:pc|computer|machine))$"),
]

_CONTROL = [(kind, re.compile(p)) for kind, p in _CONTROL_PATTERNS]
_POWER = [(kind, re.compile(p)) for kind, p in _POWER_PATTERNS]
_MEDIA = [(kind, re.compile(p)) for kind, p in _MEDIA_PATTERNS]
_CLOSE = [(kind, re.compile(p)) for kind, p in _CLOSE_PATTERNS]

_STRIP = re.compile(r"[.!?,;:]+$")

_SPOKEN = {
    "sleep": "Putting the machine to sleep",
    "lock": "Locking the screen",
}


def _normalise(text: str) -> str:
    return " ".join(_STRIP.sub("", text.strip().lower()).split())


def _match(patterns, text: str) -> Action | None:
    lowered = _normalise(text)
    for kind, pattern in patterns:
        found = pattern.search(lowered)
        if found is None:
            continue
        value = ""
        if "value" in pattern.groupindex:
            value = (found.group("value") or "").strip()
        return Action(kind=kind, value=value)
    return None


def parse_power(text: str) -> Action | None:
    return _match(_POWER, text)


def parse_media(text: str) -> Action | None:
    return _match(_MEDIA, text)


def parse_close(text: str) -> Action | None:
    """Only claims the request when a window with that name is actually open.
    "Close the door" and "quit whining" are not requests to end a process.
    """
    action = _match(_CLOSE, text)
    if action is None or _find_window(action.value) is None:
        return None
    return action


def parse_control(text: str) -> Action | None:
    action = _match(_CONTROL, text)
    if action is None:
        return None
    # Acting on a window that is not open is not a failure worth announcing -
    # it falls through, so "switch to Spotify" can still be handled as a
    # request to open it, or as conversation.
    if action.kind in ("focus", "minimise", "maximise") and _find_window(action.value) is None:
        return None
    return action


def describe_action(action: Action) -> str:  # noqa: D401
    """What the confirmation prompt says. Only reached for CONFIRM tiers, but
    written for every kind so a later tier change never produces "control.
    Should I go ahead?"."""
    if action.kind == "close":
        return f"Closing {action.value}"
    return _SPOKEN.get(action.kind, action.kind.replace("_", " "))


# --- volume -----------------------------------------------------------------


def _endpoint():
    """Fetched per call rather than cached: the default output device changes
    when headphones go in, and a cached endpoint would quietly adjust the
    speakers he is no longer listening to."""
    from ctypes import POINTER, cast

    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

    speakers = AudioUtilities.GetSpeakers()
    interface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(interface, POINTER(IAudioEndpointVolume))


def _volume(action: Action) -> str:
    volume = _endpoint()
    current = round(volume.GetMasterVolumeLevelScalar() * 100)

    if action.kind == "volume_query":
        return "Muted." if volume.GetMute() else f"Volume is at {current} percent."

    if action.kind == "mute":
        volume.SetMute(1, None)
        return "Muted."
    if action.kind == "unmute":
        volume.SetMute(0, None)
        return f"Unmuted, back to {current} percent."

    if action.kind == "volume_set":
        target = int(action.value)
        if target > 100:
            return "That's past the top. It only goes to a hundred."
    elif action.kind == "volume_up":
        target = current + _STEP
    else:
        target = current - _STEP

    target = max(0, min(100, target))
    volume.SetMasterVolumeLevelScalar(target / 100, None)
    # Unmuting on the way up: asking for it louder while muted and getting
    # silence is the wrong answer to what he meant.
    if volume.GetMute() and target > 0:
        volume.SetMute(0, None)
    return f"Volume {target} percent."


def _app_volume(action: Action) -> str:
    """Per-app, because the noisy thing is usually one app rather than the
    machine. pycaw exposes a session per process, so this needs nothing beyond
    what absolute volume already pulled in.
    """
    from pycaw.pycaw import AudioUtilities

    query = action.value.strip().lower()
    mute = action.kind == "app_mute"
    touched = []
    for session in AudioUtilities.GetAllSessions():
        if session.Process is None or session.SimpleAudioVolume is None:
            continue
        name = session.Process.name()
        if query in name.lower() or query in (session.DisplayName or "").lower():
            session.SimpleAudioVolume.SetMute(1 if mute else 0, None)
            touched.append(name.removesuffix(".exe"))

    if not touched:
        return f"Nothing called {action.value} is playing anything."
    what = touched[0] if len(set(touched)) == 1 else f"{len(touched)} {touched[0]} windows"
    return f"{'Muted' if mute else 'Unmuted'} {what}."


def _media(action: Action) -> str:
    """A keystroke, not an app integration. Windows routes the media keys to
    whatever currently holds playback, so this works for Spotify, a browser tab
    or a video player without knowing which one it is.
    """
    key = _VK_MEDIA[action.kind]
    _user32.keybd_event(key, 0, 0, 0)
    _user32.keybd_event(key, 0, _KEYUP, 0)
    # Deliberately no claim about what happened: Windows gives no feedback, and
    # "Playing" when nothing was open would be a lie.
    return ""


_WM_CLOSE = 0x0010


def _close(action: Action) -> str:
    """Asks the window to close, rather than terminating the process. WM_CLOSE
    is what clicking the X sends, so the app still gets to prompt about unsaved
    work - which is the entire reason this is CONFIRM rather than FREE.
    """
    handle = _find_window(action.value)
    if handle is None:
        return f"I can't see a window for {action.value}."
    _user32.PostMessageW(handle, _WM_CLOSE, 0, 0)
    return f"Closed {action.value}."


# --- brightness -------------------------------------------------------------


def _powershell(script: str) -> str:
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=_PS_TIMEOUT_S,
    )
    return result.stdout.strip()


def _brightness(action: Action) -> str:
    current = _powershell(
        "(Get-CimInstance -Namespace root/wmi -ClassName WmiMonitorBrightness "
        "-ErrorAction SilentlyContinue).CurrentBrightness"
    )
    if not current.isdigit():
        return ("This display doesn't expose brightness control - that's a button on the "
                "monitor, not something Windows can set. It only works on built-in panels.")

    level = int(current)
    if action.kind == "brightness_set":
        level = int(action.value)
    elif action.kind == "brightness_up":
        level += _STEP
    else:
        level -= _STEP
    level = max(0, min(100, level))

    _powershell(
        "(Get-CimInstance -Namespace root/wmi -ClassName WmiMonitorBrightnessMethods)"
        f".WmiSetBrightness(1, {level})"
    )
    return f"Brightness {level} percent."


# --- windows ----------------------------------------------------------------


def _windows() -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    callback = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def visit(handle, _param):
        if _user32.IsWindowVisible(handle):
            length = _user32.GetWindowTextLengthW(handle)
            if length:
                buffer = ctypes.create_unicode_buffer(length + 1)
                _user32.GetWindowTextW(handle, buffer, length + 1)
                found.append((handle, buffer.value))
        return True

    _user32.EnumWindows(callback(visit), 0)
    return found


def _find_window(name: str) -> int | None:
    """Substring, shortest title wins - "chrome" should reach the browser, not
    whichever window happens to mention it in a document name."""
    query = _normalise(name)
    if not query:
        return None
    matches = sorted(
        ((h, t) for h, t in _windows() if query in t.lower()), key=lambda m: len(m[1])
    )
    return matches[0][0] if matches else None


def _window_action(action: Action) -> str:
    handle = _find_window(action.value)
    if handle is None:
        return f"I can't see a window for {action.value}."

    if action.kind == "minimise":
        _user32.ShowWindow(handle, _SW_MINIMIZE)
        return f"Minimised {action.value}."
    if action.kind == "maximise":
        _user32.ShowWindow(handle, _SW_MAXIMIZE)
        return f"Maximised {action.value}."

    _user32.ShowWindow(handle, _SW_RESTORE)
    # Windows refuses SetForegroundWindow from a process that did not last
    # handle input. Tapping alt makes this one the input-owning process, which
    # is the standard way round it and the difference between working and
    # silently doing nothing.
    _user32.keybd_event(_VK_MENU, 0, 0, 0)
    _user32.keybd_event(_VK_MENU, 0, _KEYUP, 0)
    if not _user32.SetForegroundWindow(handle):
        return f"Windows wouldn't let me bring {action.value} forward."
    return f"Here's {action.value}."


def _minimise_all() -> str:
    for key, flags in ((_VK_LWIN, 0), (_VK_D, 0), (_VK_D, _KEYUP), (_VK_LWIN, _KEYUP)):
        _user32.keybd_event(key, 0, flags, 0)
    return "Desktop."


# --- displays and power -----------------------------------------------------

_DISPLAY_ARGS = {
    "display_extend": "/extend", "display_clone": "/clone", "display_single": "/internal",
}
_DISPLAY_SAID = {
    "display_extend": "Extended.", "display_clone": "Mirrored.",
    "display_single": "Main screen only.",
}


def _display(action: Action) -> str:
    subprocess.run(["DisplaySwitch.exe", _DISPLAY_ARGS[action.kind]], timeout=_PS_TIMEOUT_S)
    return _DISPLAY_SAID[action.kind]


def perform(action: Action) -> str:
    """Blocking. Called on a thread, because PowerShell and DisplaySwitch take
    seconds and the loop they would stall is the one carrying the microphone.
    """
    if action.kind.startswith("volume") or action.kind in ("mute", "unmute"):
        return _volume(action)
    if action.kind in ("app_mute", "app_unmute"):
        return _app_volume(action)
    if action.kind in _VK_MEDIA:
        return _media(action)
    if action.kind == "close":
        return _close(action)
    if action.kind.startswith("brightness"):
        return _brightness(action)
    if action.kind == "minimise_all":
        return _minimise_all()
    if action.kind in ("focus", "minimise", "maximise"):
        return _window_action(action)
    if action.kind.startswith("display"):
        return _display(action)
    if action.kind == "lock":
        _user32.LockWorkStation()
        return ""  # Nothing worth saying to a locked screen.
    if action.kind == "sleep":
        # SetSuspendState's first argument is hibernate; 0 means sleep.
        subprocess.run(
            ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"], timeout=_PS_TIMEOUT_S
        )
        return ""
    raise ValueError(f"unknown action {action.kind}")


async def apply(action: Action) -> str:
    log.info("Machine control", extra={"extra_fields": {"kind": action.kind, "value": action.value}})
    return await asyncio.to_thread(perform, action)

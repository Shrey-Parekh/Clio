"""What Windows Task Scheduler actually runs when a reminder comes due (6.4).

Started as `pythonw remind.py <root> <port> <id>`, with no import from the voice
stack, so it costs a fraction of a second and needs nothing loaded. It does two
things: asks Clio to say it, if she happens to be running, and shows a toast
either way. The toast is unconditional on purpose - a reminder that didn't reach
him is a failed reminder, so it never depends on the spoken copy working.

The root and port come in on the command line rather than out of the config, so
this file can run with nothing of Clio's importable but websockets.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import subprocess
import sys
from pathlib import Path

_CONNECT_TIMEOUT_S = 2.0
_TITLE = "Clio"
_LOST = "You set a reminder for now, but I've lost the note saying what it was."

# PowerShell's own registered app id - a toast needs one, and this avoids
# registering anything of ours.
_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
_TOAST_PS = """
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
    [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$text = $template.GetElementsByTagName('text')
$text[0].AppendChild($template.CreateTextNode('{title}')) | Out-Null
$text[1].AppendChild($template.CreateTextNode('{body}')) | Out-Null
$toast = [Windows.UI.Notifications.ToastNotification]::new($template)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{app}').Show($toast)
"""


def toast(title: str, text: str) -> None:
    """A Windows toast, falling back to a message box. One of the two always
    appears: silence here would lose the reminder entirely."""
    script = _TOAST_PS.format(title=_ps_quote(title), body=_ps_quote(text), app=_APP_ID)
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode == 0:
            return
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        pass
    # 0x00001000 = system modal, so it comes to the front of whatever is open.
    ctypes.windll.user32.MessageBoxW(None, text, title, 0x00001000 | 0x00000040)


async def announce(text: str, port: int) -> bool:
    """Ask a running Clio to speak it. False if she isn't running, which is not
    an error - it is the ordinary case at seven in the morning."""
    try:
        import websockets
    except ImportError:
        return False
    try:
        async with asyncio.timeout(_CONNECT_TIMEOUT_S):
            async with websockets.connect(f"ws://127.0.0.1:{port}") as connection:
                await connection.send(json.dumps({"cmd": "announce", "text": text}))
        return True
    except Exception:
        return False


async def fire(reminder_id: str, root: Path | str, port: int) -> bool:
    """Returns whether Clio was there to say it. The toast happens regardless."""
    sidecar = Path(root) / "reminders" / f"{reminder_id}.txt"
    try:
        text = sidecar.read_text(encoding="utf-8").strip()
    except OSError:
        text = ""

    spoken = await announce(text, port) if text else False
    toast(_TITLE, text or _LOST)
    return spoken


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: remind.py <memory root> <core port> <reminder id>", file=sys.stderr)
        return 2
    root, port, reminder_id = argv[0], int(argv[1]), argv[2]
    asyncio.run(fire(reminder_id, root, port))
    return 0


def _ps_quote(text: str) -> str:
    # Into a single-quoted PowerShell string: only the quote itself needs doubling.
    return " ".join(text.split()).replace("'", "''")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

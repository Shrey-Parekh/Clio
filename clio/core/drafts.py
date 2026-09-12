"""Saving a draft into Gmail (6.7). The only write Clio makes to a mailbox.

Deliberately not in `mailbox.py`. That module has no write in it, and a test
asserts so; keeping the append here makes the boundary exact rather than a
promise: **reading cannot write, and drafting can add a draft and nothing
else.**

There is no delete here, and no flag change. A draft Clio saved is removed by
him, in Gmail, like any other. That costs something visible - editing after a
save leaves the older draft behind - and it buys a code path that cannot lose
his mail.
"""

from __future__ import annotations

import imaplib
import re
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid

from clio.core.logging import get_logger
from clio.core.mailbox import HOST, PORT, _TIMEOUT_S, credentials

log = get_logger("clio.drafts")

# Gmail names this folder in the account's own language, so it is asked for
# rather than assumed; this is only the fallback.
_FALLBACK = '"[Gmail]/Drafts"'
_DRAFTS_FLAG = re.compile(rb"\\Drafts")


def build(sender: str, to: str, subject: str, body: str,
          in_reply_to: str = "", references: str = "") -> EmailMessage:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    message["Subject"] = subject
    message["Date"] = format_datetime(datetime.now(timezone.utc))
    message["Message-ID"] = make_msgid()
    if in_reply_to:
        # Both headers, so Gmail hangs the reply on the existing conversation
        # instead of starting a second one beside it.
        message["In-Reply-To"] = in_reply_to
        message["References"] = f"{references} {in_reply_to}".strip()
    message.set_content(body)
    return message


def save(to: str, subject: str, body: str,
         in_reply_to: str = "", references: str = "") -> str:
    """Append one draft. Returns the folder it went into, for the log."""
    sender, password = credentials()
    message = build(sender, to, subject, body, in_reply_to, references)
    imap = imaplib.IMAP4_SSL(HOST, PORT, timeout=_TIMEOUT_S)
    try:
        imap.login(sender, password)
        folder = _drafts_folder(imap)
        status, _ = imap.append(
            folder, r"\Draft", imaplib.Time2Internaldate(time.time()), message.as_bytes()
        )
        if status != "OK":
            raise OSError(f"Gmail would not keep the draft ({status})")
        log.info("Draft saved", extra={"extra_fields": {"folder": folder}})
        return folder
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def _drafts_folder(imap) -> str:
    """The folder Gmail itself flags as \\Drafts. An append to a guessed English
    name fails in a way that reads like a bug rather than a language setting."""
    status, folders = imap.list()
    if status == "OK":
        for line in folders or []:
            raw = line if isinstance(line, bytes) else str(line).encode()
            if _DRAFTS_FLAG.search(raw):
                # LIST gives: (\HasNoChildren \Drafts) "/" "[Gmail]/Drafts"
                name = raw.decode("utf-8", "replace").rsplit(' "/" ', 1)[-1].strip()
                if name:
                    return name
    return _FALLBACK

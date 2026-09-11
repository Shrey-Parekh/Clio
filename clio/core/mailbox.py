"""Reading Gmail over IMAP (6.6). The connection, and nothing about speech.

Standard library only: `imaplib` connects, `email` parses. The alternative was
the Gmail API, which enforces read-only at Google's end but costs a Cloud
project, two dependencies and a sign-in that expires weekly - see the design
note for the trade.

**Read-only by construction.** There is no function here that sets a flag,
appends or deletes, so nothing the capability does can change the mailbox, and
every fetch uses BODY.PEEK, so asking what is unread never marks it read. An app
password could do all of those things; this module is where that power is
declined.

Every call opens a connection and closes it. A PC that slept for six hours never
leaves a dead socket behind, at the cost of about a second per request.
"""

from __future__ import annotations

import imaplib
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from email import message_from_bytes
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime

from clio.core.config import Config
from clio.core.logging import get_logger

log = get_logger("clio.mailbox")

HOST = "imap.gmail.com"
PORT = 993
_TIMEOUT_S = 15.0
# Headers plus the start of the first body part, in one fetch. A whole message
# runs to megabytes and only its opening is ever used.
_PREVIEW_BYTES = 8192
_MESSAGE_BYTES = 32768

_TAGS = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")
# A Gmail search is built from words he said, so everything outside this set is
# dropped: a stray quote would otherwise end the search string and the rest of
# the sentence would arrive as IMAP commands. ASCII only - imaplib sends ASCII.
_SAFE_QUERY = re.compile(r"[^A-Za-z0-9 @.:+_-]")


@dataclass(frozen=True)
class Message:
    uid: str
    sender: str              # display name, or the address when there is no name
    address: str             # the bare address, lowercased, for rule matching
    subject: str
    received: datetime | None  # None when the Date header is missing or unparseable
    extract: str             # opening of the body, already cut to size


def credentials() -> tuple[str, str]:
    """Read per call, so a key added to .env works after a restart with nothing
    else changing. Spaces are stripped: Google shows an app password in four
    groups of four, so it is pasted with them more often than not, and a login
    failing over an invisible space is a miserable thing to debug."""
    address = Config.secret("GMAIL_ADDRESS").strip()
    password = Config.secret("GMAIL_APP_PASSWORD").replace(" ", "").strip()
    return address, password


def scope_query(window_days: int, category: str) -> str:
    """What "unread" means here: recent, and in the tab that matters. His inbox
    holds ten thousand unread messages, so the lifetime number is a fact about
    the past rather than something he can act on."""
    parts = ["is:unread"]
    if window_days > 0:
        parts.append(f"newer_than:{int(window_days)}d")
    if category:
        parts.append(f"category:{_SAFE_QUERY.sub('', category)}")
    return " ".join(parts)


@contextmanager
def _inbox():
    address, password = credentials()
    imap = imaplib.IMAP4_SSL(HOST, PORT, timeout=_TIMEOUT_S)
    try:
        imap.login(address, password)
        # readonly: even SELECT can clear flags on a mailbox opened for writing.
        imap.select("INBOX", readonly=True)
        yield imap
    finally:
        try:
            imap.logout()
        except Exception:
            # A failed logout is a closed socket, which is what was wanted.
            pass


def _uids(imap, query: str) -> list[bytes]:
    status, data = imap.uid("search", None, "X-GM-RAW", f'"{query}"')
    if status != "OK" or not data or not data[0]:
        return []
    return data[0].split()


def unread_count(window_days: int = 2, category: str = "primary") -> int:
    with _inbox() as imap:
        return len(_uids(imap, scope_query(window_days, category)))


def total_unread() -> int:
    """Every unread message ever, for "how many in total". Never the headline."""
    with _inbox() as imap:
        status, data = imap.uid("search", None, "UNSEEN")
        if status != "OK" or not data or not data[0]:
            return 0
        return len(data[0].split())


def unread(limit: int = 25, window_days: int = 2, category: str = "primary",
           extract_chars: int = 500) -> list[Message]:
    """Newest first, so "the first one" is the one that just arrived."""
    with _inbox() as imap:
        uids = _uids(imap, scope_query(window_days, category))[-limit:]
        found = [_preview(imap, uid, extract_chars) for uid in reversed(uids)]
        return [m for m in found if m is not None]


def search(query: str, limit: int = 10, extract_chars: int = 500) -> list[Message]:
    """Gmail's own search syntax, e.g. "from:priya newer_than:30d"."""
    cleaned = _SAFE_QUERY.sub(" ", query).strip()[:100]
    if not cleaned:
        return []
    with _inbox() as imap:
        uids = _uids(imap, cleaned)[-limit:]
        found = [_preview(imap, uid, extract_chars) for uid in reversed(uids)]
        return [m for m in found if m is not None]


def fetch(uid: str, extract_chars: int = 500) -> Message | None:
    """One message, read further in, for a summary."""
    with _inbox() as imap:
        return _preview(imap, uid.encode(), extract_chars, _MESSAGE_BYTES)


def _preview(imap, uid: bytes, extract_chars: int,
             byte_cap: int = _PREVIEW_BYTES) -> Message | None:
    status, data = imap.uid("fetch", uid, f"(BODY.PEEK[]<0.{byte_cap}>)")
    if status != "OK":
        return None
    raw = next((part[1] for part in data if isinstance(part, tuple)), b"")
    if not raw:
        return None
    try:
        parsed = message_from_bytes(raw)
    except Exception:
        # One unreadable message is skipped, never raised: an inbox is full of
        # things no parser was expecting.
        log.warning("Unreadable message skipped")
        return None
    name, address = _sender(parsed.get("From", ""))
    return Message(
        uid=uid.decode(),
        sender=name,
        address=address,
        subject=_decoded(parsed.get("Subject", "")) or "(no subject)",
        received=_received(parsed.get("Date", "")),
        extract=_extract(parsed, extract_chars),
    )


def _decoded(value: str) -> str:
    """Real subjects arrive MIME-encoded: =?UTF-8?B?...?= is common."""
    try:
        return _SPACE.sub(" ", str(make_header(decode_header(value)))).strip()
    except Exception:
        return value.strip()


def _sender(value: str) -> tuple[str, str]:
    name, address = parseaddr(value)
    address = address.strip().lower()
    return _decoded(name) or address, address


def _received(value: str) -> datetime | None:
    try:
        return parsedate_to_datetime(value)
    except Exception:
        return None


def _extract(parsed, limit: int) -> str:
    """The opening of the body as plain text. The fetch above is truncated
    mid-message, so a part that will not decode is ordinary here, not a fault."""
    for part in parsed.walk():
        kind = part.get_content_type()
        if kind not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_payload(decode=True) or b""
        except Exception:
            continue
        body = payload.decode(part.get_content_charset() or "utf-8", "replace")
        if kind == "text/html":
            body = _TAGS.sub(" ", body)
        text = _SPACE.sub(" ", body).strip()
        if text:
            return text[:limit]
    return ""

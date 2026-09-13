"""Sending email (6.8). The one thing Clio does that cannot be undone.

Third of three mail modules, and the pattern holds: `mailbox.py` reads,
`drafts.py` appends a draft, this sends. None can do another's job, and none can
delete.

The MIME comes from `drafts.build`, the same function the saved draft uses, so
what leaves is byte-for-byte what he reviewed. A second builder here would be a
second thing to keep in step, and the one that drifted would be the one that
sends.

Nothing in this module decides *whether* to send. That is the capability's job,
behind a readback, an explicit yes and a ten-second hold.
"""

from __future__ import annotations

import smtplib

from clio.core.drafts import build
from clio.core.logging import get_logger
from clio.core.mailbox import credentials

log = get_logger("clio.sender")

HOST = "smtp.gmail.com"
PORT = 465          # implicit SSL, so there is no plaintext moment to get wrong
_TIMEOUT_S = 20.0


def send(to: str, subject: str, body: str,
         in_reply_to: str = "", references: str = "") -> None:
    sender, password = credentials()
    message = build(sender, to, subject, body, in_reply_to, references)
    with smtplib.SMTP_SSL(HOST, PORT, timeout=_TIMEOUT_S) as smtp:
        smtp.login(sender, password)
        smtp.send_message(message)
    # Logged after the fact as well as before: the capability writes the "about
    # to" line, this one means it actually left.
    log.info("Mail sent", extra={"extra_fields": {"to": to, "subject": subject}})

"""Sending email (6.8) - the only thing in Clio that cannot be taken back.

Nothing here talks to SMTP. What is worth testing is not the sending, which is
six lines of standard library, but everything that has to be true first: that a
send is refused before he is ever asked to confirm it, that the hold really
holds, that "stop" stops it, and that a confirmation typed in the chat window is
a confirmation rather than a silent no.

Run: python tests/test_send.py
"""

import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clio.capabilities import draft as draft_mod  # noqa: E402
from clio.capabilities.draft import (  # noqa: E402
    Draft, DraftCapability, SendRequest, parse_send_request,
)
from clio.core import sender  # noqa: E402
from clio.core.config import EmailConfig  # noqa: E402
from clio.core.permissions import Permission  # noqa: E402
from clio.llm.memory import ConversationMemory  # noqa: E402
from clio.orchestrator import Orchestrator  # noqa: E402

# Quiet hours off for most of this file, so the suite does not pass or fail
# depending on what time it is run. They get their own section, with the clock
# handed in.
SETTINGS = EmailConfig(signature="Shrey", quiet_start=0, quiet_end=0)
QUIET = EmailConfig(signature="Shrey", quiet_start=23, quiet_end=7)


def a_draft(**changes):
    values = dict(to="priya@example.com", to_name="Priya", subject="Re: Friday plan",
                  body="Sorry, I'll be late on Friday. Shrey",
                  in_reply_to="<friday-1@mail.example>", references="<friday-0@mail.example>",
                  from_message=True)
    values.update(changes)
    return Draft(**values)


class FakeSMTP:
    def __init__(self):
        self.sent = []

    def __call__(self, to, subject, body, in_reply_to="", references=""):
        self.sent.append({"to": to, "subject": subject, "body": body,
                          "in_reply_to": in_reply_to, "references": references})


class FakeLLM:
    async def complete(self, messages, tier="default"):
        return "Written."


async def main():
    said = []

    async def announce(text):
        said.append(text)

    smtp = FakeSMTP()
    sender.send = smtp

    # --- what counts as a send, and what doesn't ---

    assert parse_send_request("send it") == SendRequest()
    assert parse_send_request("send it anyway").override is True
    assert parse_send_request("send that now").override is True
    for text in ["send", "send it to priya", "send me the file", "sent it"]:
        assert parse_send_request(text) is None, text
    print("OK  'send it' and nothing looser; a spoken address is not a way in")

    # --- the guards, all before he is asked anything ---

    capability = DraftCapability(SETTINGS, None, announce)
    assert capability.prepare_send().refusal == "There's no draft to send."

    capability._draft = a_draft(body="   ")
    assert "nothing in it yet" in capability.prepare_send().refusal

    # The address has to have come off a real message in his mailbox.
    capability._draft = a_draft(from_message=False)
    assert "already got mail from" in capability.prepare_send().refusal

    capability._draft = a_draft()
    prepared = capability.prepare_send()
    assert prepared.refusal == ""
    assert "Sending to Priya" in prepared.readback
    assert "priya at example dot com" in prepared.readback, prepared.readback
    assert "Re: Friday plan" in prepared.readback and "7 words" in prepared.readback
    assert smtp.sent == [], "preparing to send must never send"
    print(f"OK  readback: {prepared.readback}")

    # --- quiet hours, and the override he can actually use ---

    quiet = DraftCapability(QUIET, None, announce)
    quiet._draft = a_draft()
    for hour in (23, 2, 6):
        refusal = quiet.prepare_send(now=datetime(2026, 9, 14, hour, 30)).refusal
        assert "quiet hours" in refusal, (hour, refusal)
    assert quiet.prepare_send(now=datetime(2026, 9, 14, 15, 0)).refusal == ""
    # "Send it anyway" is the whole point of having an overridable rule.
    assert quiet.prepare_send(override=True, now=datetime(2026, 9, 14, 23, 30)).refusal == ""
    # A window that doesn't wrap midnight still behaves.
    day = DraftCapability(EmailConfig(quiet_start=1, quiet_end=2), None)
    assert day._quiet_now(datetime(2026, 9, 14, 1, 30)) is True
    assert day._quiet_now(datetime(2026, 9, 14, 5, 0)) is False
    assert smtp.sent == [], "none of that may have sent anything"
    print("OK  quiet hours refuse, 'send it anyway' overrides, and the window wraps midnight")

    # --- the hold is real, and stop stops it ---

    draft_mod._HOLD_S = 0.3
    spoken = await capability.arm_send()
    assert "Say cancel" in spoken, spoken
    await asyncio.sleep(0.1)
    assert smtp.sent == [], "nothing may leave during the hold"
    assert capability.cancel_send() == "Stopped it. Nothing's gone."
    await asyncio.sleep(0.4)
    assert smtp.sent == [], "a cancelled send must never arrive late"
    assert capability.has_draft(), "cancelling keeps the draft"
    assert capability.cancel_send() == "", "nothing armed, nothing to cancel"

    # --- and when it is left alone, it goes exactly once ---

    await capability.arm_send()
    await asyncio.sleep(0.5)
    assert len(smtp.sent) == 1, smtp.sent
    assert smtp.sent[0]["to"] == "priya@example.com"
    assert smtp.sent[0]["in_reply_to"] == "<friday-1@mail.example>", "a reply stays in its thread"
    assert smtp.sent[0]["body"].endswith("Shrey")
    assert said == ["Sent it to Priya."], said
    assert not capability.has_draft(), "the draft is cleared, so 'send it' can't send it twice"
    assert await capability.arm_send() == "There's no draft to send."
    assert len(smtp.sent) == 1
    print("OK  the hold holds, stop cancels, and a sent draft cannot be sent again")

    # --- a failed send says so and keeps the draft ---

    def refuse(*_args, **_kwargs):
        raise OSError("smtp is down")

    sender.send = refuse
    capability._draft = a_draft()
    said.clear()
    await capability.arm_send()
    await asyncio.sleep(0.5)
    assert said and "can't reach Gmail" in said[0], said
    assert capability.has_draft(), "a failed send must not lose what he wrote"
    sender.send = smtp
    print("OK  a failed send is spoken, and the draft survives it")

    # --- through the real router ---

    orchestrator = Orchestrator(
        wake_detector=None, turn_detector=None, stt=None, llm=FakeLLM(), speaker=None,
        persona_system_prompt="p", follow_up_window_s=1.0, email=SETTINGS,
    )
    orchestrator._memory = ConversationMemory(provider=FakeLLM(), system_prompt="p")

    caps = {c.name: c for c in orchestrator._router.capabilities()}
    assert caps["send"].permission is Permission.CONFIRM, "sending is the one thing that asks"
    assert caps["send_blocked"].permission is Permission.FREE

    # With no draft, "send it" is the refusal, and never reaches the gate.
    matched = orchestrator._router.match("send it")
    assert matched is not None and matched.intent == "send_blocked", matched

    orchestrator._draft._draft = a_draft()
    orchestrator._draft._announce = announce
    matched = orchestrator._router.match("send it")
    assert matched is not None and matched.intent == "send", matched
    assert "Sending to Priya" in matched.description, matched.description
    print("OK  refused sends never reach the gate; a real one carries its readback")

    # --- a typed confirmation is a confirmation ---

    smtp.sent.clear()
    said.clear()
    reply = await orchestrator.inject_text("send it")
    assert "Say yes" in reply, reply
    assert smtp.sent == [], "typing 'send it' must not send anything on its own"

    reply = await orchestrator.inject_text("yes")
    assert "Say cancel" in reply, reply
    await asyncio.sleep(0.5)
    assert len(smtp.sent) == 1 and smtp.sent[0]["to"] == "priya@example.com"
    print("OK  typed confirm-tier actions wait for a typed yes, then run")

    # Anything that is not a yes drops it, and the action does not run later.
    orchestrator._draft._draft = a_draft()
    smtp.sent.clear()
    await orchestrator.inject_text("send it")
    await orchestrator.inject_text("actually what's the time")
    await asyncio.sleep(0.5)
    assert smtp.sent == [], "a pending send must not survive a change of subject"
    await orchestrator.inject_text("yes")
    await asyncio.sleep(0.5)
    assert smtp.sent == [], "a later yes is answering some other question"
    print("OK  a pending confirmation is dropped by anything that isn't a yes")

    # --- sending added no way to remove anything ---

    # The module's prose talks about deleting; what matters is that it has no
    # function that does, and touches no mailbox it could delete from.
    public = [name for name in dir(sender) if not name.startswith("_")
              and callable(getattr(sender, name))]
    assert "send" in public
    for forbidden in ("delete", "expunge", "remove", "trash", "append"):
        assert forbidden not in public, forbidden
    code = "\n".join(line for line in
                     Path("clio/core/sender.py").read_text(encoding="utf-8").splitlines()
                     if not line.strip().startswith("#"))
    assert "imaplib" not in code, "the sender has no business opening a mailbox"
    print("OK  the sender can send, and nothing else")

    print("\nAll sending checks passed.")


if __name__ == "__main__":
    asyncio.run(main())

# 6.8 — Email, send

Design, 2026-09-13.

## The problem

Everything Clio does so far can be undone. A note can be deleted, a task
un-ticked, a reminder cancelled, a draft binned. Sending mail cannot: the moment
it leaves, it is in someone else's inbox, and no amount of good behaviour
afterwards takes it back.

So 6.8 is not really a transport problem - `smtplib` is thirty lines. It is a
question of what has to be true before those thirty lines run.

## The decision

**SMTP over the same app password, and sending only ever from a draft he has
already seen.** `smtp.gmail.com:465` with SSL, from the standard library, using
the credential 6.6 already reads. Nothing new is installed and nothing new is
authorised.

Four rules decided by him, and the reason each one exists:

1. **Only an open draft can be sent.** "Email Priya that the meeting moved to 4"
   produces a draft, not a send. There is no sentence he can say once that puts
   mail in someone's inbox - it always takes two turns, with the full text
   visible in between.
2. **Readback, an explicit yes, then a ten-second hold.** Gmail's undo-send does
   not exist over SMTP, so the hold is the only undo there is. Saying "cancel"
   or "stop" during it stops the send.
3. **Only people he has already exchanged mail with.** The address comes from a
   real message in his mailbox, carried on the draft since 6.7. An address
   spelled out loud is never accepted; a genuinely new recipient is typed by him
   in the chat window.
4. **Nothing sends between 11pm and 7am** (`quiet_hours`, configurable). Not a
   security control - a decency one. A stray send at 3am is the least likely to
   be noticed and the most likely to be regretted.

**No daily cap, deliberately.** It was offered and he declined it, and on
reflection the confirmation makes it redundant: every single send needs a
separate explicit yes, so there is no path by which a loop or a bug sends twice,
let alone a hundred times. A cap would be guarding a door that is already
locked.

## The gap this exposes

`Orchestrator._confirm` builds a `ConversationSession` and waits for the next
spoken turn. From the chat window there are no frames and no speaker, so it
returns `False`. **Every confirm-tier action typed into the chat window is
silently declined today** - closing a window, locking the screen, and now
sending.

That has to be fixed here rather than worked around, because a confirmation that
only exists in one input path is not a confirmation, it is a coincidence.

**The fix:** a pending confirmation on the orchestrator.

- When the turn came from the chat window, `_confirm` does not listen. It stores
  the matched action as pending, with a 60-second expiry, and returns `False`
  with a reply that says exactly what it wants: *"Say yes and I'll send it."*
- The next typed message is checked against `is_affirmative` before routing. A
  yes runs the pending action; anything else drops it and is handled normally.
- Voice is untouched: frames exist, so `_confirm` behaves as it does now.

This is about fifteen lines, and it repairs confirmation for every capability,
not just sending.

## Components

### `clio/core/sender.py` — the send

The third mail module, and the pattern holds: `mailbox.py` reads, `drafts.py`
appends a draft, `sender.py` sends. None of them can do each other's job, and
none of them can delete.

```python
def send(to: str, subject: str, body: str,
         in_reply_to: str = "", references: str = "") -> None
```

It reuses `drafts.build` for the MIME, so a sent message is byte-for-byte the
draft that was reviewed - no second code path that could compose something
different from what he approved.

Gmail's SMTP is expected to file a copy in Sent automatically. **That gets
verified live rather than assumed**; if it does not, the same message is
appended to the Sent folder with `\Seen`, reusing the drafts module's append.

### Sending, in `clio/capabilities/draft.py`

The draft capability already owns the open draft, so sending is one more kind on
it rather than a new place where drafts can come from.

- `send` is registered as its own intent at **`Permission.CONFIRM`**, with a
  `describe` that produces the readback: *"Sending to Priya, priya at example
  dot com, subject Re: Friday plan, 22 words."* The permission gate speaks that
  sentence, so the confirmation and the readback are the same thing rather than
  two questions in a row.
- After the yes, the send is **armed, not performed**: an `asyncio` task that
  waits ten seconds. She says "sending in ten seconds - say cancel to stop."
- **Cancelling reuses `stop`.** The existing stop capability cancels the armed
  send, which is what "stop" already means everywhere else in the app.
- When it goes: *"Sent it to Priya."* The draft is cleared, so a second "send
  it" has nothing to send rather than sending twice.

### Guards, in order

Checked before the readback, so he is never asked to confirm something that was
going to be refused anyway:

| Check | If it fails |
|---|---|
| A draft is open | "There's no draft to send." |
| Body is not empty | "There's nothing in it yet." |
| Recipient came from a real message | "I only send to people you've already got mail from. Type the address in the window if it's someone new." |
| Not quiet hours | "It's gone eleven. I'll send it in the morning, or say send it anyway." |

Quiet hours are overridable in the same breath ("send it anyway"), because a
rule he cannot override is a rule he will disable.

### Logging

Every send writes one line: recipient, subject, word count, and whether it was a
reply. **Never the body.** If mail ever goes somewhere he did not expect, that
log is the only record of what happened, so it is written before the send, not
after.

## Testing

`tests/test_send.py`, plain asserts, no network. A fake SMTP that records what it
was handed.

1. `send` at CONFIRM, and the readback names the recipient and subject.
2. A declined confirmation sends nothing and keeps the draft.
3. Nothing is sent during the ten-second hold; `stop` inside the window cancels
   it and the draft survives.
4. Quiet hours refuse, and "send it anyway" overrides.
5. A draft whose recipient did not come from a real message is refused before
   any confirmation.
6. The sent MIME is identical to the draft's, with the threading headers.
7. After sending, the draft is cleared and a second "send it" sends nothing.
8. The pending-confirmation fix: a typed confirm-tier action is armed rather than
   declined, a typed "yes" runs it, and anything else drops it.
9. `sender.py` contains no delete and no flag change, like its two siblings.

## Out of scope

Attachments, CC and BCC, HTML, scheduled sending, sending to anyone not already
in his mailbox, and any automatic send - including a rule, a routine or a chain
step. 2.7's rule stands: a chain cannot get a confirm-tier action past its
confirmation.

## What he has to do

Nothing new. The app password from 6.6 covers SMTP as well.

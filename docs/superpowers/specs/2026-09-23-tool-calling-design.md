# 7.6 — Tool calling, rebuilt

Design, 2026-09-23. Approved and built the same day.

## The problem

Every capability matches a phrase. "Can you get the ewaste thing going" is
plainly an order and matches nothing, so it fell to conversation and she talked
about doing it instead of doing it.

## The decision

**The model rewords; the router decides.** When nothing matched and the
sentence starts like an order, the fast model gets the request and one example
sentence per capability, and returns the same request said the way Clio
already understands - or `NONE`. That sentence goes through the ordinary router.

Considered and rejected:

- **Classic tool calling** - a JSON schema per capability and a validator.
  Around 40 schemas duplicating the parsers, which is what 1.7 built and
  removed because nothing used it.
- **Folding it into 7.7.** 7.7 needs this piece anyway.

What follows from rewording:

- The parsers remain the argument validation. A made-up answer matches nothing
  and falls to conversation.
- The permission table stays the only authority. A BLOCKED intent is still
  refused.
- **Every guess is read back and waits for a yes, even a harmless one** (his
  choice): "You mean: set a timer for 10 minutes. Should I go ahead?" When the
  intent can describe itself, the readback says what will actually happen too.
- **Deleting, moving, sending, powering off, "again" and "stop" are never
  offered.** Those should only ever come from his own words.
- Questions and remarks never cost the extra call: only sentences opening with
  an action verb ("run", "start", "fire up", "put on") are reworded.
- Any failure - network, fallback down, rubbish answer - is conversation, as
  before 7.6.

## Found live

- With no projects registered, "run the ewaste project" was claimed by `open`,
  which knew nothing by that name, and read back as if it would run. `open` now
  has a describer, and a guess that opens nothing is not offered.
- Groq dropped the connection on two calls during testing; both fell back to
  conversation as designed.

## Testing

`tests/test_rephrase.py`, model faked. Every example sentence matches its own
intent on the real router, so the examples can't drift from the parsers.
Around it: the action-verb gate, cleaning the answer, guesses read back as
CONFIRM, deletes/nonsense/failures falling to chat, a typed guess waiting for
a yes with nothing run, and known phrases never reaching the model.

## Out of scope

Chains ("pull ewaste and start training") are 7.7. A guess is one step.

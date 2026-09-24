# 7.7 — Supervised multi-step tasks

Design, 2026-09-24. Approved and built the same day.

## The problem

"Pull the latest ewaste code, install its requirements and start training." The
router already runs a chain when every part matches a phrase by itself (2.7).
This sentence has no part that does.

## The decision

Built on 7.6: the model writes, the router decides.

1. **The reasoning model writes the plan** as one sentence per step, in the
   shapes Clio already knows (his choice: better ordering than the fast model).
2. **Every step must match on the ordinary router, or nothing runs.** A step
   she can't match is named in the refusal. An unregistered project gets the
   fix: "say register ewaste first".
3. **Read back once, one yes covers it** (his choice): "Three steps. One,
   Running git pull, in ewaste. Two, ..." It is one CONFIRM action through the
   ordinary gate, so voice and typed turns both work. A step that destroys
   something (`git reset --hard`, `pip uninstall`) still asks on its own.
4. **A slow command is waited on** (his choice), because the next step usually
   needs it finished. That applies up to the budget. Starting a project is
   never waited on: it is handed to the job runner.
5. **A failure stops the chain**, and the reply says which step failed and how
   many were not done.
6. **Limits:** 6 steps, and 15 minutes of active work (per the brief).
7. **Progress** goes to the chat window and HUD as "Step 2 of 3: ..." through
   the existing transcript event.

Unchanged from 7.6: deleting, moving, sending and power are never offered,
BLOCKED stays blocked, and any model failure is conversation.

## Found live

- Asked for "pull the latest ewaste code", the model answered NONE: no example
  showed that git or pip commands can be said at all. Both prompts now describe
  `run <command> in <folder>`.
- **On one run in three**, "start training" became `run python train.py in
  ewaste`, a script name nobody had said. That would get round 7.2's rule that
  a project's command comes from his config. The prompt says not to invent
  names, but a prompt is not a guarantee. So the code now refuses any file in a
  guessed command whose name he did not say. "Requirements" allows
  `requirements.txt`, and "training" does not allow `train.py`.
- When the planner gives up on a sentence with several parts, it does not fall
  back to 7.6's single guess. That would quietly do half of what he asked.

## Testing

`tests/test_plans.py`: the model is faked, and the steps are real processes in
a temp project. It checks:

- the whole readback, with nothing run before the yes;
- steps run in order, and one waits for a slow step to finish (it checks for
  that step's file);
- progress events;
- a failure stops the chain;
- an unknown step, too many steps, an invented script name and an unregistered
  project are each refused with nothing run;
- a one-step plan becomes a 7.6 guess;
- an uninstall step still asks on its own;
- the time budget.

## Known limits

- Whether a step succeeded is read from its log (`jobs.explain`), because
  detached jobs have no exit code. A failure that prints nothing recognisable
  counts as success.
- Only shell steps are checked for failure. Other capabilities return a sentence,
  not a status.

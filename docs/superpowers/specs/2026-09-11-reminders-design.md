# 6.4 — Alarms, reminders and scheduling

Design, 2026-09-11.

## The problem

Clio can already set a timer. A timer lives inside her process: `TimerCapability`
holds an `asyncio.Task` per timer, and when she stops, every pending timer stops
with her. That is correct for "ten minutes for the pasta" and useless for
"remind me to take my meds at seven tomorrow", which has to survive a restart,
a crash and an overnight shutdown.

So 6.4 needs something that remembers a time when Clio does not exist.

## The decision

**Windows Task Scheduler is both the clock and the list.** Each reminder becomes
one scheduled task named `Clio-Reminder-<id>`. Windows fires it, whether or not
Clio is running, and whether or not the machine has been rebooted since it was
set. Asking what reminders exist means asking Windows. Cancelling one means
deleting its task.

Two alternatives were considered and rejected:

- **Clio owns everything** — a `reminders.json` she reloads on start. Simple, but
  the countdown only exists while she runs, so a reminder at 7am with the PC off
  fires at whatever time she is next started. That is a late notification, not an
  alarm, and it fails the "surviving restarts" requirement in the roadmap.
- **A JSON file mirrored into Task Scheduler** — two lists of the same thing,
  which drift the first time either side is edited or a write fails. Every later
  bug begins with "which one is right?".

Windows already solves durable scheduling, exposes it in a UI the user can
inspect, and needs no code from us to survive a reboot. Using it means writing no
store at all.

### What fires when Clio is not running

Chosen behaviour: **a Windows toast always; spoken as well when she is running.**
The reminder always reaches him, and a reminder never boots the voice stack (with
its GPU and microphone) on its own.

## Components

### `clio/capabilities/remind.py` — parsing and phrasing

Deterministic, no model call, matching the other capabilities.

Setting:

- `remind me to take my meds at 7am`
- `remind me to call mum in 20 minutes`
- `remind me at 7:30 tomorrow to leave`
- `remind me on Monday at 9 to send the invoice`
- `every day at 7am remind me to stretch`
- `every weekday at 8 remind me to log in`
- `every Monday at 6pm remind me to take the bins out`

Asking and cancelling:

- `what reminders do I have` / `what's coming up`
- `cancel my 7am reminder` / `cancel all my reminders`

Separation from timers is the existing rule: `parse_timer_command` requires the
literal word "timer", and a reminder always carries text to say. "Set a timer for
ten minutes" stays a timer; "remind me to check the oven in ten minutes" is a
reminder.

Task Scheduler's finest granularity is one minute. Anything under a minute is
answered with "that's a timer, not a reminder" and set as a timer instead.

### `clio/capabilities/schedule.py` — the Task Scheduler wrapper

The only module that knows `schtasks` exists. Its subprocess runner is injected
so every test runs against a fake.

- **create** — `schtasks /create /tn Clio-Reminder-<id> /tr "<fire command>" /sc once /st HH:MM /sd DD/MM/YYYY`,
  or `/sc daily`, `/sc weekly /d MON`, `/sc weekly /d MON,TUE,WED,THU,FRI` for the
  recurring shapes.
- **list** — `schtasks /query /fo csv /v`, filtered to the `Clio-Reminder-` prefix.
  The verbose CSV carries Next Run Time, so no schedule state is stored anywhere
  else.
- **delete** — `schtasks /delete /tn <name> /f`.

### The reminder text

`schtasks /tr` caps its command at 261 characters and its quote escaping is
hostile to arbitrary text, so the spoken text is not passed on the command line.
It is written to `<memory root>/reminders/<id>.txt` and the command passes only
the id.

This is not the mirrored-store problem rejected above. The sidecar holds no time
and no schedule, and is never read to decide what fires or when: Task Scheduler
remains the sole answer to "what reminders exist". The sidecar is only a payload.
A missing one still fires, saying "you set a reminder for now, but I've lost what
it said."

The directory sits under the memory root beside `notes.md`, so the files are
findable by the existing file search and readable in any text editor with no Clio
running.

### `clio/remind.py` — what Windows actually runs

A standalone module, started as `pythonw -m clio.remind <id>`. It imports no part
of the voice stack, so it starts in well under a second.

1. Read `<id>.txt` for the text.
2. Connect to `ws://127.0.0.1:<core port>` with a short timeout and send
   `{"cmd": "announce", "text": ...}`. If Clio is running, she speaks it.
3. Show a Windows toast, unconditionally — whether or not step 2 succeeded. A
   reminder that did not arrive is a failed feature, so the visual copy is never
   contingent on the spoken one.
4. If the task was one-off, delete the sidecar file. The task deletes itself.

The toast is raised through PowerShell's WinRT toast API, using the existing
`_powershell` helper pattern in `clio/capabilities/control.py` — no new
dependency. If that fails for any reason, it falls back to a `ctypes` message
box, which cannot fail. This is the one fallback kept, because silence here is
the worst possible outcome.

### Core side

`Orchestrator` gains a public `announce(text)` wrapping the existing private
`_announce`, and `announce` joins `say`, `mute` and `tts_speed` in
`_command_handler` in `clio/__main__.py`. Nothing new is built for speaking: the
announcement queue is the same one timers already fire through, so a reminder is
spoken in the same gap between turns, with the same interruption behaviour.

## Data flow

Setting one:

```
"remind me to take my meds at 7am"
  -> parse_reminder_request  -> Reminder(text, when, repeat)
  -> write <memory root>/reminders/<id>.txt
  -> schtasks /create Clio-Reminder-<id>
  -> read back via schtasks /query   (no silent success)
  -> "Okay, seven tomorrow morning: take your meds."
```

Firing:

```
Windows, 07:00
  -> pythonw -m clio.remind <id>
  -> read <id>.txt
  -> ws://127.0.0.1:8765  {"cmd": "announce"}  ->  Clio speaks it (if running)
  -> Windows toast                             ->  always
```

## Error handling

- `schtasks` refuses (permissions, malformed time): said plainly — "Windows
  wouldn't let me schedule that" — never reported as set.
- Every creation is read back with a query. If Windows does not list the task,
  she says it did not take.
- Clio not running at fire time: toast only. This is the intended behaviour, not
  a degradation.
- Machine off at fire time: the reminder is missed. The `schtasks` CLI cannot set
  the "run as soon as possible after a missed start" flag; only the XML task API
  can. Marked in the code with a `ponytail:` comment naming the ceiling and the
  upgrade path (register the task from XML) rather than building XML generation
  now.
- A sidecar file missing at fire time: fires anyway, with the text lost, as above.

## Testing

Unit, no real Task Scheduler and no real network:

- Phrase parsing, including the phrases that must **not** match: timers, "remind
  me" with no time given, and the clock and news phrasings that already exist.
- The `schtasks` argument list built for each schedule shape (once, daily,
  weekly, weekdays), against an injected fake runner.
- Parsing a real `schtasks /query /fo csv /v` sample back into a spoken list.
- The fire script against a fake WebSocket server: Clio up (announce sent and
  received), Clio down (connection refused, toast still shown).
- Sub-minute request answered as a timer, not a reminder.

Live, by hand, once: a reminder one minute out, fired with Clio running, and
again with her closed.

## Out of scope

- Editing a reminder. Cancel and set another.
- Snoozing.
- Reminders tied to a place or an event rather than a time.
- Generating task XML for missed-start catch-up (see Error handling).

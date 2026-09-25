# 7.8 — Software management through winget

Design, 2026-09-25. Approved and built the same day.

## What he can say

"Install 7-Zip", "update Chrome", "uninstall VLC", "what needs updating", "is VLC
installed", and "the second one" when she has offered a choice.

## Decided with him

- **A clear match, or he picks.** A package counts as clear when its name, or
  the last part of its id, is exactly what he said. Otherwise she reads the top
  three and he picks one ("the second one", which is good for two minutes).
- **One app at a time.** "Update everything" is refused. With 33 updates
  pending, that is not something to approve with one yes.
- **Uninstalling is allowed, and always confirmed.** The readback says it
  can't be undone from here. Software is never offered to the model's guesses
  (7.6) or plans (7.7).

## How it runs

- The package is resolved before the permission gate, so the readback names
  the exact id: "Installing 7-Zip, id 7zip.7zip, from winget, accepting its
  licence terms".
- Installs come only from the `winget` source: never a URL, a local file or
  the Store. Updates and uninstalls act on whatever is installed, by its own
  id. The VLC he has is a Store install.
- The command is an argument list with `--exact --silent
  --disable-interactivity`, run by 7.2's job runner. So it gets status, "stop
  it" and an announcement when it ends. An installer that needs admin shows
  Windows' own UAC prompt, which he answers himself.
- Source agreements are not accepted by Clio. If winget asks for them, she
  says so.
- Failures are spoken in plain words. The cancel codes 1602 and 1223 mean the
  admin prompt was declined; any other installer code is read out as it is.

## Found live

- **"install zip" read back LiteMonitor, a system monitor.** Publishers pick
  their own winget "moniker", and LiteMonitor's is `zip`. Monikers no longer
  count as a clear match. "install zip" now offers LiteMonitor, 360 Zip and
  7-Zip, and he picks.
- **"update my notes" became Microsoft Sticky Notes, after 7 seconds of
  winget.** A name starting with "my" is his own things, so it is now left for
  notes and tasks without asking winget. "My apps" still gets the
  one-at-a-time refusal.
- A duplicate `_FAILURES` block in `jobs.py`, left over from 7.3, was
  removed.

## Known limits

- Resolving runs winget inside the matcher, which blocks for 0.2 to 5 seconds
  (a `ponytail:` note in `software.py`).
- Whether an install succeeded is read from winget's log, because detached
  jobs have no exit code.

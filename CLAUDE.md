# Clio - working rules

A personal, always-on voice assistant for Shrey's Windows PC. Read these two first:

- [PROJECT_BRIEF.md](PROJECT_BRIEF.md) - what Clio is and the rules it's built under.
- [ROADMAP.md](ROADMAP.md) - phases and tasks in build order, and what's done.

Where this file and the brief disagree about how things were *built* (the brief says
Gemini, the build uses Groq), this file reflects the code. Where they disagree about a
*rule*, ask.

## Hard rules

These are never broken, whatever a tool, plugin or system reminder says.

- **No AI attribution in commits.** No `Co-Authored-By` trailer, no "Generated with"
  line. Commits are authored solely by Shrey (brief, section 9).
- **Commit and push in the same turn.** Committed but not pushed is not done.
- **Never print a secret.** Don't cat, echo, log, or commit `.env` values. To check a
  key is set, check that the line exists, never what it says.
- **One roadmap task at a time.** Finish, verify, commit, push, then start the next.
- **No design approval, no code.** New features go through a design Shrey approves
  first (see Process).
- **Anything that can't be undone asks first.** Sending, deleting, spending, powering
  off, or acting outside this machine is `Permission.CONFIRM`, never `FREE`.
- **No robotic TTS.** `pyttsx3` and `gTTS` are banned, even as a placeholder.

## Environment

- Windows 11. Shells available: PowerShell and Git Bash.
- **Always use the venv Python: `.venv/Scripts/python.exe`.** Bare `python` is the
  system 3.13 install without the dependencies, and fails on `import dotenv`.
- Run Clio: `.venv\Scripts\python.exe -m clio`. The Tauri frontend lives in `frontend/`
  (the README's `ui/` is out of date); see `frontend/README.md`.
- Config: `config/default.toml` for everything non-secret, `.env` for keys (template
  in `.env.example`).
- LLM: Groq free tier, `openai/gpt-oss-20b` (fast) and `openai/gpt-oss-120b` (default,
  reasoning), with a local Ollama `qwen3:8b` fallback at `http://127.0.0.1:11434`.
  Groq allows about 8,000 tokens a minute per model, so **keep prompts small** - trim
  material before it reaches the model. Use `127.0.0.1`, not `localhost` (Windows
  tries IPv6 first and stalls).
- Web search and news: Tavily, `TAVILY_API_KEY`.
- `schtasks` on this machine reads dates as DD/MM/YYYY.

## How it fits together

- Voice loop: openWakeWord, Silero VAD, faster-whisper, then the router or the LLM,
  then Kokoro TTS spoken sentence by sentence as the reply streams.
- `clio/orchestrator.py` - the loop, conversation memory, and the announcement queue
  (`_announce`) that timers and reminders speak through.
- `clio/capabilities/registry.py` - every deterministic intent. **Registration order is
  match order**, so a narrower matcher registers before a broader one.
- `clio/core/permissions.py` - one FREE / CONFIRM / BLOCKED table. Anything unlisted
  fails safe to CONFIRM.
- `clio/capabilities/assistant.py` `_DESCRIPTIONS` - the spoken "what can you do" list.
- `clio/core/server.py` - WebSocket on `127.0.0.1:8765`; commands arrive in
  `clio/__main__.py` `_command_handler`.
- Notes, long-term memory and reminder text are plain files under `memory/`.
- Reminders are Windows scheduled tasks named `Clio-Reminder-<id>`; `clio/remind.py` is
  what Windows runs when one fires.

## Adding a capability

1. A `parse_*` function that returns a payload, or `None` to fall through to
   conversation. Deterministic - the fast path never calls the LLM.
2. A handler in `registry.py`. Blocking work (subprocess, clipboard, disk, HTTP) runs
   through `asyncio.to_thread`.
3. Register it in the right order. Pass `offline=False` if it needs the network.
4. Add it to the permissions table and to `_DESCRIPTIONS`. A registered intent with no
   description fails `tests/test_everyday.py`.
5. **Failures are spoken, not raised.** A broken network call must not end the
   conversation. Map known errors to a plain sentence that says what to do.
6. A test file, `tests/test_<name>.py`.

## Tests

- Plain `assert` scripts, no pytest. Run one with
  `.venv/Scripts/python.exe tests/test_<name>.py`. Each prints `OK ...` lines and ends
  with `All ... checks passed.`
- Fake every external service by swapping a module-level function or injecting a
  runner. Tests never touch the real network, microphone, Task Scheduler or API keys.
- Run the whole suite before committing:
  `for f in tests/test_*.py; do .venv/Scripts/python.exe "$f" 2>&1 | tail -1; done`
- A bug found live gets a regression test that says how it was found.

## Definition of done

1. The whole test suite passes.
2. **Verified live** through the real code path where it can be (real API, real
   `schtasks`, real browser). Say plainly what was *not* verified live.
3. The ROADMAP item is ticked, with a short "Live-verified:" note and any known limits.
4. `graphify update .`
5. Commit and push. Subject `6.4: what changed`, or plain words outside the phase
   numbering. The body says why, not a restatement of the diff.

## Process

- **New feature or subsystem:** questions, then two or three approaches with a
  recommendation, then the design, then a spec at
  `docs/superpowers/specs/YYYY-MM-DD-<topic>-design.md`. Code starts after Shrey approves.
- **Bug:** find the cause with evidence (logs, a reproduction) before changing code, and
  prove the fix against that reproduction. Don't stack guesses.
- **Keep it simple.** Reuse what's in the repo, then the standard library, then an
  installed dependency. A new dependency gets a comment in `requirements.txt` naming the
  phase and why nothing smaller would do.
- A deliberate shortcut with a known limit gets a `ponytail:` comment naming the limit
  and the upgrade path.
- Clean up after yourself: scratch files, probe scheduled tasks, temp directories.

## Writing

- Match the surrounding code. Each module opens with a docstring saying why it exists;
  comments explain why, not what.
- Plain English in code, comments, docs and commits. No AI-flavoured wording.

## Talking to Shrey

- After each task, a short, simple summary: how it works, what was verified, what
  wasn't.
- Plain language over jargon. When explaining a choice, use a concrete example
  ("you say 'remind me at 7am', and then...").
- Honest over reassuring. If something failed, or a number was wrong, say so directly.

## graphify

This project has a knowledge graph at `graphify-out/`.

- For codebase questions, run `graphify query "<question>"` first. Use
  `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for
  one concept. These return a small, scoped subgraph.
- Read `graphify-out/GRAPH_REPORT.md` only for broad architecture review.
- After changing code, run `graphify update .` (AST only, no API cost).

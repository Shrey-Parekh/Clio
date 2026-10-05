# Clio

A personal voice assistant for Windows. Say "Hey Clio" or press a hotkey, talk
naturally, and she answers out loud - and does real work on this machine and my
accounts: files, apps, email, projects, commands, installs.

- **Brief:** [PROJECT_BRIEF.md](PROJECT_BRIEF.md) - what it is and the rules it's built under.
- **Plan:** [ROADMAP.md](ROADMAP.md) - phases and tasks in build order, what's done, and
  what was found when each was checked on the real machine.

## What she can do

| Area | Examples |
|---|---|
| Talking | Wake word (`Hey / Hi / Hello Clio`), `Ctrl+Alt+C` to wake, typing in the chat window, interrupting her mid-sentence, "stop", "slow down", memory of facts across sessions |
| Everyday | Timers, stopwatch, reminders and alarms that survive a restart, world clock, sums, units, currency, weather, web search and news, notes, a to-do list, coin and dice |
| The PC | Open and close apps, folders and sites; volume, windows and media; lock and sleep (asks first); system and network status; tidy the clipboard |
| Files | Find and summarise PDF, Word, PowerPoint, Excel, CSV, JSON and code; create folders and files; copy, rename, move; delete to the Recycle Bin with undo |
| Email (Gmail) | Unread counts, what needs a reply, summaries, drafts saved to Gmail, sending a draft he has seen after a readback and a 10-second hold |
| Projects | Find, register, run, watch and stop his projects; explain a failure in plain words ("it ran out of GPU memory") |
| Commands | `git`, `pip`, `python`, `npm`, `cargo` by voice - no shell anywhere; read-only commands run free, the rest are read back first |
| Software | Install, update and uninstall through `winget`, one app at a time, reading back the exact package id |
| Loose requests | A sentence she doesn't recognise is reworded into one she does and checked with him; a request with several steps becomes a plan he approves once |
| Dictation | `Ctrl+Alt+D` types what he says into whatever window has the cursor |

Anything that changes, sends, deletes or spends asks first. Deleting, sending
and powering off are never done on a guess.

## How it fits together

```
 microphone -> wake word (openWakeWord) -> speech detection (Silero VAD)
            -> speech to text (faster-whisper, on the GPU)
            -> deterministic router (most requests: no model call at all)
               or the model (Groq, gpt-oss; local Ollama as a fallback)
            -> text to speech (Kokoro, on the GPU), spoken as the reply streams
```

- **Core** (`clio/`, Python): the voice loop, the router, every capability, and a
  WebSocket server on `127.0.0.1:8765`.
- **Shell** (`frontend/`, Tauri v2): the HUD and the chat & settings window, a tray
  icon, start with Windows. It starts the core, restarts it if it dies, and logs
  why to `logs/core-console.log`.

## Requirements

- Windows 11, Python **3.11**, an NVIDIA GPU with a current driver (speech runs on CUDA)
- A [Groq](https://console.groq.com) API key (free tier)
- Optional: a [Tavily](https://tavily.com) key for web search; a Gmail app password for email;
  [Ollama](https://ollama.com) for the offline fallback
- For the desktop window: Node.js and the Rust toolchain

## Setup

```
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
```

Fill in `.env` - at least `GROQ_API_KEY`. Then put the models in `models/` (they are
not in git):

| File | From |
|---|---|
| `kokoro-v1.0.fp16-gpu.onnx`, `voices-v1.0.bin` | [kokoro-onnx releases](https://github.com/thewh1teagle/kokoro-onnx/releases), `model-files-v1.0` |
| `silero_vad_16k.onnx` | `silero_vad_16k_op15.onnx` from [snakers4/silero-vad](https://github.com/snakers4/silero-vad), renamed |
| `wake_words/` | trained per [docs/wake_word_training.md](docs/wake_word_training.md) |

Each of these is also noted beside its setting in `.env.example`.

## Run

The core on its own, from the repo root:

```
.venv\Scripts\python.exe -m clio
```

With the window, which also starts the core:

```
cd frontend
npm install
npm run build
src-tauri\target\release\clio.exe
```

The release build registers itself to start with Windows. A debug build
(`npm run dev`) never touches the startup entry. More in
[frontend/README.md](frontend/README.md).

## Configuration

- `config/default.toml` - everything that isn't a secret: hotkeys, voice, wake
  threshold, file roots, email rules, registered projects.
- `.env` - keys, and anything specific to this machine (which microphone).
- `memory/` - notes, tasks, remembered facts and conversation history, as plain
  files he can read and edit.

## Tests

Plain `assert` scripts, no framework. Every external service is faked; no test
touches the network, the microphone or a real account.

```
.venv\Scripts\python.exe tests\test_everyday.py
```

All of them (Git Bash):

```
for f in tests/test_*.py; do .venv/Scripts/python.exe "$f" 2>&1 | tail -1; done
```

## Layout

| Path | Purpose |
|---|---|
| `clio/` | Core: voice loop (`orchestrator.py`), capabilities, router, permissions, LLM, speech |
| `frontend/` | Tauri shell: HUD, chat & settings window, tray, icons |
| `config/` | `default.toml` - settings and persona |
| `tests/` | One script per capability |
| `docs/` | Wake-word training |
| `memory/`, `logs/`, `models/` | Runtime data - never committed |

## Git workflow

- `main` is the working branch. Each roadmap task lands as one commit, pushed once
  it has been verified - see [ROADMAP.md](ROADMAP.md) for what "done" means.
- Commit subjects are `7.5: what changed`, or plain words outside the phase
  numbering. The body says why, not a restatement of the diff.
- No secrets, models, `logs/` or other runtime output are ever committed - see
  `.gitignore`.

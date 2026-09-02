# Clio - Build Plan

Ordered phases. Each phase ships something usable. Each task is finished, verified, and
committed before the next one starts.

Legend: `[ ]` not started &nbsp;·&nbsp; `[~]` in progress &nbsp;·&nbsp; `[x]` done

---

## Stack decisions

Proposed, with reasoning. Sign off or override before Phase 0 code lands.

| Layer | Choice | Why |
|---|---|---|
| Core service | Python 3.11 | Best ecosystem for STT/TTS/wake-word/Windows automation. 3.11 not 3.13 - several audio/ML wheels still lag on 3.13. |
| Core transport | FastAPI + WebSocket | One core process; UI, phone webhook and remote all speak to it the same way. Gives Tier 4 a path for free later. |
| Wake word | openWakeWord (ONNX), custom-trained on "Clio" | Local, ~1% of one core, free. No pretrained "Clio" model exists, so task 1.5 trains one from synthetic speech - a few hours on the 4060 Ti, one time. Porcupine's console would be faster but ties a personal-tier access key into the critical path. |
| VAD / endpointing | Silero VAD | Tiny and accurate. Drives both turn-end detection and barge-in. |
| STT | faster-whisper `small.en`, CUDA int8_float16 | Local, roughly 200-400ms on the 4060 Ti, no per-word cost, works offline. |
| TTS | Edge TTS primary, Kokoro local fallback | Edge is neural quality, free, and streams. Kokoro is the offline path. Both get compared in task 1.1 before we commit. |
| LLM | Gemini via `google-genai`, behind a provider interface | You have Pro. The interface means swapping or adding a model is a config change. |
| Frontend | Tauri v2 + web UI | Tray, overlay HUD and chat window in one app at roughly 10-30MB idle. Electron would cost 200MB+, which fights the footprint constraint. |
| Config | TOML, secrets in `.env` | Human-editable and diffable. Secrets never in the repo. |
| Storage | SQLite, plus plain markdown/JSON for memory | Memory stays inspectable and hand-editable, per the brief. |

**Hardware confirmed:** RTX 4060 Ti 8GB - comfortably fits Whisper `small`/`medium` and Kokoro
at the same time with room left for normal work.

**Needs installing:** `gh` CLI (GitHub, Phase 8) and `ffmpeg` (audio handling, Phase 1).

---

## Phase 0 - Foundation

The skeleton everything else bolts onto. No features yet, but nothing after this needs a rewrite.

- [x] **0.1** Repo init, `.gitignore`, `.env.example`, README, Python 3.11 venv
- [x] **0.2** Package layout and dependency manifest - `clio/` package, `requirements.txt`, an entrypoint that starts and exits cleanly
- [ ] **0.3** Config system - TOML config plus `.env` secrets, typed access, validated on load, clear error when a key is missing
- [ ] **0.4** Structured logging - JSON to file, readable console, rotation. Every decision and tool call lands here.
- [ ] **0.5** Event bus - internal async pub/sub, so voice, UI, capabilities and remote all react to the same events without wiring each to each
- [ ] **0.6** GitHub remote and branch strategy - push to `Shrey-Parekh/Clio`, document commit conventions

*Verify:* `python -m clio` starts, reads config, logs a startup event, shuts down cleanly on Ctrl+C.

---

## Phase 1 - The voice loop

**The milestone.** Wake word through to natural speech, with a real capability wired end to end.
Voice quality and harness fundamentals belong to this phase, not to polish later.

### 1a - Voice quality first

- [ ] **1.1** TTS comparison - the same five real sentences through a female shortlist: Edge (`en-IE-EmilyNeural`, `en-GB-SoniaNeural`, `en-GB-LibbyNeural`, `en-AU-NatashaNeural`, `en-US-AvaNeural`), Kokoro (`bf_emma`, `af_heart`) and Gemini TTS. **You listen and pick.** Nothing else in this phase starts until the voice is chosen.
- [ ] **1.2** TTS engine module - chosen engine behind a `SpeechEngine` interface, sentence-level streaming (speak sentence one while two renders), cancellable mid-utterance
- [ ] **1.3** Audio input - mic capture, Silero VAD, turn endpointing. It knows when you started and stopped talking.
- [ ] **1.4** STT - faster-whisper on CUDA, warm-loaded, partial transcripts supported
- [ ] **1.5** Wake word - train a custom "Clio" openWakeWord model from synthetic speech, then run it always-on at low CPU with a tuned threshold and a clear acknowledgement. Measure the false-trigger rate over a normal day before calling it done.

### 1b - The brain

- [ ] **1.6** LLM provider interface and Gemini - streaming responses, timeout handling, one clean seam for swapping models
- [ ] **1.7** Tool calling with schema validation - the model picks a capability and supplies arguments; invalid arguments are rejected and retried, never executed blind
- [ ] **1.8** Session memory - rolling conversation context with trimming and summarisation so the window never blows
- [ ] **1.9** Error surfacing - every failure spoken in plain language. No silent no-ops, no dead prompt.

### 1c - Make it feel real

- [ ] **1.10** Barge-in - speaking over Clio stops it and starts listening
- [ ] **1.11** Conversation mode - it keeps listening briefly after answering, so follow-ups need no wake word
- [ ] **1.12** Persona and voice config - one voice, one personality, defined in config, consistent across responses
- [ ] **1.13** First real capability, timers - proves the whole path and the deterministic fast path at once. A timer must never cost an API call.

*Verify:* Say "Clio" - it wakes, you speak, it answers in a voice you like, you can cut it off
mid-sentence, follow up without re-waking, and "set a timer for two minutes" actually fires.

---

## Phase 2 - Harness hardening

Phase 1 works. This makes it *predictable*, which is the bar the brief actually sets.

- [ ] **2.1** Intent router - deterministic matching ahead of the LLM. Timers, system stats, app launching and conversions never reach the API.
- [ ] **2.2** Permission policy layer - one central Free / Confirm / Blocked classification. Built here because Phase 3 introduces the first destructive capabilities.
- [ ] **2.3** Retry with backoff, request timeouts, and a fallback path for API failure or rate limiting
- [ ] **2.4** Graceful degradation - an explicit map of what works offline (timers, file search, app launching, system control) against what does not, and it tells you which
- [ ] **2.5** Persistent memory - preferences and durable facts across sessions, stored as plain files you can read and edit
- [ ] **2.6** Referential follow-ups - "do that again", "open the file I mentioned", "the second one"
- [ ] **2.7** Multi-step execution with checkpoints - progress tracked and surfaced, no fire-and-forget chains
- [ ] **2.8** Observability - token and cost tracking per request, and a decision/tool-call trace you can debug from
- [ ] **2.9** Lazy loading and footprint audit - measure idle CPU and RAM, defer every heavy model until first use

*Verify:* Pull the network mid-conversation and it degrades honestly instead of hanging.

---

## Phase 3 - Capability registry and the machine

First real breadth. The registry is extracted *from* working capabilities rather than designed
ahead of them.

- [ ] **3.1** Capability registry - extract the pattern from timers into a registration interface: schema, permission tier, offline-capable flag, handler
- [ ] **3.2** Deterministic utilities - unit and currency conversion, calculations, weather. Zero LLM involvement.
- [ ] **3.3** System monitoring - CPU, GPU, RAM, disk, temperatures, and what is eating resources
- [ ] **3.4** Opening things - launch apps, open files, folders, URLs and projects by natural name
- [ ] **3.5** Window and system control - focus, minimise, arrange, volume, brightness, lock, sleep, monitor switching
- [ ] **3.6** File system, read-only - list, search by name and by content, read, summarise. Write, move and delete route through 2.2 confirmation.
- [ ] **3.7** Clipboard operations - read, transform, replace. "Fix the grammar in what I just copied."
- [ ] **3.8** Quick capture - "note this down", into a findable plain-text store

*Verify:* Ten capabilities registered, each reachable by voice, destructive ones asking first.

---

## Phase 4 - Input surfaces

Wake word alone is not enough. Three more ways in.

- [ ] **4.1** Global keyboard hotkey - works from any app, including fullscreen
- [ ] **4.2** Mouse button or gesture trigger
- [ ] **4.3** Dictation anywhere - you speak, it types into whatever has focus. High value and easy to underrate.
- [ ] **4.4** Push-to-talk mode - hold to speak, as an alternative to VAD endpointing

---

## Phase 5 - Frontend

Stops it feeling like a script you are babysitting.

- [ ] **5.1** Tauri shell and core WebSocket client
- [ ] **5.2** System tray - status, quick actions, mute, quit
- [ ] **5.3** Overlay HUD - lightweight, always on top, showing listening/thinking/speaking state and a live transcript
- [ ] **5.4** Chat window - full history, text input as an alternative to voice, editable memory view
- [ ] **5.5** Settings UI - voice, persona, permissions, capability toggles
- [ ] **5.6** Autostart with Windows - launches to tray with no measurable boot impact

---

## Phase 6 - Knowledge and accounts

- [ ] **6.1** Web search, fetch and summarise
- [ ] **6.2** News briefing - on demand, not scheduled spam
- [ ] **6.3** Calendar - read first, then create with confirmation
- [ ] **6.4** Alarms, reminders and scheduling - via Windows Task Scheduler, surviving restarts
- [ ] **6.5** Task list integration
- [ ] **6.6** Email, read-only - unread counts, triage, summarisation. You write the replies.
- [ ] **6.7** Document handling - PDF, DOCX, spreadsheets and images, read and summarised
- [ ] **6.8** Assignment help - read a brief, extract requirements, draft against them

---

## Phase 7 - Screen and context

On demand only. No continuous capture - cost and privacy both.

- [ ] **7.1** On-demand screen capture, explicitly triggered
- [ ] **7.2** Screen understanding - "what's on my screen", "read this to me", "what does this error mean"
- [ ] **7.3** Active-window context - resolve vague references by checking what you are actually looking at
- [ ] **7.4** Vision cost controls - downscaling, caching, a per-day budget cap with a visible counter

---

## Phase 8 - Work and development

- [ ] **8.1** GitHub - read freely (issues, PRs, CI, diffs); push, merge and delete behind confirmation
- [ ] **8.2** Coding agent spawn - works in a directory under supervision and reports back; you review before anything merges
- [ ] **8.3** Web automation - navigate, read, fill. Submit, post or buy needs confirmation.
- [ ] **8.4** Desktop app automation - APIs and scripting first, UI automation only where nothing else exists, brittleness acknowledged

---

## Phase 9 - Remote and ambient

The "I'm at college and left the file on my PC" phase.

- [ ] **9.1** Tailscale - identity-authenticated reach into the machine, no exposed ports
- [ ] **9.2** Wake-on-LAN - BIOS and NIC config, wake trigger, verified from outside the network
- [ ] **9.3** Remote command endpoint - authenticated, over Tailscale only
- [ ] **9.4** iPhone trigger - Shortcuts app to webhook. No native app needed.
- [ ] **9.5** File retrieval - "email me the chem assignment", delivered to mail or phone
- [ ] **9.6** Outbound push notifications - "tell me when this build finishes"

---

## Phase 10 - Automation and smart home

- [ ] **10.1** Rule engine - "when X happens, do Y", stored as editable rules
- [ ] **10.2** Scheduled routines - morning briefing, backups, file organisation
- [ ] **10.3** Batch file operations, with confirmation
- [ ] **10.4** Home Assistant - local API, vendor-neutral, slotting in as capabilities rather than a new architecture

---

## Explicitly out of scope

- **Apple Music control** - no automation surface on Windows. Revisit only on Spotify or macOS.
- **Locked platforms** - banks, WhatsApp, DRM, CAPTCHA-gated flows. Engineering does not open these.
- **Long-horizon unsupervised autonomy** - around 15 minutes of supervised multi-step work is the
  realistic ceiling. Everything here is designed around checkpoints, not fire-and-forget.

---

## Identity - settled

- **Name:** Clio
- **Wake word:** "Clio". Two syllables and uncommon in ordinary speech, which helps, but it is short -
  if false triggers turn out to be a problem in daily use, "Hey Clio" is the fallback and needs
  only a retrain, not a redesign.
- **Voice:** female, in the register of F.R.I.D.A.Y. The reference accent is Irish (Kerry Condon),
  so `en-IE-EmilyNeural` leads the Edge shortlist, with British and Australian alternatives
  alongside it - they land in similar territory without being an impersonation.
- **Frontend:** Tauri v2, confirmed.

- **Persona:** friendly and quick-witted, with room to be dry or funny - Jarvis rather than Friday
  on this axis. The constraint is restraint: wit shows up occasionally, not in every response, and
  never at the cost of getting to the answer. Concise stays the default. Defined in config, so it
  is cheap to retune once you have lived with it.

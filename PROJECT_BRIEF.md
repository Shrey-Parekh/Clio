# Clio — Personal Voice Assistant | Project Kickoff Prompt

> Paste this whole document as the first message in a new Claude chat / Claude Code session.

---

## 1. What we're building

A personal, always-available voice assistant for my Windows PC — a real working assistant in the Jarvis/Friday mould. It wakes on command, talks back, and executes real tasks on my machine and my accounts. Not a demo, not a toy: something I actually use daily and keep extending for years.

**Core loop:** wake trigger → listen → understand intent → route to a capability → execute → speak/show result.

The intelligence layer is off-the-shelf (LLM API). **The value is in two places: the capability registry** (what it can actually do — grows forever, one integration at a time) **and the harness** (how reliably it decides, executes, recovers, and remembers). Architecture must assume both keep growing.

---

## 2. Wake / activation methods (all four required, eventually)

| Trigger | Notes |
|---|---|
| Microphone wake word | Primary. Local wake-word detection, not always-streaming to cloud. |
| Keyboard shortcut | Global hotkey, works from any app. |
| Mouse shortcut | Extra mouse button / gesture. |
| Phone or other device | iPhone 14. Via Shortcuts app + webhook — no full native app needed. |

---

## 3. Voice quality — hard requirement, not a nice-to-have

**It must not sound robotic.** `pyttsx3` (Windows SAPI5) and `gTTS` are both unacceptable — they're the default choice in every tutorial and they're exactly the flat, synthetic sound I don't want. Do not use them, even as a placeholder, unless we explicitly agree it's a short stopgap.

Evaluate and pick from genuinely natural options:
- **Edge TTS** — Microsoft neural voices, free, no API key, surprisingly natural. Strong default.
- **Kokoro TTS** — small local model, fast on GPU/CPU, quality well above Piper. Good if we want zero cloud dependency.
- **Gemini native audio / TTS** — I have Gemini Pro; worth testing since it keeps everything with one provider.
- **ElevenLabs** — best quality available, paid. Reserve for if the free options aren't good enough.
- **Piper** — fast and local but noticeably lower quality. Fallback only.

**Also required for it to feel real, not just sound real:**
- **Streaming TTS** — start speaking as soon as the first sentence is ready, don't wait for the full response. This matters more for perceived speed than raw model latency.
- **Barge-in** — I can interrupt mid-sentence by speaking, and it stops and listens. Non-negotiable for anything that talks a lot.
- **Conversation mode** — after answering, it stays listening briefly so I can follow up without re-saying the wake word.
- **Consistent voice and persona** — one voice, one personality, defined in config. Not a different tone every response.

Test the shortlist on real sentences early and let me hear them before we commit. Voice is the thing I'll experience every single day.

---

## 4. The LLM harness — build this properly

This decides whether the whole thing feels solid or flaky. Concrete requirements:

**Routing and execution**
- **Tool/function calling** with schema validation — the LLM picks a capability and supplies arguments; invalid arguments are caught and retried, never executed blind.
- **Intent routing before the LLM.** Deterministic matches ("set a timer for 10 minutes", "what's my CPU usage") should never hit the API. Fast path first, LLM as fallback.
- **Multi-step execution with checkpoints** — for tasks needing several tool calls, track progress and surface it. Do not fire-and-forget long chains (see section 10 on agent drift).
- **Streaming responses** end to end, so TTS can start early.

**Reliability**
- Retry with backoff, request timeouts, and a fallback path when the API is down or rate-limited.
- **Failures get spoken, not swallowed.** If something breaks, it tells me what broke in plain language. No silent no-ops, no crashes to a dead prompt.
- Graceful degradation: define what still works with no internet (timers, file search, app launching, system control) versus what doesn't.

**Memory and context**
- **Conversation memory** within a session, with trimming/summarisation so we never blow the context window.
- **Persistent long-term memory** across sessions — preferences, recurring context, facts worth keeping. Stored locally, inspectable and editable by me as plain files, not an opaque blob.
- **Referential follow-ups** must work: "do that again", "open the file I mentioned", "the second one".

**Observability**
- Token and cost tracking per request, visible to me.
- Structured logs of what it decided, which tool it called, and why — so when it does something wrong I can debug it rather than guess.

Be realistic: nothing is literally flawless. What I want is **predictable** — it either does the thing, or it clearly tells me it couldn't and why.

---

## 5. Feature scope

### Tier 1 — Build first (high value, reliable, well-trodden)
- **Conversational chatbot** — general Q&A, reasoning, discussion. The base layer everything sits on.
- **Voice I/O** — wake word → STT → LLM → TTS, to the quality bar in section 3.
- **Alarms, timers, reminders, tasks, scheduling** — Windows Task Scheduler + calendar API.
- **Email tracking + summarisation** — read-only. "You have 12 unread, here are the 3 that need action." I write the replies myself.
- **Latest news / web research** — search + fetch + summarise. Morning briefing on demand.

### Tier 2 — Doing things on my machine
- **Opening things** — launch apps, open files, folders, URLs, projects. "Open my chem assignment", "open VS Code in the Clio repo".
- **Window and system control** — focus/minimise/arrange windows, volume, brightness, lock, sleep, monitor switching.
- **Screen understanding (on demand)** — "what's on my screen", "read this to me", "summarise this page", "what does this error mean". Hotkey or voice triggered.
- **Context awareness of what I'm doing** — when I ask something vague, it can check the active window/screen to work out what I'm referring to instead of making me re-explain. **On demand only — never continuous background capture** (cost + privacy, see section 6).
- **Doing multi-step tasks in apps** — prefer APIs and scripting wherever they exist; UI automation only where nothing else works, and expect brittleness there.
- **Dictation anywhere** — I speak, it types into whatever app has focus. One of the highest-value features and easy to underrate.
- **Clipboard operations** — read it, transform it, replace it. "Fix the grammar in what I just copied."
- **File system access (READ-ONLY by default)** — list, search by name or content, open, read, summarise documents. Delete/move/overwrite need explicit per-action confirmation.
- **System monitoring** — CPU, GPU, RAM, disk, temps, what's eating resources.
- **Quick capture** — "note this down", dumped somewhere I can find later.
- **Deterministic utilities** — timers, unit/currency conversion, calculations, weather. These should never touch the LLM.

### Tier 3 — Work and accounts
- **Writing full programs and projects** — under my supervision and control. Spawns a coding agent, works in a directory, reports back. I review before anything merges.
- **GitHub integration** — read freely (issues, PRs, CI status, diffs); write actions (push, merge, branch delete) need confirmation. Via `gh` CLI / GitHub API / MCP server.
- **Web browsing / automation** — navigate, read pages, fill forms. Anything that submits, posts, or buys needs my confirmation.
- **Assignment help** — read a PDF/DOCX brief, understand requirements, draft the work. (Draft/assist; I decide where the line is.)
- **Document handling** — read and summarise PDFs, DOCX, spreadsheets, images.

### Tier 4 — Remote and ambient
- **Remote wake + file retrieval** — the "I'm at college and left a file on my PC" case:
  - Wake-on-LAN (BIOS + NIC configured; PSU stays powered)
  - **Tailscale** for secure remote reach into home network (identity-based auth, no exposed ports — preferred over router port-forwarding)
  - Then either Clio auto-emails/uploads the requested file, or I RDP in myself (RustDesk / Chrome Remote Desktop / Windows RDP)
- **Push notifications to me** — "text/email me when this build finishes", "tell me when that PR gets reviewed".
- **Smart home integration** — coming soon on my end. Prefer Home Assistant (local API, vendor-neutral) over per-brand apps. Slots in as another capability, not a new architecture.
- **Rule-based automation** — "when X happens do Y". File organisation, batch operations, backups, scheduled routines.

### Explicitly dropped
- **Apple Music playback control** — no automation surface on Windows (Apple didn't build one). Not worth the brittleness. Revisit only if I switch to Spotify (full API) or move to macOS (AppleScript).

---

## 6. Permission model (non-negotiable)

Everything falls into one of three buckets. Implement as an explicit, central policy layer, not scattered ad-hoc checks:

1. **Free** — read files, list directories, read email, search web, read GitHub, screenshots on request, open apps/files, system info, answer questions.
2. **Confirm each time** — delete/move/overwrite files, send email, git push/merge, submit web forms, run arbitrary shell commands, purchases, anything outward-facing or destructive.
3. **Blocked** — no credential entry, no financial transactions, nothing irreversible without an explicit yes.

**Screen capture is on-demand only.** No continuous background monitoring of what I'm doing — expensive, and not something I want running silently.

Remote access (Tier 4) means the machine is reachable from outside my house — access must be authenticated (Tailscale identity), never just "whoever finds the IP."

---

## 7. Tech decisions already made

- **LLM:** Google Gemini (I have Pro). Put it behind a clean provider interface so swapping or adding models later is trivial.
- **Platform:** Windows 11, high-end PC.
- **Proper frontend, backend, and LLM layer** — three distinct, well-separated pieces.
- **Frontend needs real thought, not just a terminal window.** System tray presence, a lightweight overlay/HUD for voice interaction, and a proper chat window for longer sessions. It should feel like a product I'd use, not a script I'm babysitting.
- **Don't use the LLM for everything.** Where a deterministic script, a regex, a small ML/DL model, or a plain API call does the job faster, cheaper, or more reliably — use that. The LLM is for language and judgment. Smooth, seamless behaviour matters more than "AI did it."
- Any language, framework, or library is fair game — pick what is genuinely best for each piece.
- **Remote access:** Tailscale (recommended) for Tier 4.
- **Secrets in a config/env file, never committed.** `.gitignore` correct from commit one.

---

## 8. Performance constraints

- High-end PC, but **I don't want this eating my resources** — idle footprint minimal, active footprint modest.
- Wake-word detection runs constantly, so it must be lightweight (small local model, low CPU).
- Lazy-load models and services. Nothing heavy in memory until actually needed.
- No continuous screen capture, no continuous cloud streaming.
- **Cost discipline:** vision/screenshot calls are the expensive ones. On-demand only. Target pennies per day, not dollars.
- **Startup:** launches with Windows, sits quietly in the tray, doesn't slow boot.

---

## 9. How I want you to work with me

**Process**
- **One phase and one task at a time.** Finish it, verify it works, then move on. No sprawling half-built everything.
- After each implementation, **summarise the logic and how it works** — short, direct, simple, well-structured. I'm building this to understand it, not just to have it.
- Step by step, fully functional at each step. I should have something usable early, not after three months of architecture.

**Standards**
- Be accurate, correct, honest, and realistic. **No hallucinations, no assumptions.** If you don't know, say so. If something won't work, say that instead of building it anyway.
- Ask questions when genuinely unclear — don't guess.
- **Don't over-engineer or over-complexify.** Optimised, direct methods for implementation, execution, and integration.
- **No AI slop, no AI-flavoured wording** in code, comments, docs, or conversation.
- All code **modular, scalable, and flexible** — future capabilities must slot in without rewrites.

**Files and repo hygiene**
- Create only the files actually needed. Organise optimally, name things properly.
- **Remove throwaway test files and scratch/startup files once their work is done.** Don't leave debris.
- I'm making a GitHub repo for this. **Commit each completed task.**
- **Commits authored solely by me — no co-author trailers, no AI attribution.**
- Use git properly and professionally: sensible branch strategy, clean atomic commits, clear messages, no secrets committed.

**Tooling**
- Free to use any skills, plugins, MCP servers, or tools that genuinely help.

---

## 10. Reality check already established (don't re-litigate)

Worked through already — treat as settled context:

- Voice loop, chatbot, email, scheduling, news, file reading, GitHub, web browsing, code generation, app launching: **all solved, off-the-shelf, reliable.**
- Screen understanding: **works well** for reading and comprehension. Slow and token-costly as a *control* mechanism (screenshot → decide → click, repeated). Use APIs for control wherever they exist.
- Desktop app control: works via UI Automation or vision, but **brittle** — breaks when UIs change.
- Long-horizon autonomy: **the real gap.** ~15 minutes of unsupervised multi-step work is realistic; hours are not. Agents drift and don't notice. Design for supervised execution with checkpoints, not fire-and-forget.
- Some platforms are simply locked (banks, WhatsApp, DRM, CAPTCHA-gated flows). No amount of engineering opens those. Don't try.
- **The binding constraint is not the technology — it's how many integrations get written.** Ship a working loop with one real capability, then grow.

---

## 11. Where to start

Do **not** begin by designing a plugin system for capabilities that don't exist yet. That's how these projects die.

Phase 1 target: **a working voice loop I can actually talk to** — wake word → STT → Gemini → natural-sounding TTS — with harness fundamentals (tool calling, conversation memory, error handling) in place from the start, and one real capability wired in to prove the pattern.

Voice quality (section 3) and harness robustness (section 4) are part of phase 1, not polish to add later. A robotic-sounding, fragile prototype is not a useful milestone.

Propose the phase plan first, get my sign-off, then build phase 1.

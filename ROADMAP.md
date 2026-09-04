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
  | Wake word | openWakeWord (ONNX), custom-trained on two phrases: "Hey Clio" and "Wakey wakey Clio" | Local, ~1% of one core, free. Neither phrase has a pretrained model, so task 1.5 trains both from synthetic speech - more one-time work than a single phrase, since each needs its own trained model (or a combined multi-label classifier). Schema is a list from the start (`wake_word.phrases`) so more phrases slot in later without a redesign. |
  | VAD / endpointing | Silero VAD, raw ONNX via `onnxruntime` directly - not the `silero-vad` PyPI package | The pip package pulls in `torch` + `torchaudio` as hard dependencies even for ONNX-only use: 543MB, 61% of the entire venv. Downloaded the 1.2MB model file directly (same pattern as Kokoro) and wrote a ~40-line wrapper. Found the correct calling convention (each 512-sample chunk needs 64 samples of trailing context from the previous chunk prepended) by reading the reference implementation after an initial version scored real speech at the same near-zero probability as silence. |
  | STT | faster-whisper `small.en`, CUDA int8_float16 | Local, roughly 200-400ms on the 4060 Ti, no per-word cost, works offline. |
  | TTS | Kokoro, voice `af_heart` | Picked from an 80-sample comparison across Edge and Kokoro (task 1.1). Local and free - no cloud dependency, no billing risk like Gemini hit. Also fits the "zero cloud dependency" option the brief flagged as worth having. |
  | LLM | Groq (free, no billing), three tiers of `gpt-oss`, local Ollama fallback | Gemini's API billing wall (Sept 2026: even the nominally-free Flash tier returned zero quota, Pro is billing-only) ruled it out - not willing to pay. Groq hosts open-weight models free with no card ("openai/" names who built the weights, not an OpenAI API dependency). A tier is (model + reasoning effort): `gpt-oss-20b`/low for routing, `gpt-oss-120b`/low for spoken conversation, `gpt-oss-120b`/high for planning. All three verified live, sub-second. **`qwen/qwen3.6-27b` was benchmarked as the default and rejected** - it emits 848-1286 chars of inline `<think>` even for "what's 15% of 240", which breaks streaming TTS (you can't speak a think block). `gpt-oss` keeps reasoning in a separate response field, so nothing needs stripping before it's spoken. Local `qwen3:8b` via Ollama covers offline/fallback for free, satisfying 2.3 and 2.4 at zero cost rather than needing to be engineered separately. **Tool-calling benchmarked (the criterion that actually decides this):** `gpt-oss-20b` 6/6, `gpt-oss-120b` 6/6 - both inferred "a quarter of an hour" to `minutes:15`, mapped colloquial phrasing to the right enum, and correctly refused to call any tool for plain conversation or for a capability that does not exist. `qwen3.8-27b` scored 3/6 (silently made no call), confirming qwen is wrong here twice over. **Llama is not an option:** Groq deprecated its Llama chat models in June 2026 (only `llama-prompt-guard` classifiers remain), and local `llama3` returns HTTP 400 for any tool-equipped request - no tool support at all. Local `qwen3:8b` does score 4/4 on tools, but at 7-10s per call; acceptable for a network-down fallback, not for normal use. |
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
  - [x] **0.3** Config system - TOML config plus `.env` secrets, typed access, validated on load, clear error when a key is missing
  - [x] **0.4** Structured logging - JSON to file, readable console, rotation. Every decision and tool call lands here.
  - [x] **0.5** Event bus - internal async pub/sub, so voice, UI, capabilities and remote all react to the same events without wiring each to each
  - [x] **0.6** GitHub remote and branch strategy - push to `Shrey-Parekh/Clio`, document commit conventions

  *Verify:* `python -m clio` starts, reads config, logs a startup event, shuts down cleanly on Ctrl+C.

  ---

  ## Phase 1 - The voice loop

  **The milestone.** Wake word through to natural speech, with a real capability wired end to end.
  Voice quality and harness fundamentals belong to this phase, not to polish later.

  ### 1a - Voice quality first

  - [x] **1.1** TTS comparison - Edge (5 voices) and Kokoro (2 voices, then +9 more `af_*` voices once Kokoro's `bf_emma`/`af_heart` won the first round) compared across the same 5 sentences, 80 samples total. Gemini TTS dropped - same billing wall as the LLM. **Picked: Kokoro, `af_heart`.** Local, free, no cloud dependency.
  - [x] **1.2** TTS engine module - `clio/speech/tts.py`. `SpeechEngine` ABC + `KokoroSpeechEngine`. Sentence splitting handles abbreviations and decimals (`gpt-oss-3.5`, `192.168.1.1`, "Dr. Smith" all stay intact). Sentence N+1 renders while N plays - measured 4.50s wall time vs 5.06s synthesizing everything sequentially first. Cancellation is raced against synthesis at every await point, not just checked between them - the first version blocked cancellation for 1.4s behind a CPU-bound render before this was found and fixed; now resolves in 0ms. Remaining latency to actually go silent is ~180-240ms of `sd.stop()` itself - a real driver/backend cost, not fixed here, flagged for 1.10 (barge-in) since that's where the tighter budget belongs. Verified from a clean venv against only `requirements.txt`.
  - [x] **1.3** Audio input - `clio/speech/audio_input.py`. `VoiceActivityDetector` (Silero, raw ONNX, no torch - see stack decision below), `AudioCapture` (mic to async frame stream via sounddevice), `TurnDetector` (frames to whole-turn audio via speech onset/end-of-silence). Verified against real synthesized speech, not synthetic noise: correctly captures the turn, ignores a noise blip shorter than `vad_min_speech_ms`, doesn't cut a turn short on a normal mid-sentence pause, returns empty on pure silence. Real mic `InputStream` opens and streams correctly-shaped frames - full pipeline can't be end-to-end verified without a live speaker, which is a real limitation, not skipped.
  - [x] **1.4** STT - `clio/speech/stt.py`. `STTEngine` ABC, `FasterWhisperEngine` (local, CUDA, warm-loaded) and `GroqWhisperEngine` (whisper-large-v3-turbo). Benchmarked rather than assumed: identical accuracy (0% WER) on 5 sentences with known ground truth, both clean and at 5dB SNR (a genuinely noisy room, not a quiet office) - no accuracy edge for the larger cloud model in either condition. Local wins the default on the tiebreak: free, private, offline-capable per the brief, and doesn't spend the same Groq rate-limit budget the LLM calls use; Groq stays available as `CLIO_STT_PROVIDER=groq` (marginally faster, ~0.22s vs ~0.26s mean, useful if the GPU is busy). Partial-transcript streaming (`transcribe_stream`) verified against real audio - genuinely growing partials word by word, not a stub. One real Windows-specific fix: CTranslate2's CUDA backend needs `cublas64_12.dll`/`cudnn64_9.dll` from CUDA 12.x, which this machine's CUDA 13.0 toolkit doesn't provide - the bundled pip packages (`nvidia-cublas-cu12`, `nvidia-cudnn-cu12`) install the DLLs but don't put them on the search path; `os.add_dll_directory` doesn't work either since CTranslate2's CUDA init is lazy and happens deep in compiled code - only prepending to `PATH` itself works, which is what `_ensure_cuda_dlls_on_path()` does. Verified from a clean venv, fix included with no manual step required. Honest cost: those two packages alone are ~1.8GB on disk (cuDNN ships precompiled kernels for many GPU architectures) - matches the original CUDA stack decision, but worth knowing; `CLIO_STT_DEVICE=cpu` avoids it entirely if disk footprint ever becomes the actual constraint.
  - [~] **1.5** Wake word - `clio/speech/wake_word.py`. `WakeWordDetector` wraps openWakeWord, config-driven (`wake_word.model_paths()` maps each configured phrase to its trained `.onnx` via a slug convention). All 5 final phrases trained, copied to `models/wake_words/`, and verified through the real class (not just raw openWakeWord calls): each phrase correctly triggers itself, three negative phrases correctly trigger nothing, bad input raises a clear error. Config validation refuses to start if any configured phrase lacks a trained model, with a message pointing at `docs/wake_word_training.md`.
    - Training environment built in WSL2/Ubuntu 24.04 (the pipeline is Linux-only - Piper TTS); full process and every fix needed documented in `docs/wake_word_training.md` so it doesn't need rediscovering. Full pipeline verified end to end: generate clips -> augment -> train -> save `.onnx`.
    - **6 phrases attempted, 5 kept.** "Daddy is home Clio" (4 words, sentence-length) failed training twice at the identical score - a repeat, not noise - and was dropped rather than chased further; likely too long for openWakeWord's embedding window, which appears tuned for short 1-3 word phrases. One other phrase (`hello clio`) failed once and was fixed by a straight retry - training has real variance.
    - **The earlier CPU warning in this task was wrong and is retracted.** It assumed each phrase costs a full independent model, and that assumption is what drove trimming the phrase list down in the first place. Measured: 1 model = 1.95% of one core, 6 models = 2.28%. openWakeWord shares melspectrogram + embedding computation across models; each extra classifier head costs ~0.07%. Phrase count is effectively free at runtime - it costs training time (~8 min/phrase), not CPU.
    - **Training-reported "recall" is a misleading proxy.** The 20k-step `hey clio` model reported recall 0.446, but tested against real audio on Windows it scores 0.9365-0.9376 on "hey clio" vs 0.0009-0.001 on "hey there" / "hey google" / "what is the time" - a ~1000x separation, comfortably clear of the 0.4 threshold. Judge these models by real inference, not the training metric.
    - **Known false-trigger risk to watch:** "clio please" scores 0.48-0.75 across a couple of the trained models - above the 0.4 threshold. A natural phrase like "Clio, please open my chemistry assignment" could false-trigger before the real command is heard. Not blocking, but worth watching in real-world use.
    - **Still open, and can't be done synthetically:** everything above uses synthesized speech. Real microphone audio through the real mic is untested - that's the actual acceptance test for this task, and it needs you, not more synthetic benchmarking.

  ### 1b - The brain

  - [x] **1.6** LLM provider interface, Groq + local fallback - `clio/llm/provider.py`. `LLMProvider` ABC (`stream()` + a `complete()` convenience wrapper), `GroqProvider`, `OllamaProvider`, `FallbackLLMProvider` composing the two. Tier selection (fast/default/reasoning) is one config lookup away, matching `LLMConfig.model_for()`/`effort_for()` from 1.4's benchmarking.
    - Verified live across all 3 Groq tiers (sub-second), the real local Ollama fallback (not mocked), and the fallback path triggered by a genuine failure (a real nonexistent model name, not a simulated exception) - confirmed it actually falls through to a working local answer, not just that it doesn't crash.
    - This is where 2.3's retry/fallback requirement gets its first real exercise rather than being stubbed - every LLM call goes through `FallbackLLMProvider`.
    - Found and fixed during verification: the first retry design treated every Groq failure the same, so a permanent error (bad model name, invalid key - a 404/400/401, never fixable by retrying) still burned a full retry-with-backoff cycle before falling back. Split into `LLMError` (transient, retried) vs `LLMPermanentError` (skips straight to fallback) based on Groq's actual exception taxonomy. Verified both paths distinctly, not just the end-to-end outcome.
    - Ollama's `/api/chat` stream separates `thinking` from `content` as distinct fields - `OllamaProvider` reads only `content`, so `qwen3:8b`'s reasoning never leaks into what gets spoken, same guarantee `gpt-oss` gave on the Groq side.
  - [x] **1.7** Tool calling with schema validation - `clio/llm/provider.py` (`ToolSchema`, `ToolCall`, `call_tool()` on every provider incl. `FallbackLLMProvider`) + `clio/llm/tools.py` (`ToolCaller`: JSON-Schema validation via `jsonschema`, rejects and asks the model to correct, bounded retries, never invokes anything on unvalidated arguments).
    - Verified live: correct tool + valid args across all 3 tools from the 1.4 benchmark (timer/system_stats/open_app), plain conversation correctly returns `None` rather than forcing a tool call, and the fallback path triggered by a genuine primary failure produces a real, valid call from Ollama.
    - Verified deterministically (a fake provider, not a real model - forcing a live model to reliably produce invalid output isn't practical to script): the retry-with-correction loop recovers from a wrong-type argument, a permanently-missing required field raises `ToolCallError` after every attempt is exhausted rather than ever calling anything, and an invented tool name gets rejected and recovered from.
    - Checked Groq's and Ollama's actual response shapes rather than assuming one - Groq returns `arguments` as a JSON string (needs `json.loads`), Ollama returns it already parsed as a dict. Handled both explicitly instead of guessing a single format.
  - [x] **1.8** Session memory - `clio/llm/memory.py`. `ConversationMemory`: rolling message list, trims to a token budget (`~4 chars/token`, a proactive-safety-margin estimate, not an exact per-model count) by summarizing older turns via the LLM itself (fast tier) rather than dropping them, while the most recent turns (`keep_recent_turns`) stay verbatim so "do that again"/"the second one" keep working after a trim.
    - Verified live with real Groq-driven summarization: the summary retains specific facts from the trimmed turns (not a vague gloss), recent turns are byte-identical after trim (not paraphrased), and a second trim round correctly folds new content into the existing summary rather than losing the first round or concatenating unboundedly.
    - Two fallback paths verified, not assumed: no provider given (naive placeholder, no crash) and a provider that raises mid-summarization (caught, logged, falls back to the naive placeholder rather than losing the trim or crashing the turn).
    - **Honest characteristic, not hidden:** in the verification run, total estimated tokens went *up* slightly after one trim (84 -> 110) because the test conversation was made of deliberately terse messages and the LLM's summary prose was more verbose than what it replaced. Real conversation turns will generally be longer than that synthetic test data, so this shouldn't bite in practice, but it's a real property of summarizing very short exchanges, not a hypothetical.
  - [x] **1.9** Error surfacing - `clio/core/errors.py`. `describe_error()` classifies any exception from any layer (`ConfigError`, `LLMError`/`LLMPermanentError`, `ToolCallError`, timeouts, connection/OS errors, `ValueError`, and an unknown-exception fallback) into a plain-language `SpokenError`, never the raw exception text. `report_error()` logs the full traceback and publishes it as a `clio.error` event on the `EventBus` for whatever eventually speaks it, and always returns the `SpokenError` directly so a caller with no bus yet - nothing is wired end to end - can still use `.spoken`.
    - Applied at the two places that were actually swallowing failures silently: `KokoroSpeechEngine._render_racing_cancel` (a failed TTS synthesis used to just return `None` with only a log line - now reports it) and `ConversationMemory._summarize` (a failed LLM summarization call used to log a warning and quietly fall back - now reports it before falling back, so the fallback itself isn't invisible). Both took an optional `bus: EventBus | None = None` constructor param rather than a hard dependency, since nothing constructs them with a real bus yet.
    - `clio/__main__.py`: a bad config no longer prints the raw `ConfigError` text as the whole message - it prints the plain-language line first, then the technical detail underneath. An unhandled exception inside the run loop is now caught, reported through `report_error`, and exits 1 with a spoken-language line instead of crashing to a bare traceback.
    - Verified: all 9 known exception classes classify to the right category/retryable flag and produce spoken text with no exception name or traceback in it; a `clio.error` event actually reaches a subscriber; both real swallow-sites now publish an event on failure instead of only logging; `_run()`'s try/except returns 0 on a clean stop and 1 (with the error reported) on an unhandled exception, exercised directly since Git Bash can't reliably deliver POSIX signals to a Windows console `python.exe` process - that OS-signal path itself is unchanged from Phase 0 and wasn't what this task touched.
    - **Honest limitation:** "spoken" is not yet audible. No orchestrator wires `EventBus` to `KokoroSpeechEngine` or `ConversationMemory` yet - the `clio.error` event has nowhere to be heard until that wiring exists (flagged as the open question for 1.13). Until then, plain language surfaces as console/log text, which is still strictly better than a swallowed exception or a raw traceback.

  ### 1c - Make it feel real

  - [x] **1.10** Barge-in - `clio/speech/barge_in.py`. `BargeInSpeaker` races `SpeechEngine.speak()` against `TurnDetector.wait_for_onset()` on the live mic frame stream; the instant the user starts talking, it calls `engine.cancel()`, waits for speech to actually stop, then hands the interrupting turn to `TurnDetector.capture_until_silence()` so no audio between onset and cancellation is lost.
    - `TurnDetector` (`clio/speech/audio_input.py`) was split to make this possible without a second detection path: `listen_for_turn` used to fuse onset detection and full-turn capture in one loop, so there was no way to react the instant onset happened without waiting for the whole turn to finish. It's now `wait_for_onset()` (returns the instant onset is reached, or `None` if the stream ended first) composed with `capture_until_silence()` (continues from there to end-of-turn) - `listen_for_turn` itself is just those two calls back to back, so its existing behavior didn't change.
    - Verified the split is behavior-preserving using a scripted fake VAD (deterministic, no real speech audio needed - `VoiceActivityDetector.process()` itself is unchanged code and wasn't what moved): pure silence still returns empty, a noise blip shorter than `min_speech_ms` still doesn't trigger a turn, and `wait_for_onset()` + `capture_until_silence()` composed by hand produces a byte-identical result to `listen_for_turn()` on the same input.
    - Verified `BargeInSpeaker` itself against a fake `SpeechEngine` that honors `cancel()` mid-speech: onset arriving while speaking cancels the engine and returns the captured interrupting turn; speech finishing naturally before any onset returns `None` with `cancel()` never called - no false interruption.
    - **Not yet exercised: the real latency number.** The 180-240ms `sd.stop()` cost flagged in 1.2 is still there - `BargeInSpeaker.speak()` calls `engine.cancel()` and then `await`s the speak task, so end-to-end interruption latency is `onset-detection time + that ~200ms`. Whether that's acceptable is a real-mic, real-voice question (same limitation as 1.3/1.5) - nothing here can answer it synthetically. If it turns out too slow once felt end to end, revisit WASAPI-exclusive or smaller buffers before assuming the design needs a rethink.
    - **Also not yet exercised:** acoustic echo - nothing here stops Clio's own voice from the speaker being picked up by the mic as a false "user started talking." No orchestrator exists yet to test this against a real speaker+mic pair in the same room; flagged as a real-world risk to watch once 1.13 wires the full loop, not solved here.
    - **Correction, found while building 1.11:** the original `speak()` gave up on a not-yet-arrived onset by calling `.cancel()` on the task consuming `frames`. That cancellation propagates into the shared async generator's suspended await point and closes it - fine when nothing needs `frames` afterward, but conversation mode needs to keep listening on that exact stream once a response finishes cleanly. Fixed by giving `speak()` a `listen_after_s` parameter (default `0.0`, so plain barge-in with no grace period is unchanged and re-verified) so there's a single onset watch spanning both "during the response" and "after it," cancelled at most once, only when truly giving up. All original 1.10 test scenarios re-run and still pass under the new signature.
  - [x] **1.11** Conversation mode - `clio/speech/conversation.py`. `ConversationSession` is a thin wrapper: `respond(text, frames)` calls `BargeInSpeaker.speak(text, frames, listen_after_s=follow_up_window_s)` - the same barge-in path handles a mid-response interruption, and the new `listen_after_s` grace period handles a follow-up said after Clio finishes talking. Returns the next turn's audio either way, or `None` (wake word required again) once the window elapses with nothing said.
    - New config: `audio.conversation_follow_up_ms` (default 6000ms, `CLIO_CONVERSATION_FOLLOW_UP_MS` override), validated positive alongside the existing VAD timing fields it sits next to in `config/default.toml`.
    - Verified: a mid-response interruption still returns immediately without waiting out the follow-up window; a follow-up said only after the response finishes cleanly is captured with no wake word (the actual 1.11 case, and the one that caught the bug above - it failed under the original design because the shared generator had already been closed); the follow-up window times out cleanly and returns `None` in bounded time; plain `BargeInSpeaker.speak()` with no `listen_after_s` (1.10's own use) is unaffected.
  - [x] **1.12** Persona and voice config - `config/default.toml` `[persona]` now carries the actual identity settled at the bottom of this file: `name = "Clio"` (was still the Phase 0 placeholder `"default"` - `.env`/`.env.example`'s `CLIO_PERSONA` override had the same stale value, fixed alongside it) and a `system_prompt` capturing the settled personality (friendly, quick-witted, occasional dry humor, restraint, concise, spoken-format constraints - no markdown/bullets since it's voice, admits what it doesn't know rather than guessing). `PersonaConfig` gained the `system_prompt` field, validated non-blank; `[speech]` gained `tts_speed` (default 1.0) for voice-config completeness, validated positive.
    - "Consistent across responses" is enforced structurally, not by convention: `ConversationMemory.get_messages()` (already built in 1.8) is the one place any outgoing message list gets assembled, and `ToolCaller.call()` (1.7) takes a caller-supplied messages list rather than building its own - so whichever LLM call eventually gets wired to a `ConversationMemory` instance primed with `config.persona.system_prompt` is primed the same way every time, from one config value, not a hand-typed string per call site.
    - Verified: the settled `name`/`system_prompt`/`tts_speed` values actually load (past the stale `.env` override, caught in the process); `CLIO_PERSONA_SYSTEM_PROMPT` and `CLIO_TTS_SPEED` env overrides both apply; a blank `persona.system_prompt` and a non-positive `tts_speed` both fail validation with a clear message, exercised through the real `python -m clio` CLI path (1.9's config-error surfacing), not just the loader in isolation; three independently constructed `ConversationMemory` instances from the same config produce byte-identical system-primed message lists.
    - **Honest limitation:** nothing consumes `tts_speed` yet - no orchestrator constructs a real `KokoroSpeechEngine` from config, so this is defined and validated but not yet threaded through to an actual voice. Same unwired state as everything else; flagged rather than glossed over.
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
  - [ ] **2.10** Self-diagnosis and feedback learning - when a tool call fails, Clio reads her own trace from 2.8 and explains what actually happened, not a generic "something went wrong." When you correct her - wrong assumption, bad call, preference she should have known - that correction is written to persistent memory (2.5) as a standing rule, checked before the same situation repeats. Scope to confirm: does a correction apply narrowly (this exact tool, these exact conditions) or should it generalise (this capability, or this kind of mistake in general)? Getting that wrong in either direction is worse than being conservative at first.

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

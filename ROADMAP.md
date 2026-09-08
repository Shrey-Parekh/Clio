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
  - [x] **1.5** Wake word - `clio/speech/wake_word.py`. `WakeWordDetector` wraps openWakeWord, config-driven (`wake_word.model_paths()` maps each configured phrase to its trained `.onnx` via a slug convention). All 5 final phrases trained, copied to `models/wake_words/`, and verified through the real class (not just raw openWakeWord calls): each phrase correctly triggers itself, three negative phrases correctly trigger nothing, bad input raises a clear error. Config validation refuses to start if any configured phrase lacks a trained model, with a message pointing at `docs/wake_word_training.md`.
    - Training environment built in WSL2/Ubuntu 24.04 (the pipeline is Linux-only - Piper TTS); full process and every fix needed documented in `docs/wake_word_training.md` so it doesn't need rediscovering. Full pipeline verified end to end: generate clips -> augment -> train -> save `.onnx`.
    - **6 phrases attempted, 5 kept.** "Daddy is home Clio" (4 words, sentence-length) failed training twice at the identical score - a repeat, not noise - and was dropped rather than chased further; likely too long for openWakeWord's embedding window, which appears tuned for short 1-3 word phrases. One other phrase (`hello clio`) failed once and was fixed by a straight retry - training has real variance.
    - **The earlier CPU warning in this task was wrong and is retracted.** It assumed each phrase costs a full independent model, and that assumption is what drove trimming the phrase list down in the first place. Measured: 1 model = 1.95% of one core, 6 models = 2.28%. openWakeWord shares melspectrogram + embedding computation across models; each extra classifier head costs ~0.07%. Phrase count is effectively free at runtime - it costs training time (~8 min/phrase), not CPU.
    - **Training-reported "recall" is a misleading proxy.** The 20k-step `hey clio` model reported recall 0.446, but tested against real audio on Windows it scores 0.9365-0.9376 on "hey clio" vs 0.0009-0.001 on "hey there" / "hey google" / "what is the time" - a ~1000x separation, comfortably clear of the 0.4 threshold. Judge these models by real inference, not the training metric.
    - **Known false-trigger risk to watch:** "clio please" scores 0.48-0.75 across a couple of the trained models - above the 0.4 threshold. A natural phrase like "Clio, please open my chemistry assignment" could false-trigger before the real command is heard. Not blocking, but worth watching in real-world use.
    - **Verified live (2026-09-05):** "Hey Clio" and "Hi Clio" both trigger from a real voice through the USB mic (device 2), which was the outstanding acceptance test. Still unmeasured: the false-trigger rate. Ten wake events were logged across the session and it is not known how many were spurious, so the "clio please" risk above remains open and needs daily use to judge.

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
    - **Second correction, found reviewing 1c as a whole - the 1.11 fix below was still wrong.** Giving up by cancelling the onset task closes the shared frame generator, and 1.11's fix only narrowed *when* that happened rather than stopping it. In the orchestrator (1.13) something always needs the stream afterwards - the next wake-word wait - so every normal end of a conversation killed the mic and Clio could never wake again. The loop was one-shot, and 1.13's smoke test asserted the symptom (`RuntimeError: mic frame stream ended`) as if it were correct behaviour. Fixed properly: `TurnDetector.wait_for_onset()` now takes an optional `stop: asyncio.Event` and returns `None` cleanly when it's set, and `BargeInSpeaker.speak()` gives up by setting that event (with `asyncio.shield` so `wait_for`'s timeout can't cancel it) instead of cancelling anything. Nothing in the speech path cancels a frame reader any more. Regression tests added: a lapsed conversation leaves the stream alive, and two full wake -> talk -> lapse cycles run on one continuous stream.
    - **Correction, found while building 1.11:** the original `speak()` gave up on a not-yet-arrived onset by calling `.cancel()` on the task consuming `frames`. That cancellation propagates into the shared async generator's suspended await point and closes it - fine when nothing needs `frames` afterward, but conversation mode needs to keep listening on that exact stream once a response finishes cleanly. Fixed by giving `speak()` a `listen_after_s` parameter (default `0.0`, so plain barge-in with no grace period is unchanged and re-verified) so there's a single onset watch spanning both "during the response" and "after it," cancelled at most once, only when truly giving up. All original 1.10 test scenarios re-run and still pass under the new signature.
  - [x] **1.11** Conversation mode - `clio/speech/conversation.py`. `ConversationSession` is a thin wrapper: `respond(text, frames)` calls `BargeInSpeaker.speak(text, frames, listen_after_s=follow_up_window_s)` - the same barge-in path handles a mid-response interruption, and the new `listen_after_s` grace period handles a follow-up said after Clio finishes talking. Returns the next turn's audio either way, or `None` (wake word required again) once the window elapses with nothing said.
    - New config: `audio.conversation_follow_up_ms` (default 6000ms, `CLIO_CONVERSATION_FOLLOW_UP_MS` override), validated positive alongside the existing VAD timing fields it sits next to in `config/default.toml`.
    - Verified: a mid-response interruption still returns immediately without waiting out the follow-up window; a follow-up said only after the response finishes cleanly is captured with no wake word (the actual 1.11 case, and the one that caught the bug above - it failed under the original design because the shared generator had already been closed); the follow-up window times out cleanly and returns `None` in bounded time; plain `BargeInSpeaker.speak()` with no `listen_after_s` (1.10's own use) is unaffected.
  - [x] **1.12** Persona and voice config - `config/default.toml` `[persona]` now carries the actual identity settled at the bottom of this file: `name = "Clio"` (was still the Phase 0 placeholder `"default"` - `.env`/`.env.example`'s `CLIO_PERSONA` override had the same stale value, fixed alongside it) and a `system_prompt` capturing the settled personality (friendly, quick-witted, occasional dry humor, restraint, concise, spoken-format constraints - no markdown/bullets since it's voice, admits what it doesn't know rather than guessing). `PersonaConfig` gained the `system_prompt` field, validated non-blank; `[speech]` gained `tts_speed` (default 1.0) for voice-config completeness, validated positive.
    - "Consistent across responses" is enforced structurally, not by convention: `ConversationMemory.get_messages()` (already built in 1.8) is the one place any outgoing message list gets assembled, and `ToolCaller.call()` (1.7) takes a caller-supplied messages list rather than building its own - so whichever LLM call eventually gets wired to a `ConversationMemory` instance primed with `config.persona.system_prompt` is primed the same way every time, from one config value, not a hand-typed string per call site.
    - Verified: the settled `name`/`system_prompt`/`tts_speed` values actually load (past the stale `.env` override, caught in the process); `CLIO_PERSONA_SYSTEM_PROMPT` and `CLIO_TTS_SPEED` env overrides both apply; a blank `persona.system_prompt` and a non-positive `tts_speed` both fail validation with a clear message, exercised through the real `python -m clio` CLI path (1.9's config-error surfacing), not just the loader in isolation; three independently constructed `ConversationMemory` instances from the same config produce byte-identical system-primed message lists.
    - ~~**Honest limitation:** nothing consumes `tts_speed` yet~~ - resolved by 1.13: `build_orchestrator()`'s `_build_tts()` passes `config.speech.tts_speed` into the real `KokoroSpeechEngine`, and `config.persona.system_prompt` into the `ConversationMemory` every conversation is primed from. Both are genuinely threaded through now.
  - [x] **1.13** First real capability, timers - `clio/capabilities/timer.py` + `clio/orchestrator.py`. This is the task the handoff flagged as the real gap: every Phase 1 piece through 1.12 was built and verified independently, but nothing talked to anything else - `clio/__main__.py` still just started, logged, and slept. `Orchestrator` is where that stops being true.
    - **`parse_timer_command()`** is pure regex - digits or a modest set of number words (`one`-`sixty`) plus a unit (seconds/minutes/hours and common abbreviations), gated on the literal word "timer" being present so it never fires on unrelated speech. No LLM path exists for timers at all - it either parses deterministically or falls through to plain conversation; there's no fallback tool-call that would let a timer ever cost an API call. A duration over 24h or an ambiguous phrase ("cancel my timer") returns `None` rather than guessing.
    - **`TimerCapability.start()`** schedules an `asyncio.sleep`-based background task and returns the spoken confirmation immediately - scheduling never blocks the turn. On firing, it calls back into the orchestrator to announce out loud.
    - **`Orchestrator`** is the actual loop: `wait_for_wake_word()` re-buffers the mic's 512-sample float32 VAD frames into the 1280-sample int16 chunks `WakeWordDetector` needs, without ever cancelling the shared frame generator - same fix as 1.11's bug, applied on sight this time rather than discovered by a failing test. After wake, `_conversation_loop()` captures a turn, transcribes it, routes it through the timer parser or the LLM (`_handle_utterance()`), speaks the result via `ConversationSession`/`BargeInSpeaker` (barge-in and follow-up-without-rewaking both apply to every response), and continues on whatever `ConversationSession.respond()` returns until it's `None`, at which point it goes back to waiting for the wake word. `build_orchestrator(config, bus)` wires the real components (`GroqProvider`+`OllamaProvider` via 1.6's existing `build_default_provider()`, local or Groq Whisper per config, real `KokoroSpeechEngine`) from config, matching everything settled in 1.1-1.12 exactly - nothing hand-tuned here that config didn't already decide.
    - **A new correctness concern - first "solved" wrongly, then actually solved.** A fired timer announces itself independently of whatever the orchestrator is doing. The first attempt used an `asyncio.Lock` (`_speak_lock`) around speaking, and this file claimed it verified; **that claim was wrong.** The lock only covered speaking, while `wait_for_wake_word()` and `listen_for_turn()` read the same frame generator *without* it - so a timer firing while Clio was idle (the normal case: you set a timer, she goes back to waiting) started a second reader and hit `RuntimeError: anext(): asynchronous generator is already running`. That error died inside the timer's background task as an unretrieved exception - a silent failure of exactly the kind 1.9 exists to prevent - and left the TTS task orphaned and still playing with the lock already released, so the serialization the lock was supposed to provide didn't hold either. The lock test passed only because it held the lock by hand instead of going through the real `run()` loop.
    - **The actual fix:** the main loop is now the sole reader of the frame stream, always. `_announce()` (called from the timer's task) only enqueues text and sets an event; `_speak_pending_announcements()` drains and speaks it from the main loop, at points where nothing else is mid-read. `wait_for_wake_word()` takes that event as an `interrupt` and returns `None` cleanly when set, so an alarm doesn't have to wait for a wake word to be spoken first. The lock is gone entirely - with one reader there's nothing to serialize. Cost, accepted and documented: a timer firing mid-conversation announces once the current turn finishes rather than cutting into it. Regression test: a timer fired while idle announces, doesn't crash, and the loop is still alive and listening afterwards.
    - Verified with fakes at the boundaries (mic frames, wake scoring, STT, LLM) and real coordination logic in the middle (real `TurnDetector`, `BargeInSpeaker`, `ConversationSession`, `ConversationMemory` - the actual pieces, not stand-ins for them): the timer path calls the LLM zero times and the plain-conversation path calls it exactly once with both turns landing in memory; wake-word rebuffering triggers correctly and leaves the shared frame generator intact and still consumable afterward; the `speak_lock` never lets two `speak()` calls overlap; and one full smoke test - wake, timer command, zero LLM calls, one spoken confirmation, follow-up window times out, back to waiting for a wake word - runs the entire loop end to end. Building that smoke test surfaced a real bug in the *test fixture* itself (a scripted fake VAD that rewound its script on every `reset()`, which `wait_for_onset()` legitimately calls on every fresh listen - it replayed the same "user is speaking" burst forever), not in the orchestrator; fixed by making the fake consume its script rather than replay it, matching what the real `VoiceActivityDetector.reset()` actually does (clears model state, never rewinds already-passed audio).
    - **Also fixed in the 1c review:** the deterministic timer path now records both sides of the exchange in `ConversationMemory` (it previously left no trace, so a follow-up had no idea a timer had just been set) - deliberately without calling `trim_if_needed()`, since trimming summarizes via the LLM and that would let a timer cost an API call after all. And `wait_for_wake_word()` clips samples before the int16 cast; a mic running hotter than full scale previously wrapped around (a 1.5 sample became -16386) and would have fed the wake models noise on loud speech.
    - ~~**Known, not fixed:** memory resets per wake; barge-in leaves the full reply in memory though only part was heard.~~ Both fixed - see 2.5 for the memory scope and persistence, and below for barge-in fidelity.
    - **Barge-in memory fidelity, fixed.** `SpeechEngine.speak()` now returns the text actually spoken aloud rather than `None`: `KokoroSpeechEngine` counts a sentence only once it has played all the way through, so a sentence cut off partway is left out (under-reporting by at most one sentence beats claiming whole sentences the user never heard). `BargeInSpeaker.speak()` and `ConversationSession.respond()` return a `SpeechOutcome` carrying `spoken_text`, `interrupted` and `next_turn`, and the orchestrator records the heard version - tagged `[cut off here - the user interrupted]` - so Clio never believes she said more than the user heard. Verified: interrupted mid-reply, memory contains the early sentences and the cut-off marker and none of the later ones; uninterrupted replies are still recorded whole.
    - **Not done, and can't be done from here:** the actual acceptance test below needs a real microphone, a real speaker, and a real human voice in a real room - none of which exist in this environment. Everything above is verified as far as synthetic testing can reach, same honest boundary as 1.3's and 1.5's own "still needs you" notes. This is the first time that boundary actually matters for correctness rather than just polish: `wait_for_wake_word`'s <80ms discarded rebuffering leftover (flagged in this same session), the barge-in latency budget (flagged in 1.10), and acoustic echo on announcements/responses (also flagged in 1.10) are all real open questions a live run will answer that no amount of scripted-fake testing can.

  *Verify:* Say "Clio" - it wakes, you speak, it answers in a voice you like, you can cut it off
  mid-sentence, follow up without re-waking, and "set a timer for two minutes" actually fires.

  ---

  ## Phase 2 - Harness hardening

  Phase 1 works. This makes it *predictable*, which is the bar the brief actually sets.

  - [x] **2.1** Intent router - `clio/core/router.py`. `IntentRouter`: a matcher returns a payload or `None`, a handler turns that payload into what to say (or `None` to stay silent), and registration order is match order. Anything matched is handled without an API call. `_handle_utterance` now routes first and falls through to the LLM only when nothing matches.
    - Timers and stop commands were two hardcoded branches; both moved onto the router unchanged. No new capabilities were added - those belong in 3.1, where the registry is extracted from working code rather than designed ahead of it.
    - A matcher that raises is logged and skipped rather than failing the turn, so one bad pattern cannot block the intents registered behind it.
    - Verified: stop returns silence and timer returns its confirmation, both with zero LLM calls; a normal question still reaches the LLM exactly once; "stop the timer" is not swallowed by the stop intent; a raising matcher falls through to the next one.
  - [x] **2.2** Permission policy layer - `clio/core/permissions.py`. One central Free / Confirm / Blocked table, read in one place. A capability declares nothing about its own risk; the policy decides, so adding a capability cannot quietly grant it authority. An action with no rule is CONFIRM, not FREE - a capability added without classification asks before acting.
    - `IntentRouter` was split so matching and running are separate: `match()` recognises the request and attaches its tier, and nothing executes until the caller has cleared it. BLOCKED is refused outright, CONFIRM asks out loud via the existing speak-and-listen path, FREE runs directly.
    - Confirmation is spoken and answered by voice. Only an explicit yes counts - silence, a question, or an unrelated sentence is a no, since the cost of a false yes is a destructive action that was never asked for.
    - No destructive capability exists yet, so this ships with the two current actions classified FREE and was verified against test intents: BLOCKED refused without asking, CONFIRM declined left the handler unrun, CONFIRM granted ran it, and `match()` alone never executes.
  - [x] **2.3** Retry with backoff, request timeouts, and a fallback path - mostly already shipped in 1.6 (`_MAX_RETRIES`, exponential backoff, 20s timeouts, real `OllamaProvider` fallback, permanent-vs-transient split). This task was an audit of that rather than a rebuild, and found two gaps.
    - **Mid-stream retry was wrong.** `FallbackLLMProvider.stream()` retried after a failure that happened *after* chunks had already been yielded, so the caller kept the partial text and then received the whole reply again on top of it - audible as a duplicated prefix. A failure once output has started is now final: no retry, no fallback, the error propagates and 1.9 speaks it.
    - **Rate limiting was treated as an ordinary transient error** and retried on a 0.5s/1s backoff, which cannot clear a 429 and just spends the budget faster. New `LLMRateLimited` skips retries and goes straight to the local model, which is free and already running - the graceful-degradation answer rather than a tuned backoff.
    - Standing check at `tests/test_provider_retry.py` (plain asserts, no framework): transient retried then fell back, rate limit and permanent error both skipped retries, mid-stream failure produced no duplicated text, both-down reported one error naming both.
    - Not done, deliberately: `GroqWhisperEngine` has no retry, but local STT is the default and the hosted one is opt-in. Belongs with 2.4 offline mapping.
  - [x] **2.4** Graceful degradation - the honest finding is that almost everything already worked offline: STT, TTS, memory and every deterministic intent are local, and 1.6 already fell back to Ollama. What was missing was not capability but *visibility* - the downgrade was silent, so a weaker local answer was indistinguishable from a normal one.
    - `FallbackLLMProvider.using_fallback` is tri-state (`None` until a call is made, then which model answered). The orchestrator mentions the downgrade once per outage rather than every turn, and re-arms when the primary recovers.
    - New `status` intent (`clio/capabilities/status.py`), FREE and deterministic: answers "what is working" / "are you online" without touching the network, which a connectivity question obviously must not. The offline list is generated from the registered intents rather than a hand-kept table, so it cannot drift.
    - No offline-capable *map* was added. With every current intent offline-capable, a table of all-true entries is dead config; the per-capability flag belongs in 3.1's registry alongside schema and permission tier, where capabilities that genuinely need network first appear.
    - Standing check at `tests/test_degradation.py`: timers answer with both providers dead and no LLM touched, status answers offline, the downgrade is announced exactly once, status reflects the real state, and recovery re-arms it. Caught one honesty bug in the process - status claimed "cloud model is up" before any call had been made, which the tri-state fixes.
  - [x] **2.5** Persistent memory - `clio/memory/store.py`. Pulled forward from Phase 2 because 1c's review turned up the per-wake memory reset, and "remember it until I say otherwise" was the actual requirement behind it. Three layers, plain files first: `memory/sessions/*.jsonl` is the complete append-only turn record, `memory/facts.md` is the distilled part carried between sessions (markdown, `##` groups, one `- ` fact per line, hand-editable), and `memory/index.sqlite3` is a derived FTS5 index. The plain files are the source of truth - `rebuild_index()` reconstructs the database from them exactly, so nothing important lives only inside a DB, which is the brief's "inspectable and editable, not an opaque blob" requirement.
    - **A graph store (Graphiti and similar) was considered and rejected**, not skipped: they need a Neo4j/FalkorDB server running beside Clio and an LLM call per ingest to extract entities. That's real idle footprint and per-turn API spend against the constraints in sections 8 and 4, to buy recall that SQLite's FTS5 already does here with zero new dependencies. Revisit only if keyword recall measurably fails in daily use; the plain-file layer means switching the index later costs nothing.
    - **Cost discipline is structural, not incidental.** Nothing ever loads the whole history: a question retrieves the facts (small, always) plus the top few matching turns, and only those enter the prompt. Retrieval runs on the LLM path only, so a deterministic command never even touches search. Fact consolidation is one `fast`-tier call at the *end* of a conversation, skipped entirely when the conversation never used the LLM - so a timer-only exchange still costs nothing, preserving 1.13's guarantee.
    - Verified: turns land on disk as plain JSONL; a brand-new process over the same directory sees the full history and the correct most-recent turns; `facts.md` dedupes and picks up hand edits immediately; deleting `index.sqlite3` and rebuilding restores every turn and search works again; recall of a real question came back in 194 characters rather than the whole transcript; and prior sessions seed back into the working window on a fresh run.
    - Config: `[memory]` - `root`, `recent_turns_on_start` (8), `recall_hits` (4), `consolidate` (true), all `CLIO_MEMORY_*` overridable. The store is a plain directory, so syncing it to Drive is a matter of pointing `root` at a synced folder - no integration needed.
    - **Fixed alongside:** `ConversationMemory` is now created once per run rather than once per wake, so re-waking continues the conversation instead of starting from amnesia. And a `.gitignore` trap caught in review - the `memory/` pattern silently excluded the new `clio/memory/` source package as well as the data store; anchored to `/memory/`.
    - **Third correction, found when asked directly whether this was actually solved.** It wasn't - three real crash paths existed and none were caught by the verification above, because every test used a healthy store. Reproduced directly: corrupting `index.sqlite3` mid-run (a real failure mode - disk hiccup, antivirus lock, concurrent access, not hypothetical) and asking a plain question crashed the whole turn with `sqlite3.DatabaseError: file is not a database`, because `_handle_utterance`'s recall call wasn't guarded. Two more had the same shape: `_consolidate_memory`'s fact-writing loop, and `Orchestrator.__init__`'s `start_session()`/`recent_turns()` at startup. All three now degrade instead of crash - a recall failure skips recalled context for that turn and keeps answering; a consolidation failure skips writing that round's facts; a broken store at construction falls back to in-memory-only conversation rather than stopping Clio from starting at all. `MemoryStore.search()`'s own except clause was also too narrow (`sqlite3.OperationalError` misses `DatabaseError`, which is what a corrupted file actually raises) and `facts()` had no protection against an unreadable `facts.md`; both fixed at the source, not just papered over from the orchestrator side. Re-verified: the exact corruption that crashed the turn now survives and still answers; consolidation survives an unwritable `facts.md`; startup survives an unusable memory root.
  - [x] **2.6** Referential follow-ups - split cleanly in two, and only half needed building.
    - **Conversational references already worked.** 1.8 keeps recent turns verbatim precisely so they resolve, and the model reads them: asked for three board games then "tell me more about the second one", it correctly picked Ticket to Ride. Verified live before writing anything, rather than rebuilt.
    - **Actions did not.** An utterance the router handles never reaches the model, so it cannot be referred back to. `clio/capabilities/repeat.py` matches "do that again" / "same again" and the orchestrator remembers the last `Match`, so a timer can be repeated with zero API calls.
    - Two deliberate properties, both tested. A repeat re-enters `_execute` rather than calling `Match.run()` directly, so a CONFIRM-tier action asks again every time instead of being silently re-run by saying "again". And a match is remembered only once it has actually run, so a blocked or declined action cannot be resurrected by repeating it.
    - "open the file I mentioned" and "the second one" over a *result list* need capabilities that do not exist yet - file access is 3.6 and list-returning capabilities are Phase 3. Building reference resolution for them now would be guessing at their shape.
    - Standing check at `tests/test_referential.py`.
  - [ ] **2.7** Multi-step execution with checkpoints - progress tracked and surfaced, no fire-and-forget chains
    - **Deferred, not skipped.** Nothing executes multi-step chains yet: every intent is single-shot and the LLM path is one completion. The only chaining machinery in the tree is `ToolCaller`, which has zero call sites. There is no progress to checkpoint, so building this now would be designing against capabilities that do not exist. Revisit when the first genuinely multi-step capability lands in Phase 3 - its shape is what defines what a checkpoint has to hold.
  - [x] **2.8** Observability - `clio/core/usage.py`. `Usage` per request and a `UsageTracker` per session, recorded by each provider and folded into the turn's existing log line, so one record carries what was decided, how long it took and what it spent.
    - Groq returns exact counts on the final streamed chunk with no `stream_options` (which this client version does not support anyway), so accounting is exact rather than the `~4 chars/token` estimate `ConversationMemory` uses for trimming - and costs nothing extra.
    - **Reasoning tokens are counted separately.** `gpt-oss` spends them thinking and never speaks them: a three-word answer measured 27 completion tokens of which 11 were invisible. Without splitting them out the numbers look wrong.
    - Cost is reported only where a rate is known, and `_RATES` ships empty because the current stack (Groq free tiers, local Ollama) bills nothing. Zero is the truth here rather than an invented price; add a rate if a paid model is ever wired in.
    - `FallbackLLMProvider.usage` reports whichever model actually answered, so a degraded turn is not attributed to the cloud model. The `status` intent now reports the session total out loud.
    - Standing check at `tests/test_usage.py`. The decision trace itself was already there - `logs/clio.jsonl` records intent, permission tier, latency and errors - so this only added the missing numbers rather than a second logging system.
  - [x] **2.9** Lazy loading and footprint audit - a measurement task, not a code one. Every heavy model was already lazy (`_ensure_loaded` on Kokoro, faster-whisper and the wake models); what was never measured is what the pre-warming added during 1.13's live debugging actually costs.
    - Measured on this machine (31.7 GB RAM, 20 logical cores), idle and waiting for the wake word:

      | | RAM | CPU (one core) | first reply |
      |---|---|---|---|
      | `prewarm = true` (default) | **1,817 MB** (5.6% of system) | 4.4% | fast, ~0.2s synth |
      | `prewarm = false` | **193 MB** (0.6%) | 6.3% | +~7s of model loading |

    - **CPU is the same either way** - about 5% of one core, 0.25% of the machine. That is the wake word plus VAD running continuously, and it confirms 1.5's claim (1.95-2.28% for the wake models) with the audio pipeline on top. The always-on part is genuinely cheap, which was the constraint that mattered.
    - **RAM is the whole trade: pre-warming costs 1.6 GB resident, permanently.** Kept `true` as the default because the 20-30s first-response delay was a real complaint from live use and 1.8 GB of 31.7 GB is affordable, but `memory.prewarm = false` is the documented lever for anyone who would rather have the RAM back - now with real numbers behind the choice rather than a guess.
    - **Not measured, honestly:** per-process VRAM. This GPU reports `[N/A]` for compute-app memory under WDDM, and Ollama plus a running game made the system total useless for attribution. Recorded as unknown rather than estimated.
    - No runtime footprint logging was added. The task asked for a measurement, and a monitoring subsystem for a number that changes only when models load would be the wrong trade.
  - [x] **2.10** Self-diagnosis and feedback learning - `clio/capabilities/diagnose.py` and `clio/capabilities/correction.py`, both on the deterministic path, so asking what broke never costs an API call.
    - **The trace already existed and nothing read it back.** Every failure since 1.9 has published its stage, category, detail and whether a retry could help; the spoken side then threw all of that away and said a fixed sentence per category. The fix was reading the record, not writing a new one: "About 2 minutes ago, LLM response failed - a language model problem: groq 503 upstream unavailable. That one is worth another try."
    - Captured by subscribing to `ERROR_EVENT` rather than recording at each call site, so failures raised inside TTS and summarization are diagnosable too, not just the orchestrator's own.
    - **Corrections are stored verbatim and generalised to nothing** - the conservative answer to the scoping question. "From now on, call me boss" is kept as that sentence, under a `Corrections` heading in `facts.md`, where it rides into every later session with the other facts and is in front of her before the situation repeats. A rule inferred wider than the sentence it came from misfires in situations he never spoke about, and that file is permanent and hand-editable.
    - Detection is anchored markers only ("from now on", "no, I said", "stop calling me", "remember that"), four words minimum. A false positive writes a standing rule that outlives the conversation, so "what happened next in the story" and "I have no idea what you mean" must not trigger it - both are in the standing check.
    - Recording a correction does not consume the turn: it is answered normally as well.
    - Standing check at `tests/test_diagnosis.py`.

  *Verify:* Pull the network mid-conversation and it degrades honestly instead of hanging.

  ---

  ## Phase 3 - Capability registry and the machine

  First real breadth. The registry is extracted *from* working capabilities rather than designed
  ahead of them.

  - [x] **3.1** Capability registry - extended `IntentRouter` rather than building a registry beside it. Registration was already matcher plus handler; what the interface was missing was the offline flag and any way to list what exists without running it.
    - **The offline flag is the part that was actually missing, and 3.2 needs it immediately.** `status` answered "what still works with no network" by listing every registered intent and excluding itself - true only while every capability happened to be local. Weather and currency conversion in 3.2 are not, and would have been announced as offline-ready. It is now each capability's own declaration, since only the capability knows.
    - **The permission tier is deliberately not declared at registration.** A capability that names its own tier means adding one can quietly grant it authority; the policy assigns it, and anything unclassified is CONFIRM. Registering now resolves the tier immediately, so an unclassified capability shows up in the log at startup instead of at first use.
    - `capabilities()` lists name, tier and offline claim without running anything - what `status` reads, and what the 5.5 settings UI will.
    - Registration stayed in `_register_intents`. Moving each capability's registration into its own module is a real question at eight more capabilities, not at five, and the interface is the same either way.
    - `IntentRouter.names` deleted - `capabilities()` replaces its one caller. Standing check at `tests/test_registry.py`.
  - [x] **3.2** Deterministic utilities - `calculate.py`, `convert.py`, `currency.py`, `weather.py`. Four capabilities, zero LLM calls, and no new dependency: `urllib` on a thread rather than an HTTP client, since between them the two network ones make one request each.
    - **Arithmetic is evaluated by walking the parse tree, never `eval`.** The transcriber will eventually mishear something into the expression, and an assistant that executes what it mishears is a different class of problem from one that gets a sum wrong. `__import__('os').system(...)` and `2 ** 999999999` are both in the standing check - the second because a huge exponent would hang the loop that is also carrying the microphone.
    - Anything that isn't clearly a sum returns None and falls through to conversation rather than being guessed at. Same contract as timers, for the same reason.
    - **Temperature is affine, so it converts through celsius rather than by a factor.** A factor table gets every other unit right and silently produces nonsense for this one, because 20C is not twice 10C.
    - Mismatched dimensions are refused: "5 miles in kilograms" gets no answer instead of a meaningless number.
    - **Currency is the one thing here that cannot be offline** - a rate is a fact about today, and a cached one quoted confidently is worse than saying it is unavailable. Frankfurter (ECB daily reference rates) needs no key or account, and the request carries an amount and two currency codes and nothing else. These are reference rates, not a trading price.
    - **Weather sends coordinates, which are the only genuinely personal thing any of these transmits, so it is opt-in.** `[location]` ships empty; until it is filled in she says she doesn't know where you are rather than inferring it from the IP address. Open-Meteo, also keyless.
    - Both network capabilities register `offline=False`, so 3.1's `status` stops claiming them during an outage - the reason that flag was built.
    - Verified live against both APIs, not only against the mocks in `tests/test_utilities.py`.
  - [x] **3.3** System monitoring - `system.py`. One capability with seven topics rather than seven capabilities, because the matcher is the only thing that differs between them, and `status` is already the precedent.
    - **`psutil` is the one new dependency, and it earns it.** Disk is stdlib (`shutil.disk_usage`), but memory pressure and per-process CPU have no stdlib equivalent on Windows: the alternative is shelling out to WMI or `typeperf` and parsing text that changes with the system locale - more code, slower, and wrong on a non-English machine.
    - **It refuses to report a CPU temperature.** `psutil.sensors_temperatures` does not exist on Windows, and the WMI thermal zone is usually empty, needs admin, and often reports a chipset sensor rather than the die. Saying "I'd rather not guess at it" is worth more than a number that might be the wrong sensor. GPU temperature comes from `nvidia-smi`, which is already on this machine for CUDA, so that costs no dependency either.
    - **The first live run called the System Idle Process the busiest thing on the machine.** The test passed - it was reading the printed output that caught it. Idle time is the machine doing nothing, so reporting it as the top consumer inverts the answer entirely. Excluded by name and pid.
    - Per-process CPU is divided by core count, so "Chrome at 40 percent" is the same scale as the machine-wide number he just heard, rather than psutil's per-core figure that runs past 100.
    - Everything blocks, so the snapshot runs on a thread - the loop it would otherwise stall is the one carrying the microphone. One 0.3s sampling window shared by every number, since psutil's first CPU reading is always zero.
    - Matching is deliberately narrow around the neighbours: "how hot is it outside" is weather, not thermals, and both are in the standing check at `tests/test_system.py`. "system status" still routes to the connectivity answer, which is now arguably the wrong one - left alone rather than churned.
    - **Ten capabilities registered, so the Phase 3 verify line is met on count.** Reachable by voice is not yet confirmed live.
  - [x] **3.4** Opening things - `launch.py`. Start Menu shortcuts by name, configured `[shortcuts]` for projects and sites, the Windows user folders, and a spoken domain. `os.startfile` is stdlib and already handles a shortcut, a folder and a URL, so there are no three code paths and no dependency.
    - **It never resolves a path out of the utterance.** "Open C colon backslash..." is not something she can be talked into. The transcriber will eventually mishear something into that slot, and running whatever came out of it is a different class of problem from opening the wrong app. Every target comes from his Start Menu, his own `[shortcuts]`, or a domain - all lists he built on purpose. In the standing check.
    - **FREE, not CONFIRM.** Opening something is reversible by closing the window, and the target is always from that bounded set. A capability that asks "shall I?" before every launch is worse than the Start Menu it replaces. 3.5 and 3.6's writes are where CONFIRM starts earning its keep.
    - **An unmatched short name is refused by name; an unmatched long phrase falls through to conversation.** The first version said "I couldn't find anything called about what is bothering you" to "open up about what's bothering you". Two words or fewer is a name she should admit she can't find - anything longer is speech. Refusing rather than falling through matters for short names, because the alternative is the model improvising a confirmation for something that never launched.
    - Substring matching before fuzzy, shortest name winning: "chrome" is inside "google chrome" but only ~0.6 similar to it, and "word" should reach Microsoft Word rather than a longer accidental container.
    - The article is kept as well as stripped when matching shortcuts - stripping "the" off "open the roadmap" made a shortcut he'd named "the roadmap" unreachable. Caught by the check, not by reading it.
    - 210 Start Menu shortcuts indexed on this machine, cached for the process. Verified live: `open my downloads` opened Explorer.
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

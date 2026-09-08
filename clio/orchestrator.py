"""The actual voice loop: wake word, turn capture, STT, the deterministic
timer fast path or LLM conversation, and speech - with barge-in and
conversation mode threaded through. Every piece up to 1.12 was built and
verified independently; this is where they become one running loop instead
of separate modules.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import numpy as np

from clio.capabilities.calculate import format_number, parse_calculation
from clio.capabilities.convert import format_conversion, parse_conversion
from clio.capabilities.control import apply as apply_control, describe_action, parse_control, parse_power
from clio.capabilities.correction import parse_correction
from clio.capabilities.currency import convert_currency, parse_currency_request
from clio.capabilities.files import look_up, parse_file_request
from clio.capabilities.diagnose import explain_failure, is_diagnosis_query
from clio.capabilities.launch import open_target, resolve as resolve_target
from clio.capabilities.repeat import is_repeat_command
from clio.capabilities.status import is_status_query
from clio.capabilities.stop import is_stop_command
from clio.capabilities.system import describe_system, parse_system_query
from clio.capabilities.timer import TimerCapability, parse_timer_command
from clio.capabilities.weather import describe_weather, is_weather_query
from clio.core.config import Config, ConfigError, LocationConfig
from clio.core.errors import ERROR_EVENT, report_error
from clio.core.events import Event, EventBus
from clio.core.logging import get_logger
from clio.core.permissions import Permission, PermissionPolicy, is_affirmative
from clio.core.router import IntentRouter, Match
from clio.llm.memory import ConversationMemory
from clio.llm.provider import LLMProvider, build_default_provider
from clio.memory.store import MemoryStore
from clio.speech.audio_input import AudioCapture, TurnDetector, VoiceActivityDetector
from clio.speech.barge_in import BargeInSpeaker
from clio.speech.conversation import ConversationSession
from clio.speech.cues import play_wake_cue
from clio.speech.stt import FasterWhisperEngine, GroqWhisperEngine, STTEngine
from clio.speech.tts import KokoroSpeechEngine, SpeechEngine
from clio.speech.wake_word import CHUNK_SAMPLES, WakeWordDetector

log = get_logger("clio.orchestrator")


async def wait_for_wake_word(
    detector: WakeWordDetector,
    frames: AsyncIterator[np.ndarray],
    interrupt: asyncio.Event | None = None,
) -> str | None:
    """Consumes VAD-shaped frames (512 float32 samples each) from the live mic
    stream, re-buffered into the larger int16 chunks WakeWordDetector needs,
    until a configured phrase triggers. Returns the triggered phrase, or None
    if `interrupt` was set (something else - a fired timer - needs the stream).

    Exits by returning, not by cancelling a task - cancelling a task mid-
    `async for` on a shared generator closes it (the bug found while building
    1.11). Only this function's own re-buffering leftover (under 80ms) is
    discarded on trigger; `frames` itself keeps its exact position, so turn
    capture can keep reading from it immediately after.
    """
    buffer = np.zeros(0, dtype=np.float32)
    async for frame in frames:
        if interrupt is not None and interrupt.is_set():
            return None

        buffer = np.concatenate([buffer, frame])
        while buffer.size >= CHUNK_SAMPLES:
            # Clip before scaling: a mic hotter than full scale would otherwise
            # wrap around in the int16 cast and turn loud speech into noise.
            chunk = (np.clip(buffer[:CHUNK_SAMPLES], -1.0, 1.0) * 32767.0).astype(np.int16)
            buffer = buffer[CHUNK_SAMPLES:]
            phrase = detector.process(chunk)
            if phrase is not None:
                return phrase
    raise RuntimeError("mic frame stream ended before a wake word was detected")


class Orchestrator:
    def __init__(
        self,
        wake_detector: WakeWordDetector,
        turn_detector: TurnDetector,
        stt: STTEngine,
        llm: LLMProvider,
        speaker: BargeInSpeaker,
        persona_system_prompt: str,
        follow_up_window_s: float,
        bus: EventBus | None = None,
        memory_max_tokens: int = 6000,
        store: MemoryStore | None = None,
        recent_turns_on_start: int = 8,
        recall_hits: int = 4,
        consolidate: bool = True,
        prewarm: bool = True,
        location: LocationConfig | None = None,
        shortcuts: dict[str, str] | None = None,
        file_roots: tuple = (),
    ):
        self._location = location or LocationConfig(name="", latitude=0.0, longitude=0.0)
        self._shortcuts = shortcuts or {}
        self._file_roots = tuple(file_roots or ())
        # Set per intent: an intent is deterministic unless it says otherwise.
        self._intent_used_llm = False
        self._wake_detector = wake_detector
        self._turn_detector = turn_detector
        self._stt = stt
        self._llm = llm
        self._speaker = speaker
        self._persona_system_prompt = persona_system_prompt
        self._follow_up_window_s = follow_up_window_s
        self._bus = bus
        self._memory_max_tokens = memory_max_tokens
        self._announcements: asyncio.Queue[str] = asyncio.Queue()
        self._announcement_ready = asyncio.Event()
        self._timers = TimerCapability(announce=self._announce)
        self._policy = PermissionPolicy()
        self._router = IntentRouter(self._policy)
        self._register_intents()
        self._frames: AsyncIterator[np.ndarray] | None = None
        self._woke_at: float | None = None
        self._announced_degraded = False
        self._last_match: Match | None = None
        self._last_failure: dict | None = None
        if bus is not None:
            # Subscribed rather than recorded at each call site: every failure
            # anywhere in the app already passes through report_error and onto
            # the bus, including the ones raised inside TTS and summarization.
            bus.subscribe(ERROR_EVENT, self._remember_failure)

        self._store = store
        self._recall_hits = recall_hits
        self._consolidate = consolidate
        self._prewarm = prewarm

        # One memory for the whole run, not one per wake: re-waking continues
        # the conversation rather than starting from nothing, which is what
        # "conversation memory within a session" has to mean once the wake word
        # is how every exchange begins.
        self._memory = ConversationMemory(
            provider=self._llm,
            max_tokens=memory_max_tokens,
            system_prompt=persona_system_prompt,
            bus=bus,
        )
        if self._store is not None and recent_turns_on_start > 0:
            try:
                self._store.start_session()
                prior = self._store.recent_turns(
                    limit=recent_turns_on_start, exclude_session=self._store.session_id
                )
            except Exception:
                # A broken long-term store must not stop Clio from starting at
                # all - fall back to a fresh, in-memory-only conversation.
                log.exception("Long-term memory unavailable, continuing without it")
                self._store = None
                prior = []
            if prior:
                self._memory.seed([{"role": t.role, "content": t.content} for t in prior])
                log.info(
                    "Seeded working memory from long-term store",
                    extra={"extra_fields": {"turns": len(prior)}},
                )

    async def run(self, capture: AudioCapture) -> None:
        self._frames = capture.frames()
        if self._prewarm:
            asyncio.ensure_future(self._warm_up_models())
        while True:
            await self._speak_pending_announcements()

            phrase = await wait_for_wake_word(
                self._wake_detector, self._frames, interrupt=self._announcement_ready
            )
            if phrase is None:
                continue

            # Before STT or the model runs, so the wake word is acknowledged
            # while the slow work happens.
            play_wake_cue()
            self._woke_at = time.monotonic()
            log.info("Wake word triggered", extra={"extra_fields": {"phrase": phrase}})
            if self._bus is not None:
                await self._bus.publish("clio.wake", {"phrase": phrase}, source="clio.orchestrator")
            await self._conversation_loop()

    async def _warm_up_models(self) -> None:
        """Load STT and TTS in the background at startup.

        Both are lazy by design to keep the idle footprint down, but paying for
        both on the first request is several seconds of dead air. Set
        memory.prewarm = false to trade responsiveness for a lighter idle GPU.
        """
        for label, engine in (("stt", self._stt), ("tts", self._speaker.engine)):
            try:
                await engine.warm_up()
                log.info("Pre-warmed model", extra={"extra_fields": {"engine": label}})
            except Exception as exc:
                await report_error(
                    self._bus, exc, context=f"pre-warming {label}", source="clio.orchestrator"
                )

    async def _conversation_loop(self) -> None:
        await self._speak_pending_announcements()
        turn_audio = await self._turn_detector.listen_for_turn(self._frames)
        used_llm = False

        while turn_audio.size > 0:
            text = await self._transcribe(turn_audio)
            if not text:
                break

            heard_at = time.monotonic()
            log.info(
                "User turn",
                extra={
                    "extra_fields": {
                        "text": text,
                        "since_wake_s": round(heard_at - self._woke_at, 2) if self._woke_at else None,
                    }
                },
            )
            self._memory.add_user(text)
            self._record(role="user", content=text)

            reply_text, turn_used_llm = await self._handle_utterance(text)
            used_llm = used_llm or turn_used_llm

            # Where the time actually goes, so "it felt slow" can be diagnosed
            # from the log instead of guessed at.
            log.info(
                "Reply ready",
                extra={
                    "extra_fields": {
                        "think_s": round(time.monotonic() - heard_at, 2),
                        "chars": len(reply_text) if reply_text else 0,
                        "used_llm": turn_used_llm,
                        **(self._last_usage_fields() if turn_used_llm else {}),
                    }
                },
            )

            if reply_text is None:
                # A stop command: say nothing, don't restart the follow-up-window
                # clock through speech that doesn't happen - just keep listening.
                next_turn = await self._listen_silently()
            else:
                session = ConversationSession(self._speaker, follow_up_window_s=self._follow_up_window_s)
                outcome = await session.respond(reply_text, self._frames)

                # What she actually said, not what was generated: barge-in means
                # those differ, and recording the generated text would leave her
                # believing she said things the user never heard.
                heard = (outcome.spoken_text or "").strip()
                if outcome.interrupted:
                    # Worded as "stopped deliberately", not "unfinished": the
                    # latter reads as a prompt to retry the same content.
                    heard = (
                        f"{heard} [he cut you off here - he had heard enough, do not repeat this]"
                        if heard
                        else "[he cut you off before you said anything - drop it and move on]"
                    )
                if heard:
                    self._memory.add_assistant(heard)
                    self._record(role="assistant", content=heard)
                next_turn = outcome.next_turn

            if turn_used_llm:
                await self._memory.trim_if_needed()

            if next_turn is None:
                break
            turn_audio = next_turn

        if used_llm:
            await self._consolidate_memory()

    async def _listen_silently(self) -> np.ndarray | None:
        """The stop-command counterpart to ConversationSession's follow-up
        window: listens for up to follow_up_window_s more without speaking
        anything first. Uses TurnDetector directly rather than BargeInSpeaker,
        since there is no speech in flight to race against or cancel.
        """
        stop = asyncio.Event()
        onset_task = asyncio.ensure_future(self._turn_detector.wait_for_onset(self._frames, stop=stop))
        try:
            onset_frames = await asyncio.wait_for(asyncio.shield(onset_task), timeout=self._follow_up_window_s)
        except asyncio.TimeoutError:
            stop.set()
            onset_frames = await onset_task

        if onset_frames is None:
            return None
        return await self._turn_detector.capture_until_silence(self._frames, onset_frames)

    async def _execute(self, matched: Match) -> str | None:
        """The permission gate. Nothing runs before its tier is checked."""
        if matched.permission is Permission.BLOCKED:
            log.warning("Blocked action refused", extra={"extra_fields": {"intent": matched.intent}})
            return f"I can't do that one. {matched.description} is off limits."

        if matched.permission is Permission.CONFIRM:
            if not await self._confirm(f"{matched.description}. Should I go ahead?"):
                log.info("Action declined", extra={"extra_fields": {"intent": matched.intent}})
                return "Left it alone."

        # Remembered only once it has actually run, so "again" never repeats
        # something that was refused or declined.
        if matched.intent != "repeat":
            self._last_match = matched
        return await matched.run()

    async def _confirm(self, prompt: str) -> bool:
        """Asks out loud and waits for an answer. Silence is a no, as is
        anything that is not an explicit yes.
        """
        session = ConversationSession(self._speaker, follow_up_window_s=self._follow_up_window_s)
        outcome = await session.respond(prompt, self._frames)
        if outcome.next_turn is None:
            return False

        answer = await self._transcribe(outcome.next_turn)
        granted = is_affirmative(answer)
        log.info(
            "Confirmation answered",
            extra={"extra_fields": {"answer": answer, "granted": granted}},
        )
        return granted

    def _register_intents(self) -> None:
        """Everything registered here is handled without an API call.

        Stop is registered first: it is the narrowest match, and the phrases
        used to interrupt her must never be treated as a new request.
        """

        async def stop(_payload: object) -> None:
            return None

        async def start_timer(payload: object) -> str:
            return self._timers.start(float(payload))

        async def repeat(_payload: object) -> str | None:
            if self._last_match is None:
                return "You haven't asked me to do anything yet."
            log.info("Repeating", extra={"extra_fields": {"intent": self._last_match.intent}})
            # Back through the gate, not straight to run(): a confirm-tier action
            # must ask again every time, including when it is being repeated.
            return await self._execute(self._last_match)

        async def diagnose(_payload: object) -> str:
            return explain_failure(self._last_failure)

        async def weather(_payload: object) -> str:
            return await describe_weather(self._location)

        async def currency(payload: object) -> str:
            amount, source, target = payload  # type: ignore[misc]
            return await convert_currency(amount, source, target)

        async def convert_units(payload: object) -> str:
            value, source, target = payload  # type: ignore[misc]
            return format_conversion(value, source, target)

        async def files(payload: object) -> str:
            spoken, to_summarise = await look_up(payload, self._file_roots)  # type: ignore[arg-type]
            if not to_summarise:
                return spoken
            # The only capability that reaches the model, and it says so, so
            # the turn is accounted for honestly rather than counted as free.
            self._intent_used_llm = True
            return await self._llm.complete(
                [
                    {"role": "system", "content": self._persona_system_prompt},
                    {"role": "user", "content":
                        "Say what this file is and what's in it, out loud, in three sentences "
                        f"at most. No lists, no code, no file paths.\n\n{to_summarise}"},
                ],
                tier="default",
            )

        async def control(payload: object) -> str:
            return await apply_control(payload)  # type: ignore[arg-type]

        async def open_thing(payload: object) -> str:
            return open_target(payload)  # type: ignore[arg-type]

        async def system(payload: object) -> str:
            return await describe_system(str(payload))

        async def calculate(payload: object) -> str:
            return f"{format_number(float(payload))}."  # type: ignore[arg-type]

        async def status(_payload: object) -> str:
            # Asked from the registry, not hardcoded: a capability that needs
            # the network must not be listed as working without one.
            offline_ready = ", ".join(
                c.name for c in self._router.capabilities() if c.offline and c.name != "status"
            )
            state = getattr(self._llm, "using_fallback", None)
            if state is None:
                head = "Haven't needed the cloud model yet this session."
            elif state:
                head = "Running on the local model, the cloud one is unreachable."
            else:
                head = "Cloud model is up."
            tracker = getattr(self._llm, "usage", None)
            spend = tracker.summary() if tracker is not None else ""
            return (
                f"{head} Speech, memory and {offline_ready} all work with no network at all. {spend}"
            ).strip()

        self._router.register("repeat", lambda t: True if is_repeat_command(t) else None, repeat)
        self._router.register("diagnose", lambda t: True if is_diagnosis_query(t) else None, diagnose)
        self._router.register("status", lambda t: True if is_status_query(t) else None, status)
        self._router.register("stop", lambda t: True if is_stop_command(t) else None, stop)
        self._router.register("timer", parse_timer_command, start_timer)
        # Currency before units: both say "convert X to Y", and only the
        # currency matcher knows a rupee is not a unit of length.
        self._router.register(
            "weather", lambda t: True if is_weather_query(t) else None, weather, offline=False
        )
        self._router.register("currency", parse_currency_request, currency, offline=False)
        self._router.register("system", parse_system_query, system)
        # Two intents over one module, because they are not the same risk: the
        # describer is what the confirmation actually reads out.
        self._router.register("power", parse_power, control, describe=describe_action)
        self._router.register("control", parse_control, control, describe=describe_action)
        self._router.register(
            "files", lambda t: parse_file_request(t, self._file_roots), files
        )
        self._router.register("convert", parse_conversion, convert_units)
        self._router.register("calculate", parse_calculation, calculate)
        # Last: its verbs are the broadest here, so every narrower matcher -
        # "start a timer" above all - gets the utterance first.
        self._router.register(
            "open", lambda t: resolve_target(t, self._shortcuts), open_thing
        )

    async def _remember_failure(self, event: Event) -> None:
        self._last_failure = {**event.payload, "at": event.timestamp}

    def _record_correction(self, text: str) -> None:
        """A correction becomes a standing rule in his own words, kept beside
        the other durable facts - so it is in front of her every later session,
        before the situation it came from can repeat.
        """
        if self._store is None:
            return
        rule = parse_correction(text)
        if rule is None:
            return
        try:
            if self._store.add_fact(rule, category="Corrections"):
                log.info("Correction recorded", extra={"extra_fields": {"rule": rule}})
        except Exception:
            log.exception("Failed to record correction")

    def _last_usage_fields(self) -> dict:
        """Token counts for the call just made, folded into the turn's log line
        so one record carries what was decided, how long it took and what it
        cost."""
        tracker = getattr(self._llm, "usage", None)
        last = getattr(tracker, "last", None)
        return last.as_fields() if last is not None else {}

    async def _transcribe(self, audio: np.ndarray) -> str:
        try:
            text = await self._stt.transcribe(audio)
        except Exception as exc:
            await report_error(self._bus, exc, context="speech-to-text", source="clio.orchestrator")
            return ""
        return text.strip()

    async def _handle_utterance(self, text: str) -> tuple[str | None, bool]:
        """Returns the reply to speak (None means say nothing at all) and
        whether the LLM was used. The caller records the assistant turn
        afterwards, using what was actually spoken.
        """
        # Before routing: correcting her is also a normal turn, and gets
        # answered like one - recording it must not swallow the reply.
        self._record_correction(text)

        matched = self._router.match(text)
        if matched is not None:
            self._intent_used_llm = False
            spoken = await self._execute(matched)
            # Almost every intent is free, but summarising a file is not, and
            # reporting it as free would under-count the session and skip the
            # end-of-conversation consolidation.
            return spoken, self._intent_used_llm

        # Retrieval happens only on the LLM path - a deterministic command must
        # not touch the store's search or anything else that could cost time.
        if self._store is not None and self._recall_hits > 0:
            try:
                recalled = self._store.recall(
                    text, limit=self._recall_hits, exclude_session=self._store.session_id
                )
                self._memory.set_recalled(recalled or None)
            except Exception as exc:
                # A broken index must degrade the answer (no recalled context this
                # turn), never take down the conversation that asked the question.
                await report_error(self._bus, exc, context="memory recall", source="clio.orchestrator")
                self._memory.set_recalled(None)

        try:
            reply = await self._llm.complete(self._memory.get_messages())
        except Exception as exc:
            described = await report_error(self._bus, exc, context="LLM response", source="clio.orchestrator")
            return described.spoken, False

        if getattr(self._llm, "using_fallback", None) is True:
            if not self._announced_degraded:
                self._announced_degraded = True
                reply = f"Heads up, the cloud model is unreachable so I'm on the local one. {reply}"
        else:
            self._announced_degraded = False

        return reply, True

    def _record(self, role: str, content: str) -> None:
        if self._store is None:
            return
        try:
            self._store.append_turn(role, content)
        except Exception:
            log.exception("Failed to persist turn to long-term memory")

    async def _consolidate_memory(self) -> None:
        """One fast-tier call at the end of a conversation to distil anything
        worth keeping into facts.md. Skipped entirely when the conversation
        never used the LLM, so a timer-only exchange still costs nothing.
        """
        if not self._consolidate or self._store is None:
            return

        transcript = "\n".join(
            f"{m['role']}: {m['content']}" for m in self._memory.get_messages() if m["role"] != "system"
        )
        if not transcript.strip():
            return

        prompt = (
            "From this conversation, list durable facts about the user worth "
            "remembering in future sessions - preferences, ongoing projects, names, "
            "recurring context.\n"
            "Each line must be a complete standalone statement that still makes sense "
            "months from now with no other context. Write 'Prefers to be called boss', "
            "never just 'Boss'. A bare word or fragment is useless later, so skip it.\n"
            "One per line, no bullets, no preamble. Skip anything transient - timers, "
            "one-off questions, whatever was merely discussed rather than true about him. "
            "Most conversations contain nothing worth keeping: reply with nothing at all "
            "in that case, which is the normal outcome.\n\n" + transcript
        )

        try:
            raw = await self._llm.complete([{"role": "user", "content": prompt}], tier="fast")
        except Exception as exc:
            await report_error(
                self._bus, exc, context="memory consolidation", source="clio.orchestrator"
            )
            return

        added = 0
        try:
            for line in raw.splitlines():
                candidate = line.strip()
                if len(candidate) > 3 and self._store.add_fact(candidate):
                    added += 1
        except Exception as exc:
            # Facts not written this round is a shrug, not a crash - the run
            # must survive to keep listening either way.
            await report_error(
                self._bus, exc, context="fact consolidation write", source="clio.orchestrator"
            )
        if added:
            log.info("Consolidated durable facts", extra={"extra_fields": {"added": added}})

    async def _announce(self, text: str) -> None:
        """Queues an unprompted announcement (a fired timer) for the main loop
        to speak. Deliberately does not speak here: this runs on the timer's
        own background task, and speaking would start a second consumer of the
        shared frame stream while the main loop is already reading it, which
        an async generator refuses outright ("anext(): asynchronous generator
        is already running"). The main loop is the sole reader; it drains this
        queue at points where it is not mid-read.
        """
        await self._announcements.put(text)
        self._announcement_ready.set()

    async def _speak_pending_announcements(self) -> None:
        """Speaks whatever is queued. Only ever called from the main loop, so
        the frame stream still has exactly one reader. An announcement raised
        while a conversation is in progress waits until that conversation's
        current turn is done rather than cutting into it.
        """
        while not self._announcements.empty():
            text = await self._announcements.get()
            try:
                await self._speaker.speak(text, self._frames, listen_after_s=0.0)
            except Exception as exc:
                await report_error(
                    self._bus, exc, context="timer announcement", source="clio.orchestrator"
                )
        self._announcement_ready.clear()


def build_orchestrator(config: Config, bus: EventBus | None = None) -> Orchestrator:
    wake_detector = WakeWordDetector(config.wake_word.model_paths(), config.wake_word.threshold)
    vad = VoiceActivityDetector(config.audio.vad_model_path)
    turn_detector = TurnDetector(
        vad,
        threshold=config.audio.vad_threshold,
        min_speech_ms=config.audio.vad_min_speech_ms,
        end_silence_ms=config.audio.vad_end_silence_ms,
    )
    stt = _build_stt(config)
    llm = build_default_provider(config)
    tts_engine = _build_tts(config, bus)
    speaker = BargeInSpeaker(tts_engine, turn_detector)

    return Orchestrator(
        wake_detector=wake_detector,
        turn_detector=turn_detector,
        stt=stt,
        llm=llm,
        speaker=speaker,
        persona_system_prompt=config.persona.system_prompt,
        follow_up_window_s=config.audio.conversation_follow_up_ms / 1000.0,
        bus=bus,
        store=_build_memory_store(config),
        recent_turns_on_start=config.memory.recent_turns_on_start,
        recall_hits=config.memory.recall_hits,
        consolidate=config.memory.consolidate,
        prewarm=config.memory.prewarm,
        location=config.location,
        shortcuts=config.shortcuts,
        file_roots=config.file_roots,
    )


def _build_memory_store(config: Config) -> MemoryStore | None:
    """A broken long-term store (bad path, permissions, disk full) must not
    stop Clio from starting at all - she runs with in-memory-only conversation
    instead. Every later use of the store is already guarded the same way."""
    try:
        return MemoryStore(config.memory.root)
    except Exception:
        log.exception(
            "Long-term memory unavailable at startup, continuing without it",
            extra={"extra_fields": {"root": config.memory.root}},
        )
        return None


def _stt_initial_prompt(config: Config) -> str:
    """Vocabulary hint for the transcriber, built from config rather than
    hardcoded so renaming her or adding a wake phrase keeps it correct.
    Her own name is the single most important word in the system and was the
    one it reliably got wrong.
    """
    name = config.persona.name
    phrases = ", ".join(config.wake_word.phrases)
    return (
        f"This is a conversation with {name}, a voice assistant. "
        f"{name} is spelled {'-'.join(name.upper())}. "
        f"She is addressed as: {phrases}. "
        "Topics include timers, reminders, files, code, and general questions."
    )


def _build_stt(config: Config) -> STTEngine:
    if config.speech.stt_provider == "groq":
        return GroqWhisperEngine(model=config.speech.stt_groq_model)
    return FasterWhisperEngine(
        config.speech.stt_model,
        device=config.speech.stt_device,
        initial_prompt=_stt_initial_prompt(config),
    )


def _build_tts(config: Config, bus: EventBus | None) -> SpeechEngine:
    if config.speech.tts_engine != "kokoro":
        raise ConfigError(
            f"orchestrator: no TTS engine implementation wired for speech.tts_engine='{config.speech.tts_engine}' "
            "(only 'kokoro' is built - see stack decision in ROADMAP.md)"
        )
    return KokoroSpeechEngine(
        config.speech.kokoro_model_path,
        config.speech.kokoro_voices_path,
        config.speech.tts_voice,
        speed=config.speech.tts_speed,
        bus=bus,
        device=config.speech.tts_device,
    )

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

from clio.capabilities.stop import is_stop_command
from clio.capabilities.timer import TimerCapability, parse_timer_command
from clio.core.config import Config, ConfigError
from clio.core.errors import report_error
from clio.core.events import EventBus
from clio.core.logging import get_logger
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
    ):
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
        self._frames: AsyncIterator[np.ndarray] | None = None
        self._woke_at: float | None = None

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
        if is_stop_command(text):
            # The words used to interrupt her must not themselves become a new
            # question - "Shut up." going to the LLM is exactly the bug this
            # exists to prevent. Deterministic, no API call, same as timers.
            log.info("Deterministic stop command, no API call, no reply")
            return None, False

        duration_s = parse_timer_command(text)
        if duration_s is not None:
            log.info("Deterministic timer match, no API call", extra={"extra_fields": {"duration_s": duration_s}})
            return self._timers.start(duration_s), False

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



from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path

import numpy as np

from clio.capabilities.clipboard import Clipboard
from clio.capabilities.correction import parse_correction
from clio.capabilities.notes import NoteBook
from clio.capabilities.registry import register_capabilities
from clio.capabilities.stopwatch import Stopwatch
from clio.capabilities.timer import TimerCapability
from clio.core.config import (
    Config, ConfigError, DictationConfig, HotkeyConfig, LocationConfig, MouseConfig,
    PushToTalkConfig,
)
from clio.core.errors import ERROR_EVENT, describe_error, report_error
from clio.input.hotkey import HotkeyListener
from clio.input.keyboard import KeyListener
from clio.input.mouse import MouseTrigger
from clio.input.typing import type_text
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

# How long a step needs before the next one can see its effect. Only launching
# needs it, and only when something follows.
_SETTLE_AFTER = {"open": 1.5}

# How often to prove the microphone is still delivering while nothing matches.
_WAKE_HEARTBEAT_S = 15.0

# Ceiling on one push-to-talk hold, so a stuck key can't record without end.
_PTT_MAX_S = 30.0


async def wait_for_wake_word(
    detector: WakeWordDetector,
    frames: AsyncIterator[np.ndarray],
    interrupt: asyncio.Event | None = None,
) -> str | None:
    buffer = np.zeros(0, dtype=np.float32)
    # Heartbeat so a silent log is diagnosable: no beat means no frames arriving;
    # a beat with a flat peak means audio is arriving but nothing matched.
    chunks = 0
    peak = 0.0
    last_beat = time.monotonic()

    async for frame in frames:
        if interrupt is not None and interrupt.is_set():
            return None

        now = time.monotonic()
        if now - last_beat >= _WAKE_HEARTBEAT_S:
            log.info(
                "Still listening",
                extra={"extra_fields": {
                    "chunks_since": chunks, "peak_score": round(peak, 3),
                    "threshold": detector._threshold,
                }},
            )
            chunks, peak, last_beat = 0, 0.0, now

        buffer = np.concatenate([buffer, frame])
        while buffer.size >= CHUNK_SAMPLES:
            # Clip before scaling: a mic hotter than full scale would otherwise
            # wrap around in the int16 cast and turn loud speech into noise.
            chunk = (np.clip(buffer[:CHUNK_SAMPLES], -1.0, 1.0) * 32767.0).astype(np.int16)
            buffer = buffer[CHUNK_SAMPLES:]
            phrase = detector.process(chunk)
            chunks += 1
            peak = max(peak, getattr(detector, "last_best", 0.0))
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
        memory_max_tokens: int = 2000,
        store: MemoryStore | None = None,
        recent_turns_on_start: int = 8,
        recall_hits: int = 4,
        consolidate: bool = True,
        prewarm: bool = True,
        location: LocationConfig | None = None,
        hotkey: HotkeyConfig | None = None,
        mouse: MouseConfig | None = None,
        dictation: DictationConfig | None = None,
        push_to_talk: PushToTalkConfig | None = None,
        shortcuts: dict[str, str] | None = None,
        file_roots: tuple = (),
        notes_path: str | None = None,
    ):
        self._location = location or LocationConfig(name="", latitude=0.0, longitude=0.0)
        self._shortcuts = shortcuts or {}
        self._file_roots = tuple(file_roots or ())
        # Set per intent: an intent is deterministic unless it says otherwise.
        self._intent_used_llm = False
        self._stopwatch = Stopwatch()
        # Held so the task is not garbage collected mid-flight.
        self._consolidating: asyncio.Future | None = None
        self._clipboard = Clipboard()
        self._notes = NoteBook(notes_path or "memory/notes.md")
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
        # Hotkey and mouse both feed one manual-trigger path. The flag tells the
        # wake wait its interrupt was a trigger, not a queued announcement, so
        # the loop goes into a conversation; the source is just for the log.
        self._hotkey_config = hotkey
        self._mouse_config = mouse
        self._dictation_config = dictation
        self._ptt_config = push_to_talk
        self._hotkey_listener: HotkeyListener | None = None
        self._mouse_trigger: MouseTrigger | None = None
        self._dictation_listener: HotkeyListener | None = None
        self._ptt_listener: KeyListener | None = None
        self._ptt_down = False
        self._triggered = False
        self._trigger_source = ""
        self._muted = False
        self._timers = TimerCapability(announce=self._announce)
        self._policy = PermissionPolicy()
        self._router = IntentRouter(self._policy)
        self._register_intents()
        self._frames: AsyncIterator[np.ndarray] | None = None
        self._woke_at: float | None = None
        self._announced_degraded = False
        # The last thing actually run, as steps: "again" after a chain has to
        # repeat the chain, not just whichever part happened to come last.
        self._last_plan: list[Match] = []
        self._declined = False
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
        self._start_triggers()
        try:
            await self._run_loop()
        finally:
            for listener in (self._hotkey_listener, self._mouse_trigger,
                             self._dictation_listener, self._ptt_listener):
                if listener is not None:
                    listener.stop()

    async def _run_loop(self) -> None:
        while True:
            await self._speak_pending_announcements()

            log.info("Listening for the wake word")
            phrase = await wait_for_wake_word(
                self._wake_detector, self._frames, interrupt=self._announcement_ready
            )
            if phrase is None:
                if self._triggered:
                    # A trigger, not a queued announcement: drop into a turn.
                    self._triggered = False
                    self._announcement_ready.clear()
                    phrase = self._trigger_source or "trigger"
                else:
                    continue

            if self._muted:
                # Woken while muted: acknowledge nothing, go back to waiting.
                self._announcement_ready.clear()
                continue

            # Before STT or the model runs, so the trigger is acknowledged
            # while the slow work happens.
            play_wake_cue()
            self._woke_at = time.monotonic()
            log.info("Woke", extra={"extra_fields": {"trigger": phrase}})
            if self._bus is not None:
                await self._bus.publish("clio.wake", {"phrase": phrase}, source="clio.orchestrator")
            await self._emit("clio.state", {"state": "listening"})
            if phrase == "dictation":
                await self._dictate_once()
            elif phrase == "ptt":
                await self._ptt_turn()
            else:
                await self._conversation_loop()
            await self._emit("clio.state", {"state": "idle"})

    def _start_triggers(self) -> None:
        hotkey_on = self._hotkey_config is not None and self._hotkey_config.enabled
        mouse_on = self._mouse_config is not None and self._mouse_config.enabled
        dictation_on = self._dictation_config is not None and self._dictation_config.enabled
        ptt_on = self._ptt_config is not None and self._ptt_config.enabled
        if not (hotkey_on or mouse_on or dictation_on or ptt_on):
            return
        loop = asyncio.get_running_loop()
        if hotkey_on:
            listener = HotkeyListener(
                self._hotkey_config.combo, on_press=lambda: self._fire_trigger("hotkey"), loop=loop
            )
            # Start failed (combo already taken): the wake word still works.
            self._hotkey_listener = listener if listener.start() else None
        if mouse_on:
            trigger = MouseTrigger(
                self._mouse_config.button, on_press=lambda: self._fire_trigger("mouse"), loop=loop
            )
            self._mouse_trigger = trigger if trigger.start() else None
        if dictation_on:
            listener = HotkeyListener(
                self._dictation_config.combo, on_press=lambda: self._fire_trigger("dictation"), loop=loop
            )
            self._dictation_listener = listener if listener.start() else None
        if ptt_on:
            listener = KeyListener(
                self._ptt_config.key, on_press=self._ptt_pressed, on_release=self._ptt_released,
                loop=loop,
            )
            self._ptt_listener = listener if listener.start() else None

    def _fire_trigger(self, source: str) -> None:
        """Runs on the loop thread, scheduled from a listener thread. Kept to
        flag writes: the wake wait notices the interrupt on its next frame."""
        if self._muted:
            return
        self._triggered = True
        self._trigger_source = source
        self._announcement_ready.set()

    # --- frontend command surface (5.2/5.4/5.5) ---

    @property
    def muted(self) -> bool:
        return self._muted

    async def set_muted(self, on: bool) -> None:
        """Muted: wake word and triggers are ignored until unmuted."""
        self._muted = bool(on)
        log.info("Mute toggled", extra={"extra_fields": {"muted": self._muted}})
        await self._emit("clio.state", {"state": "muted" if self._muted else "idle"})

    async def inject_text(self, text: str) -> str:
        """Answer a typed message from the chat window. Text in, text out: no
        speech, to avoid a second reader on the microphone stream while the
        voice loop owns it. Returns the reply for the window to show."""
        text = text.strip()
        if not text:
            return ""
        await self._emit("clio.transcript", {"role": "user", "text": text})
        await self._emit("clio.state", {"state": "thinking"})
        self._memory.add_user(text)
        self._record(role="user", content=text)
        reply, _ = await self._handle_utterance(text)
        reply = reply or ""
        if reply:
            self._memory.add_assistant(reply)
            self._record(role="assistant", content=reply)
            await self._emit("clio.transcript", {"role": "assistant", "text": reply})
        await self._emit("clio.state", {"state": "idle"})
        return reply

    def facts(self) -> list[str]:
        return self._store.facts() if self._store is not None else []

    def add_fact(self, text: str) -> bool:
        return bool(self._store is not None and self._store.add_fact(text, category="Notes"))

    def settings(self) -> dict:
        """A snapshot for the settings panel. Live-changeable keys are muted and
        tts_speed; the rest are read-only until the config file changes."""
        return {
            "muted": self._muted,
            "tts_speed": getattr(self._speaker.engine, "speed", None),
            "capabilities": [
                {"name": c.name, "permission": c.permission.value, "offline": c.offline}
                for c in self._router.capabilities()
            ],
            "permissions": self._policy.summary(),
        }

    def set_tts_speed(self, speed: float) -> None:
        engine = self._speaker.engine
        if hasattr(engine, "speed"):
            engine.speed = max(0.7, min(1.5, float(speed)))

    async def _emit(self, name: str, payload: dict | None = None) -> None:
        """Publish a frontend event, if a bus is wired. State and transcript go
        out this way so the HUD reflects what she's doing without polling."""
        if self._bus is not None:
            await self._bus.publish(name, payload or {}, source="clio.orchestrator")

    def _ptt_pressed(self) -> None:
        # Key-down repeats while held; only the first edge starts a turn.
        if self._ptt_down:
            return
        self._ptt_down = True
        self._fire_trigger("ptt")

    def _ptt_released(self) -> None:
        self._ptt_down = False

    async def _dictate_once(self) -> None:
        """Capture one turn and type it into the focused window — dictation is
        transcription, not conversation, so no model and no speech."""
        turn_audio = await self._turn_detector.listen_for_turn(self._frames)
        if turn_audio.size == 0:
            return
        text = await self._transcribe(turn_audio)
        if not text:
            return
        typed = await asyncio.to_thread(type_text, text)
        log.info("Dictated", extra={"extra_fields": {"chars": len(text), "typed": typed}})

    async def _ptt_turn(self) -> None:
        """Hold-to-talk: capture from key-down to key-up (no VAD endpointing),
        then answer it as a normal turn."""
        audio = await self._ptt_capture()
        if audio.size == 0:
            return
        text = await self._transcribe(audio)
        if not text:
            return
        self._memory.add_user(text)
        self._record(role="user", content=text)
        reply_text, used_llm = await self._handle_utterance(text)
        if reply_text:
            # No follow-up window: the next turn is another key-hold, not speech.
            session = ConversationSession(self._speaker, follow_up_window_s=0.0)
            outcome = await session.respond(reply_text, self._frames)
            heard = (outcome.spoken_text or "").strip()
            if heard:
                self._memory.add_assistant(heard)
                self._record(role="assistant", content=heard)
        if used_llm:
            self._consolidating = asyncio.ensure_future(self._consolidate_memory())

    async def _ptt_capture(self) -> np.ndarray:
        """Collect frames while the key is held, capped so a stuck key can't
        record forever."""
        frames: list[np.ndarray] = []
        deadline = time.monotonic() + _PTT_MAX_S
        async for frame in self._frames:
            frames.append(frame)
            if not self._ptt_down or time.monotonic() > deadline:
                break
        return np.concatenate(frames) if frames else np.array([], dtype=np.float32)

    async def _warm_up_models(self) -> None:
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
            await self._emit("clio.transcript", {"role": "user", "text": text})
            await self._emit("clio.state", {"state": "thinking"})

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

            if not reply_text:
                # Stop, or an empty reply (lock/sleep/media): say nothing and keep
                # listening. Speaking an empty string is still a turn with latency.
                next_turn = await self._listen_silently()
            else:
                await self._emit("clio.transcript", {"role": "assistant", "text": reply_text})
                await self._emit("clio.state", {"state": "speaking"})
                session = ConversationSession(self._speaker, follow_up_window_s=self._follow_up_window_s)
                outcome = await session.respond(reply_text, self._frames)

                # What was actually said, not generated — barge-in makes them differ.
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

            if next_turn is None:
                break
            turn_audio = next_turn

        if used_llm:
            # Not awaited: it's a Groq call with retries, and awaiting it here
            # would stop the mic being read meanwhile. It only writes to memory.
            self._consolidating = asyncio.ensure_future(self._consolidate_memory())

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
        self._declined = False
        if matched.permission is Permission.BLOCKED:
            log.warning("Blocked action refused", extra={"extra_fields": {"intent": matched.intent}})
            return f"I can't do that one. {matched.description} is off limits."

        if matched.permission is Permission.CONFIRM:
            if not await self._confirm(f"{matched.description}. Should I go ahead?"):
                log.info("Action declined", extra={"extra_fields": {"intent": matched.intent}})
                # Flagged rather than inferred from the wording, so a chain can
                # stop on a refusal without string-matching its own reply.
                self._declined = True
                return "Left it alone."

        return await matched.run()

    async def _run_plan(self, steps: list[Match]) -> str | None:
        """Runs the steps in order, and stops the moment one does not finish.

        This is the whole of 2.7. A chain that keeps going after a step failed
        leaves him with no idea which half of what he asked for actually
        happened - so a failure or a refusal ends the chain, and the reply says
        what ran before it and what did not run after.

        Each step goes through the permission gate individually. A chain is not
        a way to get a confirm-tier action past its confirmation.
        """
        said: list[str] = []
        # Only what actually ran is remembered, so "again" never re-runs a
        # declined step.
        ran: list[Match] = []
        for index, step in enumerate(steps, start=1):
            if len(steps) > 1:
                log.info(
                    "Plan step",
                    extra={"extra_fields": {
                        "step": index, "of": len(steps), "intent": step.intent,
                        "permission": step.permission.value,
                    }},
                )
            try:
                reply = await self._execute(step)
            except Exception as exc:
                await report_error(
                    self._bus, exc, context=f"step {index}, {step.description}",
                    source="clio.orchestrator",
                )
                self._remember(steps, ran)
                if len(steps) == 1:
                    raise
                return self._stopped_short(said, steps, index, describe_error(exc).spoken)

            if self._declined:
                self._remember(steps, ran)
                if len(steps) == 1:
                    return reply
                return self._stopped_short(said, steps, index, "You said no.")

            ran.append(step)
            if reply:
                said.append(reply)

            # os.startfile returns before the window exists, so let a launch
            # settle before the next step acts on the desktop behind it.
            if index < len(steps) and step.intent in _SETTLE_AFTER:
                await asyncio.sleep(_SETTLE_AFTER[step.intent])

        self._remember(steps, ran)
        return " ".join(said) if said else (reply if len(steps) == 1 else "")

    def _remember(self, steps: list[Match], ran: list[Match]) -> None:
        """A repeat is not itself a thing to repeat, and neither is a step that
        never happened. Nothing is recorded when nothing ran, so the previous
        request stays repeatable."""
        if ran and steps[0].intent != "repeat":
            self._last_plan = ran

    @staticmethod
    def _stopped_short(said: list[str], steps: list[Match], index: int, why: str) -> str:
        """What he needs to hear is the boundary: what is done, and what is not."""
        done = " ".join(said)
        remaining = len(steps) - index
        # Counted, not named: an intent description is a fragment, so "I haven't
        # Locking the screen" reads worse than a number.
        tail = "" if remaining == 0 else (
            " There's one more I haven't done." if remaining == 1
            else f" There are {remaining} more I haven't done."
        )
        return f"{done} Stopped at {steps[index - 1].description}. {why}{tail}".strip()

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
        """Wire every deterministic (no-API) intent onto the router."""
        register_capabilities(self)

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

        steps = self._router.plan(text)
        if steps:
            self._intent_used_llm = False
            spoken = await self._run_plan(steps)
            # Most intents are free; summarising a file isn't, and that must be
            # reported so the session is accounted for.
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

        # Trimmed before the call, not after it: this is the path voice, push-to-
        # talk and typed turns all share, and the prompt going out now is the one
        # counted against the per-minute token budget.
        await self._memory.trim_if_needed()
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

        await self._announcements.put(text)
        self._announcement_ready.set()

    async def _speak_pending_announcements(self) -> None:
  
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
        hotkey=config.hotkey,
        mouse=config.mouse,
        dictation=config.dictation,
        push_to_talk=config.push_to_talk,
        shortcuts=config.shortcuts,
        file_roots=config.file_roots,
        notes_path=str(Path(config.memory.root) / "notes.md"),
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

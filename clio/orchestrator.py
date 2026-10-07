

from __future__ import annotations

import asyncio
import dataclasses
import re
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path

import numpy as np

from clio.capabilities import rephrase, this
from clio.capabilities.assistant import capability_brief
from clio.capabilities.stop import is_stop_command
from clio.capabilities.clipboard import Clipboard
from clio.capabilities.correction import parse_correction
from clio.capabilities.notes import NoteBook
from clio.capabilities.registry import register_capabilities
from clio.capabilities.stopwatch import Stopwatch
from clio.capabilities.remind import ReminderCapability
from clio.capabilities.meeting import STARTED, SUMMARY_ASK, Meeting, addressed, parse_meeting_request
from clio.capabilities.screen import Budget
from clio.speech.loopback import loopback_frames
from clio.capabilities.draft import DraftCapability
from clio.capabilities.filewrite import FileWriter
from clio.capabilities.projects import ProjectCapability
from clio.capabilities.shellcmd import ShellCommands
from clio.capabilities.software import Software
from clio.capabilities.email import EmailCapability
from clio.capabilities.tasks import TaskList
from clio.capabilities.timer import TimerCapability
from clio.core.config import (
    PROJECT_ROOT, Config, ConfigError, DictationConfig, EmailConfig, HotkeyConfig,
    LocationConfig, MouseConfig, PushToTalkConfig,
)
from clio.core import context as front_context
from clio.core.errors import ERROR_EVENT, describe_error, report_error
from clio.input.hotkey import HotkeyListener
from clio.input.keyboard import KeyListener
from clio.input.mouse import MouseTrigger
from clio.input.typing import type_text
from clio.core.events import Event, EventBus
from clio.core.jobs import JobRunner
from clio.core.logging import get_logger
from clio.core.permissions import Permission, PermissionPolicy, is_affirmative
from clio.core.router import IntentRouter, Match
from clio.llm import longform
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
# How long a confirm-tier action typed in the chat window waits for a typed yes.
# Long enough to read what it is about to do, short enough that a yes typed much
# later is answering some other question.
_PENDING_CONFIRM_S = 60.0
# 7.7: active work one request may take, per the brief. A long job inside a
# plan (training) is started and handed to the job runner, not waited on.
_PLAN_BUDGET_S = 15 * 60.0
_COUNTS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six"}

# How long she waits for him to start speaking after a wake, before saying she
# heard nothing. Long enough to gather a thought; not the forever it used to be.
_FIRST_TURN_S = 12.0
# Loudest sample below this while he was meant to be speaking means the mic is
# too quiet, not that he said nothing. Measured: speech through his USB mic at
# 27% peaked near 0.01; a healthy level is 0.1 and up.
_QUIET_PEAK = 0.05

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
        tasks_path: str | None = None,
        email: EmailConfig | None = None,
        projects: dict | None = None,
        config_path: Path | None = None,
        memory_root: str | None = None,
        core_port: int = 8765,
        vision_daily_cap: int = 50,
        meeting_ear=None,
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
        self._task_list = TaskList(tasks_path or "memory/tasks.md")
        # Defaulted, so an orchestrator built without an [email] section still
        # starts; the capability itself says "email isn't set up yet".
        self._email = EmailCapability(email or EmailConfig())
        # Shares the listing: "reply to Priya" means whoever she just read out.
        # Announces through the same queue as timers, because a send finishes
        # ten seconds after the turn that asked for it is over.
        self._draft = DraftCapability(email or EmailConfig(), self._email, self._announce)
        # A confirm-tier action typed in the chat window, waiting for a typed
        # yes. There is no microphone on that path, so _confirm cannot listen.
        self._pending_confirm: tuple[Match, float] | None = None
        self._typed = False
        # Jobs outlive the turn that started them, and Clio herself, so the
        # runner keeps its bookkeeping beside the other memory files.
        self._jobs = JobRunner(memory_root or "memory", announce=self._announce)
        self._projects = ProjectCapability(
            projects or {}, self._file_roots, self._jobs, config_path=config_path)
        # The write half of 3.6, fenced into the same roots the read half uses.
        self._writer = FileWriter(self._file_roots)
        # Spoken commands (7.5): same roots, same projects, and the same job
        # runner, so a slow command becomes a job like any project run.
        self._shell = ShellCommands(self._file_roots, self._projects, self._jobs)
        self._software = Software(self._jobs)
        self._vision_budget = Budget(Path(memory_root or "memory") / "vision_usage.json", vision_daily_cap)
        self._memory_root = Path(memory_root or "memory")
        # 8.5: makes a second turn detector, with its own VAD state, for the
        # call audio. None where there is no audio stack (tests, mostly).
        self._meeting_ear = meeting_ear
        self._meeting: Meeting | None = None
        self._meeting_stop = asyncio.Event()
        # "Undo that" belongs to whichever of files or the clipboard changed
        # something most recently. Empty until one of them does.
        self._last_undoable = ""
        # Reminders live in Task Scheduler, not here; this only needs to know
        # where the words are kept and which port to speak through when one fires.
        self._reminders = ReminderCapability(root=memory_root or "memory", port=core_port)
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
            # The persona, then what she can really do - read from the registry
            # so it cannot go stale. Without it the model made her features up.
            system_prompt=(persona_system_prompt + "\n\n"
                           + capability_brief(self._router.capabilities())),
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
        # A job that ended while she was closed never got announced, and nothing
        # else will ever mention it. Queued before the loop starts, so it is the
        # first thing she says rather than something he has to go looking for.
        for report in self._jobs.missed():
            await self._announce(report)
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

            if phrase == "meeting":
                # Started from the chat window: no wake cue, straight to notes.
                await self._meeting_loop()
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
            if self._meeting is not None:
                await self._meeting_loop()
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

    # --- frontend command surface (5.2/5.4/5.5), and reminders firing (6.4) ---

    async def announce(self, text: str) -> None:
        """Say something she wasn't asked for, at the next gap. A reminder
        arrives here from `clio/remind.py` when Windows fires it, through the
        same queue timers already use."""
        text = text.strip()
        if text:
            await self._announce(text)

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

        self._typed = True
        try:
            pending = self._take_pending()
            if pending is not None and is_affirmative(text):
                # The typed yes *is* the confirmation, so this runs the action
                # itself rather than going back through the gate and asking
                # again. Anything that is not a yes drops it, by the same rule
                # the spoken path uses: only an explicit yes counts.
                log.info("Typed confirmation granted",
                         extra={"extra_fields": {"intent": pending.intent}})
                reply = await pending.run() or ""
            else:
                reply, _ = await self._handle_utterance(text)
                reply = reply or ""
        finally:
            # Reset whatever happened, or one failed typed turn would leave the
            # voice path unable to confirm by ear.
            self._typed = False
        if reply:
            self._memory.add_assistant(reply)
            self._record(role="assistant", content=reply)
            await self._emit("clio.transcript", {"role": "assistant", "text": reply})
        await self._emit("clio.state", {"state": "meeting" if self._meeting is not None else "idle"})
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
        reply, used_llm = await self._stream_utterance(text)
        if reply:
            # No follow-up window: the next turn is another key-hold, not speech.
            session = ConversationSession(self._speaker, follow_up_window_s=0.0)
            outcome = await session.respond(reply, self._frames)
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
        turn_audio = await self._listen_silently(_FIRST_TURN_S)
        if turn_audio is None:
            await self._heard_nothing()
            return
        used_llm = False

        while turn_audio.size > 0:
            text = await self._transcribe(turn_audio)
            if not text:
                break
            await self._emit("clio.mic", {"quiet": False})

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

            reply, turn_used_llm = await self._stream_utterance(text)
            used_llm = used_llm or turn_used_llm
            streamed = reply is not None and not isinstance(reply, str)

            if not streamed:
                # Where the time actually goes, so "it felt slow" can be diagnosed
                # from the log instead of guessed at. A streamed reply logs its
                # own timings once the model has finished.
                log.info(
                    "Reply ready",
                    extra={
                        "extra_fields": {
                            "think_s": round(time.monotonic() - heard_at, 2),
                            "chars": len(reply) if reply else 0,
                            "used_llm": turn_used_llm,
                            **(self._last_usage_fields() if turn_used_llm else {}),
                        }
                    },
                )

            if not reply:
                # Stop, or an empty reply (lock/sleep/media): say nothing and keep
                # listening. Speaking an empty string is still a turn with latency.
                self._close_silent_turn(text)
                next_turn = await self._listen_silently()
            else:
                if not streamed:
                    await self._emit("clio.transcript", {"role": "assistant", "text": reply})
                    await self._emit("clio.state", {"state": "speaking"})
                if self._meeting is not None and isinstance(reply, str):
                    # Meeting notes just started: say so once, with no follow-up
                    # listen - from here the meeting loop owns the microphone.
                    await self._speaker.speak(reply, self._frames, listen_after_s=0.0)
                    self._memory.add_assistant(reply)
                    self._record(role="assistant", content=reply)
                    return
                session = ConversationSession(self._speaker, follow_up_window_s=self._follow_up_window_s)
                outcome = await session.respond(reply, self._frames)

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

    def _close_silent_turn(self, text: str) -> None:
        """After a stop, write down that the stop is finished.

        Found live, 2026-10-02: one "Stop." silenced her for the rest of the
        conversation. Stopping is answered by saying nothing, which left his
        "Stop." in the history with no reply after it - and the persona says
        that when he says stop she says nothing. Every later turn the model
        re-read that and stayed silent: three empty replies in a row, and the
        same three times when replayed. Kept in the long-term record too,
        because the next session is seeded from the last few turns.
        """
        if not is_stop_command(text):
            return
        note = ("[you went quiet, as he asked. That request is finished - "
                "answer whatever he says next as normal]")
        self._memory.add_assistant(note)
        self._record(role="assistant", content=note)

    async def _heard_nothing(self) -> None:
        """She woke and no speech arrived. Found live, 2026-10-01: his mic was
        at 27% in Windows, so audio flowed and nothing in it was loud enough to
        count as speech. She waited without limit, said nothing, and the HUD
        said MIC LIVE. Now the wait ends and she says which it was - in the
        window too, since a broken audio setup may mean he can't hear her."""
        peak = float(getattr(self._turn_detector, "last_peak", 0.0) or 0.0)
        quiet = peak < _QUIET_PEAK
        log.warning("Woke but heard no speech", extra={"extra_fields": {
            "waited_s": _FIRST_TURN_S, "peak": round(peak, 4), "mic_too_quiet": quiet}})
        text = (
            "I can't hear you - your microphone is very quiet. Turn its volume up in "
            "Windows sound settings, under Input."
            if quiet else "I didn't catch anything."
        )
        if quiet:
            await self._emit("clio.mic", {"quiet": True})
        await self._emit("clio.transcript", {"role": "assistant", "text": text})
        try:
            await self._speaker.speak(text, self._frames, listen_after_s=0.0)
        except Exception as exc:
            await report_error(self._bus, exc, context="heard nothing", source="clio.orchestrator")

    async def _listen_silently(self, timeout_s: float | None = None) -> np.ndarray | None:
        """The stop-command counterpart to ConversationSession's follow-up
        window: listens for up to follow_up_window_s more (or timeout_s)
        without speaking anything first. Uses TurnDetector directly rather than
        BargeInSpeaker, since there is no speech in flight to race against or
        cancel.
        """
        stop = asyncio.Event()
        onset_task = asyncio.ensure_future(self._turn_detector.wait_for_onset(self._frames, stop=stop))
        try:
            onset_frames = await asyncio.wait_for(
                asyncio.shield(onset_task), timeout=timeout_s or self._follow_up_window_s)
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
            if self._typed:
                # Typed turns have no microphone to answer with, so the action
                # waits for a typed yes instead of being silently declined -
                # which is what happened to every confirm-tier action from the
                # chat window before 6.8.
                self._pending_confirm = (matched, time.monotonic() + _PENDING_CONFIRM_S)
                self._declined = True
                return f"{matched.description}. Say yes and I'll do it."
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

    def _take_pending(self) -> Match | None:
        """The action waiting on a typed yes, if it hasn't gone stale. Taken
        rather than read: one pending confirmation answers one question."""
        if self._pending_confirm is None:
            return None
        matched, expires = self._pending_confirm
        self._pending_confirm = None
        return matched if time.monotonic() < expires else None

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
        routed = await self._route(text)
        if routed is not None:
            return routed
        if self._meeting is not None:
            # Typed during a call: almost always about the call.
            return await self._about_meeting(text), True
        await self._prepare_prompt(text)
        try:
            reply = await self._llm.complete(self._memory.get_messages())
        except Exception as exc:
            described = await report_error(self._bus, exc, context="LLM response", source="clio.orchestrator")
            return described.spoken, False
        return self._degraded_prefix() + reply, True

    async def _stream_utterance(self, text: str) -> tuple[str | AsyncIterator[str] | None, bool]:
        """The spoken counterpart of _handle_utterance. An intent still answers
        with plain text; an LLM answer comes back as a stream, so speech starts
        on its first sentence instead of waiting for the whole reply."""
        routed = await self._route(text)
        if routed is not None:
            return routed
        await self._prepare_prompt(text)
        return self._reply_stream(self._memory.get_messages()), True

    async def _route(self, text: str) -> tuple[str | None, bool] | None:
        """A deterministic intent's reply and whether it used the LLM, or None
        when nothing matched and the model should answer."""
        # Before routing: correcting her is also a normal turn, and gets
        # answered like one - recording it must not swallow the reply.
        self._record_correction(text)

        if this.wants_context(text):
            resolved = this.resolve(text, await asyncio.to_thread(front_context.snapshot))
            if resolved is not None:
                log.info("Resolved 'this'", extra={"extra_fields": {"as": resolved.why}})
                if resolved.kind == "refuse":
                    return resolved.text, False
                if resolved.kind == "ask":
                    return await self._about_selection(resolved.text), True
                text = resolved.text

        steps = self._router.plan(text)
        self._intent_used_llm = False
        if not steps:
            if rephrase.looks_like_plan(text):
                # Several parts: a plan or nothing. Falling back to a single
                # guess would quietly do half of what he asked.
                planned = await self._planned(text)
                if isinstance(planned, str):
                    return planned, True
                guessed = planned
            else:
                guessed = await self._reworded(text)
            if guessed is None:
                return None
            steps = [guessed]
            self._intent_used_llm = True
        spoken = await self._run_plan(steps)
        # Most intents are free; summarising a file isn't, and that must be
        # reported so the session is accounted for.
        return spoken, self._intent_used_llm

    # --- meeting notes (8.5) ---

    def start_meeting(self) -> str:
        if self._meeting is not None:
            return "I'm already taking notes."
        if self._muted:
            return "I'm muted, so I can't hear the call - unmute me first."
        if self._meeting_ear is None:
            return "I can't hear what the PC plays on this setup, so I can't take call notes."
        self._meeting = Meeting()
        self._meeting_stop = asyncio.Event()
        # From the chat window the voice loop is waiting for the wake word;
        # this wakes it straight into the meeting. By voice, the conversation
        # loop hands over after saying it has started.
        if self._typed:
            self._fire_trigger("meeting")
        log.info("Meeting notes started")
        return STARTED

    def stop_meeting(self) -> str:
        if self._meeting is None:
            return "I'm not taking notes at the moment."
        self._meeting_stop.set()
        return "Stopping - I'll write the notes up."

    async def _meeting_loop(self) -> None:
        """Both sides of the call, transcribed until he says stop: his mic as
        "You", what the PC plays as "Them". She says nothing until it ends."""
        meeting, stop = self._meeting, self._meeting_stop
        if meeting is None:
            return
        await self._emit("clio.state", {"state": "meeting"})
        loopback_stop = threading.Event()
        # One speech-to-text model, two speakers: taken in turn.
        stt_lock = asyncio.Lock()

        async def hear(frames, detector, who: str) -> None:
            while not stop.is_set():
                onset = await detector.wait_for_onset(frames, stop=stop)
                if onset is None:
                    return
                audio = await detector.capture_until_silence(frames, onset)
                async with stt_lock:
                    text = await self._transcribe(audio)
                if not text:
                    continue
                asked = addressed(text) if who == "You" else None
                if asked is not None:
                    await self._meeting_heard(asked)
                    continue
                meeting.add(who, text)
                if meeting.too_long():
                    log.info("Meeting notes hit the time limit")
                    stop.set()

        # Never cancelled: cancelling a reader closes the shared mic stream for
        # every later reader. Both notice `stop` on their next frame.
        them = asyncio.ensure_future(hear(loopback_frames(loopback_stop), self._meeting_ear(), "Them"))
        you = asyncio.ensure_future(hear(self._frames, self._turn_detector, "You"))
        await stop.wait()
        loopback_stop.set()
        await asyncio.gather(you, them, return_exceptions=True)
        self._meeting = None
        await self._emit("clio.state", {"state": "thinking"})
        said = await self._write_up(meeting)
        await self._emit("clio.state", {"state": "speaking"})
        await self._speaker.speak(said, self._frames, listen_after_s=0.0)

    async def _meeting_heard(self, asked: str) -> None:
        """He addressed her mid-call: stop, or a question answered on screen."""
        if parse_meeting_request(asked) == "stop" or parse_meeting_request(f"stop {asked}") == "stop":
            self._meeting_stop.set()
            return
        await self._emit("clio.transcript", {"role": "user", "text": asked})
        reply = await self._about_meeting(asked)
        await self._emit("clio.transcript", {"role": "assistant", "text": reply})

    async def _about_meeting(self, question: str) -> str:
        try:
            return await self._llm.complete(
                [{"role": "system", "content": self._persona_system_prompt},
                 {"role": "user", "content": self._meeting.question_prompt(question)}])
        except Exception as exc:
            described = await report_error(self._bus, exc, context="call question",
                                           source="clio.orchestrator")
            return described.spoken

    async def _write_up(self, meeting: Meeting) -> str:
        """The summary saved, the transcript dropped. If the summary can't be
        written, the transcript is saved instead - losing the whole call to a
        dropped connection would be worse than keeping what he said not to."""
        if not meeting.lines:
            return "Stopped. I didn't catch anything to take notes on, so there's nothing saved."
        root = self._memory_root
        try:
            summary = await longform.summarise(meeting.transcript(), SUMMARY_ASK,
                                               self._persona_system_prompt, self._llm,
                                               max_chunks=40)
        except Exception as exc:
            await report_error(self._bus, exc, context="call summary", source="clio.orchestrator")
            path = await asyncio.to_thread(meeting.save, root, "Summary failed; the transcript, "
                                           "kept so the call isn't lost:\n\n" + meeting.transcript())
            return f"I couldn't write the summary, so I kept the transcript instead, in {path.name}."
        path = await asyncio.to_thread(meeting.save, root, summary)
        await self._emit("clio.transcript", {"role": "assistant", "text": summary})
        # So "what did we decide on that call?" can be answered later.
        self._memory.add_assistant(f"Notes from the call just now: {summary}")
        log.info("Meeting notes saved", extra={"extra_fields": {"file": path.name, "lines": len(meeting.lines)}})
        return "Notes saved. The summary's in the chat window."

    async def _about_selection(self, request: str) -> str:
        """8.3: a question about the text he has selected, answered from that
        text alone - no screenshot, and the selection goes nowhere else."""
        try:
            return await self._llm.complete(
                [{"role": "system", "content": self._persona_system_prompt},
                 {"role": "user", "content": request}])
        except Exception as exc:
            described = await report_error(self._bus, exc, context="selected text",
                                           source="clio.orchestrator")
            return described.spoken

    async def _reworded(self, text: str) -> Match | None:
        """7.6: nothing matched, but it sounds like an order. The model rewords
        it into a sentence the router knows, and the router - not the model -
        decides what that is and whether it may run. Always read back first,
        because it was a guess. Any failure here means ordinary conversation."""
        if not rephrase.looks_like_action(text):
            return None
        offered = self._offered()
        try:
            said = await rephrase.rephrase(self._llm, text, offered)
        except Exception:
            log.exception("Rewording failed")
            return None
        matched = self._as_guess(said, offered, text)
        return None if matched is None else self._read_back(said, matched)

    def _offered(self) -> list[str]:
        """What the model may pick from: the examples that are registered."""
        registered = {c.name for c in self._router.capabilities()}
        return [name for name in rephrase.EXAMPLES if name in registered]

    def _as_guess(self, said: str | None, offered: list[str], heard: str) -> Match | None:
        """The router's reading of the model's sentence, if it is one on offer."""
        matched = self._router.match(said) if said else None
        if matched is None or matched.intent not in offered:
            return None
        # Found live: "start training" became "run python train.py in ewaste",
        # a script nobody named - on one run in three, whatever the prompt said.
        # A file in a command must be one he said, at least by its name.
        for arg in getattr(matched._payload, "argv", [])[1:]:
            stem = re.match(r"^([\w-]+)\.\w+$", arg.replace("\\", "/").split("/")[-1])
            if stem and not re.search(rf"\b{re.escape(stem.group(1))}\b", heard, re.IGNORECASE):
                log.info("Guess named a file he didn't", extra={"extra_fields": {"file": arg}})
                return None
        # "open" claims anything shaped like "run X", so a guess naming nothing
        # installed would only earn "I couldn't find it" after he said yes.
        if matched.intent == "open" and getattr(matched._payload, "kind", "") == "unknown":
            return None
        return matched

    @staticmethod
    def _step_label(said: str, matched: Match) -> str:
        """What will actually happen, when the intent can say; else the sentence."""
        return matched.description if matched.description != matched.intent else said

    @staticmethod
    def _read_back(said: str, matched: Match) -> Match:
        if matched.permission is Permission.BLOCKED:
            return matched
        # The sentence says what she understood; the description says what will
        # actually happen. Both, because they can differ: live, with no projects
        # registered, "run the ewaste project" was going to open a folder.
        readback = f"You mean: {said}"
        if matched.description != matched.intent:
            readback += f". {matched.description}"
        return dataclasses.replace(matched, permission=Permission.CONFIRM, description=readback)

    async def _planned(self, text: str) -> Match | str | None:
        """7.7: an order with several parts the router could not split. The
        reasoning model writes the steps as sentences the router knows; every
        one must match, or nothing runs. The result is one confirm-tier action
        whose readback is the whole plan - so his one yes covers it, through the
        same gate as everything else, typed or spoken. A string is a refusal to
        say; None means conversation."""
        if not rephrase.looks_like_plan(text):
            return None
        offered = self._offered()
        try:
            said = await rephrase.plan(self._llm, text, offered)
        except Exception:
            log.exception("Planning failed")
            return None
        if not said:
            return None
        if len(said) > rephrase.MAX_STEPS:
            return (f"That's {len(said)} steps, more than I'll run in one go. "
                    "Give me it in two halves.")
        steps: list[tuple[str, Match]] = []
        for sentence in said:
            matched = self._as_guess(sentence, offered, text)
            if matched is None:
                # Found live: the likeliest miss is a project he never
                # registered, and that one has a fix he can say.
                project = re.match(r"^run the (.+?) project$", sentence, re.IGNORECASE)
                if project:
                    return (f"{project.group(1)} isn't one of your registered projects yet, so "
                            f"I haven't started any of it. Say register {project.group(1)} first.")
                return (f"I couldn't turn '{sentence}' into something I can do, "
                        "so I haven't started any of it.")
            if matched.permission is Permission.BLOCKED:
                return f"'{sentence}' is off limits, so I haven't started any of it."
            steps.append((sentence, matched))
        if len(steps) == 1:
            return self._read_back(*steps[0])

        count = _COUNTS.get(len(steps), str(len(steps)))
        listed = ". ".join(f"{_COUNTS[i].capitalize()}, {self._step_label(s, m)}"
                           for i, (s, m) in enumerate(steps, start=1))
        log.info("Plan proposed", extra={"extra_fields": {"steps": [m.intent for _, m in steps]}})

        async def run_plan(_payload: object) -> str:
            return await self._run_approved(steps)

        return Match(intent="plan", permission=Permission.CONFIRM,
                     description=f"{count.capitalize()} steps. {listed}",
                     _handler=run_plan, _payload=None)

    async def _run_approved(self, steps: list[tuple[str, Match]]) -> str:
        """Runs an approved plan. His yes covered every step, except one that
        destroys something - that still asks on its own. A command still going
        is waited on, because the next step usually needs it done, up to the
        budget. A failure stops the chain, and the reply says where."""
        deadline = time.monotonic() + _PLAN_BUDGET_S
        said: list[str] = []

        def stopped(index: int, why: str) -> str:
            label = self._step_label(*steps[index - 1])
            remaining = len(steps) - index
            tail = "" if remaining == 0 else (
                " I haven't done the step after it." if remaining == 1
                else f" I haven't done the {_COUNTS.get(remaining, remaining)} steps after it.")
            return f"{' '.join(said)} Stopped at step {index}, {label}. {why}{tail}".strip()

        for index, (sentence, step) in enumerate(steps, start=1):
            label = self._step_label(sentence, step)
            log.info("Plan step", extra={"extra_fields": {
                "step": index, "of": len(steps), "intent": step.intent}})
            await self._emit("clio.transcript", {
                "role": "assistant", "text": f"Step {index} of {len(steps)}: {label}"})

            if getattr(step._payload, "warning", ""):
                if self._typed:
                    return stopped(index, "That one destroys something, so it needs its own yes. "
                                          "Say it on its own.")
                if not await self._confirm(f"{label}. Should I go ahead?"):
                    return stopped(index, "You said no.")

            remaining_s = deadline - time.monotonic()
            if remaining_s <= 0:
                return stopped(index, "That's the time I give one request.")
            try:
                if step.intent in ("shell", "shell_read"):
                    ok, reply, output = await self._shell.run_until(step._payload, remaining_s)
                    if output:
                        await self._emit("clio.transcript", {"role": "assistant", "text": output})
                    if not ok:
                        # None: still going, and carries on as a background job.
                        return stopped(index, reply)
                else:
                    reply = await step.run()
            except Exception as exc:
                await report_error(self._bus, exc, context=f"plan step {index}, {label}",
                                   source="clio.orchestrator")
                return stopped(index, describe_error(exc).spoken)
            if reply:
                said.append(reply)
        return " ".join(said) or "Done, all of it."

    async def _prepare_prompt(self, text: str) -> None:
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

    def _degraded_prefix(self) -> str:
        """Said once when the local model starts answering, then quiet until the
        cloud is back - so the next outage is announced again."""
        if getattr(self._llm, "using_fallback", None) is True:
            if not self._announced_degraded:
                self._announced_degraded = True
                return "Heads up, the cloud model is unreachable so I'm on the local one. "
            return ""
        self._announced_degraded = False
        return ""

    async def _reply_stream(self, messages: list[dict]) -> AsyncIterator[str]:
        """The model's reply as it is written. A failure or the downgrade notice
        is spoken in-line, as _handle_utterance would say it, so the speaker
        never sees an exception. Logs timings and publishes the transcript once
        the model is done."""
        started = time.monotonic()
        first_token_s = None
        parts: list[str] = []
        try:
            async for chunk in self._llm.stream(messages):
                if first_token_s is None:
                    first_token_s = round(time.monotonic() - started, 2)
                    await self._emit("clio.state", {"state": "speaking"})
                    prefix = self._degraded_prefix()
                    if prefix:
                        parts.append(prefix)
                        yield prefix
                parts.append(chunk)
                yield chunk
        except Exception as exc:
            described = await report_error(self._bus, exc, context="LLM response", source="clio.orchestrator")
            if first_token_s is None:
                await self._emit("clio.state", {"state": "speaking"})
            spoken = f" {described.spoken}" if parts else described.spoken
            parts.append(spoken)
            yield spoken

        reply = "".join(parts)
        log.info(
            "Reply ready",
            extra={"extra_fields": {
                "first_token_s": first_token_s,
                "think_s": round(time.monotonic() - started, 2),
                "chars": len(reply),
                "used_llm": True,
                "streamed": True,
                **self._last_usage_fields(),
            }},
        )
        await self._emit("clio.transcript", {"role": "assistant", "text": reply})

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
        if self._meeting is not None:
            # A timer or reminder during a call is shown, never said: it would
            # be heard on the call.
            await self._emit("clio.transcript", {"role": "assistant", "text": text})
            return

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
        tasks_path=str(Path(config.memory.root) / "tasks.md"),
        email=config.email,
        projects=config.projects,
        # Where an approved project gets appended. The same file load_config read.
        config_path=PROJECT_ROOT / "config" / "default.toml",
        memory_root=config.memory.root,
        core_port=config.runtime.core_port,
        vision_daily_cap=config.llm.vision_daily_cap,
        meeting_ear=lambda: TurnDetector(
            VoiceActivityDetector(config.audio.vad_model_path),
            threshold=config.audio.vad_threshold,
            min_speech_ms=config.audio.vad_min_speech_ms,
            end_silence_ms=config.audio.vad_end_silence_ms,
        ),
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

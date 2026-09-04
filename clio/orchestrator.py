"""The actual voice loop: wake word, turn capture, STT, the deterministic
timer fast path or LLM conversation, and speech - with barge-in and
conversation mode threaded through. Every piece up to 1.12 was built and
verified independently; this is where they become one running loop instead
of separate modules.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import numpy as np

from clio.capabilities.timer import TimerCapability, parse_timer_command
from clio.core.config import Config, ConfigError
from clio.core.errors import report_error
from clio.core.events import EventBus
from clio.core.logging import get_logger
from clio.llm.memory import ConversationMemory
from clio.llm.provider import LLMProvider, build_default_provider
from clio.speech.audio_input import AudioCapture, TurnDetector, VoiceActivityDetector
from clio.speech.barge_in import BargeInSpeaker
from clio.speech.conversation import ConversationSession
from clio.speech.stt import FasterWhisperEngine, GroqWhisperEngine, STTEngine
from clio.speech.tts import KokoroSpeechEngine, SpeechEngine
from clio.speech.wake_word import CHUNK_SAMPLES, WakeWordDetector

log = get_logger("clio.orchestrator")


async def wait_for_wake_word(detector: WakeWordDetector, frames: AsyncIterator[np.ndarray]) -> str:
    """Consumes VAD-shaped frames (512 float32 samples each) from the live mic
    stream, re-buffered into the larger int16 chunks WakeWordDetector needs,
    until a configured phrase triggers. Returns the triggered phrase.

    Exits by returning, not by cancelling a task - cancelling a task mid-
    `async for` on a shared generator closes it (the bug found while building
    1.11). Only this function's own re-buffering leftover (under 80ms) is
    discarded on trigger; `frames` itself keeps its exact position, so turn
    capture can keep reading from it immediately after.
    """
    buffer = np.zeros(0, dtype=np.float32)
    async for frame in frames:
        buffer = np.concatenate([buffer, frame])
        while buffer.size >= CHUNK_SAMPLES:
            chunk = (buffer[:CHUNK_SAMPLES] * 32767.0).astype(np.int16)
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
        self._speak_lock = asyncio.Lock()
        self._timers = TimerCapability(announce=self._announce)
        self._frames: AsyncIterator[np.ndarray] | None = None

    async def run(self, capture: AudioCapture) -> None:
        self._frames = capture.frames()
        while True:
            phrase = await wait_for_wake_word(self._wake_detector, self._frames)
            log.info("Wake word triggered", extra={"extra_fields": {"phrase": phrase}})
            if self._bus is not None:
                await self._bus.publish("clio.wake", {"phrase": phrase}, source="clio.orchestrator")
            await self._conversation_loop()

    async def _conversation_loop(self) -> None:
        memory = ConversationMemory(
            provider=self._llm,
            max_tokens=self._memory_max_tokens,
            system_prompt=self._persona_system_prompt,
            bus=self._bus,
        )
        turn_audio = await self._turn_detector.listen_for_turn(self._frames)

        while turn_audio.size > 0:
            text = await self._transcribe(turn_audio)
            if not text:
                break

            log.info("User turn", extra={"extra_fields": {"text": text}})
            reply_text = await self._handle_utterance(text, memory)

            async with self._speak_lock:
                session = ConversationSession(self._speaker, follow_up_window_s=self._follow_up_window_s)
                next_turn = await session.respond(reply_text, self._frames)

            if next_turn is None:
                return
            turn_audio = next_turn

    async def _transcribe(self, audio: np.ndarray) -> str:
        try:
            text = await self._stt.transcribe(audio)
        except Exception as exc:
            await report_error(self._bus, exc, context="speech-to-text", source="clio.orchestrator")
            return ""
        return text.strip()

    async def _handle_utterance(self, text: str, memory: ConversationMemory) -> str:
        duration_s = parse_timer_command(text)
        if duration_s is not None:
            log.info("Deterministic timer match, no API call", extra={"extra_fields": {"duration_s": duration_s}})
            return self._timers.start(duration_s)

        memory.add_user(text)
        try:
            reply = await self._llm.complete(memory.get_messages())
        except Exception as exc:
            described = await report_error(self._bus, exc, context="LLM response", source="clio.orchestrator")
            return described.spoken

        memory.add_assistant(reply)
        await memory.trim_if_needed()
        return reply

    async def _announce(self, text: str) -> None:
        """Speaks an unprompted announcement (a fired timer). Serialized
        through the same lock as conversational replies so two speak() calls
        never run concurrently on the same shared frames stream - a barge-in
        during an announcement stops it, but doesn't itself start a new
        conversation turn (the user would say the wake word again for that).
        """
        if self._frames is None:
            return
        async with self._speak_lock:
            await self._speaker.speak(text, self._frames, listen_after_s=0.0)


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
    )


def _build_stt(config: Config) -> STTEngine:
    if config.speech.stt_provider == "groq":
        return GroqWhisperEngine(model=config.speech.stt_groq_model)
    return FasterWhisperEngine(config.speech.stt_model, device=config.speech.stt_device)


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
    )

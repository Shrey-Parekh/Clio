"""Audio input: mic capture, voice-activity detection, turn endpointing.

Determines when the user started and stopped talking. Downstream consumers (STT,
barge-in) work with whole-turn audio from TurnDetector, not raw frames.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import numpy as np

from clio.core.logging import get_logger

log = get_logger("clio.speech.audio_input")

SAMPLE_RATE = 16000
FRAME_SAMPLES = 512  # required chunk size for the 16kHz Silero VAD model
CONTEXT_SAMPLES = 64  # trailing samples fed back in as left-context - the model is
# trained expecting this; omitting it silently collapses every prediction toward zero
# rather than raising an error. See the reference OnnxWrapper in snakers4/silero-vad.


class VoiceActivityDetector:
    """Wraps the raw Silero VAD ONNX model with its required context/state handling.
    Stateful and single-stream: call reset() before starting a new utterance stream.
    """

    def __init__(self, model_path: str | Path):
        import onnxruntime as ort

        self._session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, CONTEXT_SAMPLES), dtype=np.float32)

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, CONTEXT_SAMPLES), dtype=np.float32)

    def process(self, frame: np.ndarray) -> float:
        """frame: exactly FRAME_SAMPLES float32 mono samples. Returns speech probability [0, 1]."""
        if frame.shape[-1] != FRAME_SAMPLES:
            raise ValueError(f"VAD frame must be {FRAME_SAMPLES} samples, got {frame.shape[-1]}")

        x = np.concatenate([self._context, frame.reshape(1, -1)], axis=1)
        sr = np.array(SAMPLE_RATE, dtype=np.int64)
        out, state = self._session.run(["output", "stateN"], {"input": x, "state": self._state, "sr": sr})
        self._state = state
        self._context = x[:, -CONTEXT_SAMPLES:]
        return float(out[0][0])


class AudioCapture:
    """Mic capture as an async stream of FRAME_SAMPLES float32 frames at SAMPLE_RATE."""

    def __init__(self, device: int | str | None = None):
        self._device = device

    async def frames(self) -> AsyncIterator[np.ndarray]:
        import sounddevice as sd

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[np.ndarray] = asyncio.Queue()

        def _callback(indata, frame_count, time_info, status):
            if status:
                log.warning("Audio input status", extra={"extra_fields": {"status": str(status)}})
            loop.call_soon_threadsafe(queue.put_nowait, indata[:, 0].copy())

        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            blocksize=FRAME_SAMPLES,
            channels=1,
            dtype="float32",
            device=self._device,
            callback=_callback,
        ):
            while True:
                frame = await queue.get()
                yield frame


class TurnDetector:
    """Consumes a frame stream, returns the audio for one complete user turn: from
    speech onset (min_speech_ms of continuous speech, filtering brief noise blips) to
    speech end (end_silence_ms of continuous silence).
    """

    def __init__(
        self,
        vad: VoiceActivityDetector,
        threshold: float = 0.5,
        min_speech_ms: float = 250,
        end_silence_ms: float = 700,
    ):
        self._vad = vad
        self._threshold = threshold
        frame_ms = FRAME_SAMPLES / SAMPLE_RATE * 1000
        self._min_speech_frames = max(1, round(min_speech_ms / frame_ms))
        self._end_silence_frames = max(1, round(end_silence_ms / frame_ms))

    async def wait_for_onset(self, frames: AsyncIterator[np.ndarray]) -> list[np.ndarray] | None:
        """Consume frames until min_speech_ms of continuous speech is detected,
        filtering brief noise blips. Returns the buffered frames from onset
        onward (what capture_until_silence needs to resume from), or None if
        `frames` ended before onset was ever reached. Resets VAD state.
        """
        self._vad.reset()

        pending: list[np.ndarray] = []
        speech_run = 0

        async for frame in frames:
            prob = self._vad.process(frame)
            is_speech = prob >= self._threshold

            pending.append(frame)
            if is_speech:
                speech_run += 1
            else:
                speech_run = 0
                pending = pending[-self._min_speech_frames :]
            if speech_run >= self._min_speech_frames:
                log.debug("Turn started")
                return pending

        return None

    async def capture_until_silence(
        self, frames: AsyncIterator[np.ndarray], onset_frames: list[np.ndarray]
    ) -> np.ndarray:
        """Continue from onset_frames (as returned by wait_for_onset), consuming
        further frames until end_silence_ms of continuous silence. Returns the
        concatenated turn audio.
        """
        speech_frames: list[np.ndarray] = list(onset_frames)
        silence_run = 0

        async for frame in frames:
            prob = self._vad.process(frame)
            is_speech = prob >= self._threshold

            speech_frames.append(frame)
            if is_speech:
                silence_run = 0
            else:
                silence_run += 1
                if silence_run >= self._end_silence_frames:
                    log.debug(
                        "Turn ended",
                        extra={"extra_fields": {"frames": len(speech_frames)}},
                    )
                    break

        return np.concatenate(speech_frames) if speech_frames else np.array([], dtype=np.float32)

    async def listen_for_turn(self, frames: AsyncIterator[np.ndarray]) -> np.ndarray:
        onset_frames = await self.wait_for_onset(frames)
        if onset_frames is None:
            return np.array([], dtype=np.float32)
        return await self.capture_until_silence(frames, onset_frames)

"""Speech-to-text behind a swappable interface.

Benchmarked local faster-whisper (small.en, CUDA) against Groq's hosted
whisper-large-v3-turbo on real synthesized speech, clean and at 5dB SNR: identical
accuracy in both conditions, Groq marginally faster (~0.22s vs ~0.26s mean). Local
is the default - free, private, works offline, and doesn't spend the same Groq
rate-limit budget the LLM calls use. Groq is available as an alternate provider,
useful if the GPU is busy or the model isn't warm yet.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

import numpy as np

from clio.core.cuda import ensure_cuda_dlls_on_path
from clio.core.logging import get_logger
from clio.speech.audio_input import SAMPLE_RATE

log = get_logger("clio.speech.stt")

class STTEngine(ABC):
    @abstractmethod
    async def transcribe(self, audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> str:
        """Transcribe a complete audio segment (e.g. one full user turn) to text."""

    async def warm_up(self) -> None:
        """Load whatever is loaded lazily, ahead of the first real request.
        Default is a no-op - the hosted engine has nothing local to load."""
        return None

    async def transcribe_stream(
        self, audio_chunks: AsyncIterator[np.ndarray], interval_s: float = 1.5
    ) -> AsyncIterator[str]:
        """Yield improving partial transcripts as audio accumulates, by periodically
        re-transcribing the growing buffer. Generic over any transcribe() implementation -
        neither engine here supports true incremental decoding, so this is the practical
        approximation: not free, but cheap enough at typical utterance lengths.
        """
        loop = asyncio.get_running_loop()
        buffer: list[np.ndarray] = []
        last_emit = loop.time()

        async for chunk in audio_chunks:
            buffer.append(chunk)
            now = loop.time()
            if now - last_emit >= interval_s:
                last_emit = now
                text = await self.transcribe(np.concatenate(buffer))
                if text:
                    yield text

        if buffer:
            text = await self.transcribe(np.concatenate(buffer))
            if text:
                yield text


class FasterWhisperEngine(STTEngine):
    """Local STT via faster-whisper. Lazy-loads and warm-keeps the model."""

    def __init__(self, model_size: str, device: str = "cuda", compute_type: str | None = None):
        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type or ("int8_float16" if device == "cuda" else "int8")
        self._model = None

    def _ensure_loaded(self):
        if self._model is None:
            if self._device == "cuda":
                ensure_cuda_dlls_on_path()
            from faster_whisper import WhisperModel

            log.info(
                "Loading faster-whisper model",
                extra={
                    "extra_fields": {
                        "model": self._model_size,
                        "device": self._device,
                        "compute_type": self._compute_type,
                    }
                },
            )
            self._model = WhisperModel(self._model_size, device=self._device, compute_type=self._compute_type)
        return self._model

    async def warm_up(self) -> None:
        # Same reasoning as the TTS side: run one throwaway transcription so the
        # first real utterance doesn't pay model load *and* kernel warm-up.
        loop = asyncio.get_running_loop()
        silence = np.zeros(SAMPLE_RATE // 2, dtype=np.float32)
        await loop.run_in_executor(None, self._transcribe_sync, silence)

    async def transcribe(self, audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._transcribe_sync, audio)

    def _transcribe_sync(self, audio: np.ndarray) -> str:
        model = self._ensure_loaded()
        segments, _info = model.transcribe(audio, language="en")
        return " ".join(segment.text.strip() for segment in segments).strip()


class GroqWhisperEngine(STTEngine):
    """STT via Groq's hosted whisper-large-v3-turbo. Needs network + GROQ_API_KEY."""

    def __init__(self, model: str = "whisper-large-v3-turbo"):
        self._model = model
        self._client = None

    def _ensure_client(self):
        if self._client is None:
            from groq import Groq

            self._client = Groq()
        return self._client

    async def transcribe(self, audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._transcribe_sync, audio, sample_rate)

    def _transcribe_sync(self, audio: np.ndarray, sample_rate: int) -> str:
        import io

        import soundfile as sf

        client = self._ensure_client()
        buf = io.BytesIO()
        sf.write(buf, audio, sample_rate, format="WAV")
        buf.seek(0)
        buf.name = "audio.wav"
        result = client.audio.transcriptions.create(model=self._model, file=buf, language="en")
        return result.text.strip()

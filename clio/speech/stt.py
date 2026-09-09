"""Speech-to-text behind a swappable interface.

Local faster-whisper is the default — free, private, offline, and it doesn't
spend the Groq rate-limit budget the LLM calls use. (Benchmarked as accurate as
Groq's hosted turbo, marginally slower.) Groq is available as an alternate.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod

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
        """Load lazily-loaded models ahead of the first request. No-op by
        default — a hosted engine has nothing local to load."""
        return None


class FasterWhisperEngine(STTEngine):
    """Local STT via faster-whisper. Lazy-loads and warm-keeps the model."""

    def __init__(
        self,
        model_size: str,
        device: str = "cuda",
        compute_type: str | None = None,
        initial_prompt: str | None = None,
    ):
        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type or ("int8_float16" if device == "cuda" else "int8")
        self._initial_prompt = initial_prompt
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
        # One throwaway transcription, so the first real utterance doesn't pay
        # model load and kernel warm-up together.
        loop = asyncio.get_running_loop()
        silence = np.zeros(SAMPLE_RATE // 2, dtype=np.float32)
        await loop.run_in_executor(None, self._transcribe_sync, silence)

    async def transcribe(self, audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._transcribe_sync, audio)

    def _transcribe_sync(self, audio: np.ndarray) -> str:
        model = self._ensure_loaded()
        segments, _info = model.transcribe(
            audio,
            language="en",
            # Biases decoding toward the assistant's name and domain terms,
            # which are otherwise transcribed as commoner near-homophones.
            initial_prompt=self._initial_prompt,
            # Guards Whisper's repetition-loop failure mode on longer audio.
            condition_on_previous_text=False,
        )
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

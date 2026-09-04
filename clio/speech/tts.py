"""Text-to-speech behind a swappable interface. Sentence-level streaming: playback of
sentence N starts as soon as it's rendered, while sentence N+1 renders concurrently.

Single-flight contract: call speak() again, or cancel(), only after the previous
speak() has returned. Overlapping speak() calls are not supported here - that
coordination belongs to whatever drives barge-in (task 1.10), which knows the
actual interrupt semantics the voice loop needs.
"""

from __future__ import annotations

import asyncio
import os
import re
from abc import ABC, abstractmethod
from pathlib import Path

from clio.core.cuda import ensure_cuda_dlls_on_path
from clio.core.errors import report_error
from clio.core.events import EventBus
from clio.core.logging import get_logger

log = get_logger("clio.speech.tts")

_ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "vs", "etc",
    "e.g", "i.e", "st", "vol", "no", "fig", "approx",
}
# Split after sentence-ending punctuation only when followed by whitespace and
# something that looks like a new sentence (capital letter or quote) - avoids
# splitting on things like "3.5 flash" or "192.168.1.1".
_SENTENCE_SPLIT = re.compile(r'(?<=[.!?])\s+(?=[A-Z"\'])')

# What a model writes and what speaks well are different things. Models reach for
# typographic punctuation constantly - em dashes with nothing around them
# ("means—sounds"), curly quotes, ellipses - and the phonemizer either runs the
# words together or tries to pronounce the character itself. Normalising here is
# far more reliable than asking the model not to use them.
_SPEAKABLE = {
    "—": ", ",   # em dash - a comma is the pause it was standing in for
    "–": ", ",   # en dash
    "…": ", ",   # ellipsis
    "’": "'",    # curly apostrophe
    "‘": "'",
    "“": "",     # curly quotes - spoken quotes only add phonemizer noise
    "”": "",
    " ": " ",    # non-breaking space
    "&": " and ",
    "%": " percent",
    "*": "",          # stray markdown emphasis that slipped through
    "#": "",
}


def normalize_for_speech(text: str) -> str:
    """Make text say-able. Kokoro reads what it is given literally, so anything
    typographic has to become either a pause or a word before it gets there."""
    for source, replacement in _SPEAKABLE.items():
        text = text.replace(source, replacement)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)  # no space before punctuation
    text = re.sub(r",\s*,", ",", text)            # collapse doubled commas
    text = re.sub(r",\s*$", ".", text)            # a trailing comma reads as an unfinished thought
    return text.strip()


def split_sentences(text: str) -> list[str]:
    """Split text into speakable chunks, merging back fragments that ended on
    a common abbreviation (so "Dr. Smith called" doesn't split after "Dr.").
    """
    text = normalize_for_speech(text)
    if not text:
        return []

    raw = _SENTENCE_SPLIT.split(text)
    merged: list[str] = []
    for part in raw:
        part = part.strip()
        if not part:
            continue
        if merged:
            last_word = merged[-1].rstrip(".!?").split()[-1].lower() if merged[-1].split() else ""
            if last_word in _ABBREVIATIONS:
                merged[-1] = f"{merged[-1]} {part}"
                continue
        merged.append(part)
    return merged


class SpeechEngine(ABC):
    @abstractmethod
    async def speak(self, text: str) -> str:
        """Speak text sentence by sentence, streaming as each renders.
        Returns when finished, or as soon as possible after cancel() is called.

        Returns the text actually spoken aloud - the whole thing normally, or
        just the sentences that finished playing if cancel() cut it short.
        Callers record that rather than the full text, so Clio never believes
        she said more than the user heard.
        """

    @abstractmethod
    def cancel(self) -> None:
        """Stop speaking immediately. Safe to call at any time, including when idle."""

    async def warm_up(self) -> None:
        """Load whatever is loaded lazily, ahead of the first real request.
        Default is a no-op for engines with nothing to load."""
        return None


class KokoroSpeechEngine(SpeechEngine):
    """Local, offline TTS via Kokoro ONNX. Lazy-loads the model on first use."""

    def __init__(
        self,
        model_path: str | Path,
        voices_path: str | Path,
        voice: str,
        speed: float = 1.0,
        bus: EventBus | None = None,
        device: str = "cuda",
    ):
        self._model_path = str(model_path)
        self._voices_path = str(voices_path)
        self._voice = voice
        self._speed = speed
        self._kokoro = None
        self._cancelled = asyncio.Event()
        self._bus = bus
        self._device = device

    def _ensure_loaded(self):
        if self._kokoro is None:
            # The shipped model is fp16 built for GPU. Left to itself, ONNX Runtime
            # picks TensorRT first (not installed here, and it fails loudly) or
            # quietly lands on CPU, where fp16 runs through generic kernels roughly
            # 30x slower and sounds worse. Pin the provider and put CUDA's DLLs on
            # PATH before the session exists, since it can't be fixed afterwards.
            if self._device == "cuda":
                ensure_cuda_dlls_on_path()
                os.environ.setdefault("ONNX_PROVIDER", "CUDAExecutionProvider")

            from kokoro_onnx import Kokoro

            log.info(
                "Loading Kokoro model",
                extra={
                    "extra_fields": {
                        "model_path": self._model_path,
                        "voice": self._voice,
                        "device": self._device,
                    }
                },
            )
            self._kokoro = Kokoro(self._model_path, self._voices_path)
        return self._kokoro

    async def warm_up(self) -> None:
        # Loading the model isn't enough: the first inference still pays CUDA
        # kernel compilation (~2s). A throwaway synthesis moves that cost here,
        # off the first thing the user actually asks for.
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._synthesize, "Ready.")

    def _synthesize(self, sentence: str):
        kokoro = self._ensure_loaded()
        return kokoro.create(sentence, voice=self._voice, speed=self._speed, lang="en-us")

    async def _render_racing_cancel(self, sentence: str):
        """Run synthesis in a thread, but don't block on it if cancel() fires first.
        Synthesis is CPU-bound (1-2s) and can't be preempted once started - the thread
        keeps running to completion regardless, but the coroutine stops waiting on it.
        Returns None if cancellation won the race or synthesis failed.
        """
        loop = asyncio.get_running_loop()
        render_task = loop.run_in_executor(None, self._synthesize, sentence)
        cancel_wait = asyncio.ensure_future(self._cancelled.wait())
        try:
            done, _pending = await asyncio.wait(
                {render_task, cancel_wait}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            cancel_wait.cancel()

        if render_task not in done:
            render_task.cancel()
            return None
        try:
            return render_task.result()
        except Exception as exc:
            await report_error(self._bus, exc, context=f"TTS synthesis for {sentence!r}", source="clio.speech.tts")
            return None

    async def speak(self, text: str) -> str:
        sentences = split_sentences(text)
        if not sentences:
            return ""

        self._cancelled.clear()
        spoken: list[str] = []

        next_audio = await self._render_racing_cancel(sentences[0])
        if next_audio is None:
            return ""

        for i in range(len(sentences)):
            if self._cancelled.is_set():
                break

            samples, sample_rate = next_audio

            render_coro = None
            if i + 1 < len(sentences):
                render_coro = asyncio.ensure_future(self._render_racing_cancel(sentences[i + 1]))

            # Only counted as spoken once it has played all the way through. A
            # sentence cut off partway is left out: under-reporting by at most
            # one sentence beats claiming whole sentences the user never heard.
            if await self._play(samples, sample_rate):
                spoken.append(sentences[i])

            if render_coro is not None:
                if self._cancelled.is_set():
                    render_coro.cancel()
                    break
                next_audio = await render_coro
                if next_audio is None:
                    break

        return " ".join(spoken)

    async def _play(self, samples, sample_rate: int) -> bool:
        """Returns True if playback ran to completion, False if it was cancelled
        before starting or partway through."""
        import sounddevice as sd

        if self._cancelled.is_set():
            return False

        sd.play(samples, sample_rate)
        try:
            while True:
                stream = sd.get_stream()
                if stream is None or not stream.active:
                    return True
                if self._cancelled.is_set():
                    sd.stop()
                    return False
                await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            sd.stop()
            raise

    def cancel(self) -> None:
        self._cancelled.set()
        try:
            import sounddevice as sd

            sd.stop()
        except Exception:
            pass

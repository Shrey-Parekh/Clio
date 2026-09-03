"""Text-to-speech behind a swappable interface. Sentence-level streaming: playback of
sentence N starts as soon as it's rendered, while sentence N+1 renders concurrently.

Single-flight contract: call speak() again, or cancel(), only after the previous
speak() has returned. Overlapping speak() calls are not supported here - that
coordination belongs to whatever drives barge-in (task 1.10), which knows the
actual interrupt semantics the voice loop needs.
"""

from __future__ import annotations

import asyncio
import re
from abc import ABC, abstractmethod
from pathlib import Path

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


def split_sentences(text: str) -> list[str]:
    """Split text into speakable chunks, merging back fragments that ended on
    a common abbreviation (so "Dr. Smith called" doesn't split after "Dr.").
    """
    text = text.strip()
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
    async def speak(self, text: str) -> None:
        """Speak text sentence by sentence, streaming as each renders.
        Returns when finished, or as soon as possible after cancel() is called.
        """

    @abstractmethod
    def cancel(self) -> None:
        """Stop speaking immediately. Safe to call at any time, including when idle."""


class KokoroSpeechEngine(SpeechEngine):
    """Local, offline TTS via Kokoro ONNX. Lazy-loads the model on first use."""

    def __init__(self, model_path: str | Path, voices_path: str | Path, voice: str, speed: float = 1.0):
        self._model_path = str(model_path)
        self._voices_path = str(voices_path)
        self._voice = voice
        self._speed = speed
        self._kokoro = None
        self._cancelled = asyncio.Event()

    def _ensure_loaded(self):
        if self._kokoro is None:
            from kokoro_onnx import Kokoro

            log.info(
                "Loading Kokoro model",
                extra={"extra_fields": {"model_path": self._model_path, "voice": self._voice}},
            )
            self._kokoro = Kokoro(self._model_path, self._voices_path)
        return self._kokoro

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
        except Exception:
            log.error("TTS synthesis failed", exc_info=True, extra={"extra_fields": {"sentence": sentence}})
            return None

    async def speak(self, text: str) -> None:
        sentences = split_sentences(text)
        if not sentences:
            return

        self._cancelled.clear()

        next_audio = await self._render_racing_cancel(sentences[0])
        if next_audio is None:
            return

        for i in range(len(sentences)):
            if self._cancelled.is_set():
                break

            samples, sample_rate = next_audio

            render_coro = None
            if i + 1 < len(sentences):
                render_coro = asyncio.ensure_future(self._render_racing_cancel(sentences[i + 1]))

            await self._play(samples, sample_rate)

            if render_coro is not None:
                if self._cancelled.is_set():
                    render_coro.cancel()
                    break
                next_audio = await render_coro
                if next_audio is None:
                    break

    async def _play(self, samples, sample_rate: int) -> None:
        import sounddevice as sd

        if self._cancelled.is_set():
            return

        sd.play(samples, sample_rate)
        try:
            while True:
                stream = sd.get_stream()
                if stream is None or not stream.active:
                    break
                if self._cancelled.is_set():
                    sd.stop()
                    break
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

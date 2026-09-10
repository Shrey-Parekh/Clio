"""Text-to-speech behind a swappable interface. Sentence-level streaming:
sentence N plays while N+1 renders.

Single-flight: call speak() again, or cancel(), only after the previous speak()
returns. Overlapping calls are the barge-in driver's job to coordinate.
"""

from __future__ import annotations

import asyncio
import os
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
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
    "‑": "-",    # non-breaking hyphen - a plain hyphen phonemizes fine, this doesn't
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
    """Make text say-able: Kokoro reads literally, so typographic marks must
    become a pause or a word first."""
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


def _sentence_end(text: str) -> int:
    """Where the last complete sentence in a growing buffer ends, or 0 if none
    has yet. A boundary only counts once the next sentence has started, so the
    tail is always held back for more text, and "Dr." never ends one - the same
    abbreviation rule split_sentences merges on."""
    end = 0
    for match in _SENTENCE_SPLIT.finditer(text):
        words = text[: match.start()].rstrip(".!?").split()
        if words and words[-1].lower() in _ABBREVIATIONS:
            continue
        end = match.end()
    return end


async def _feed_sentences(text: str | AsyncIterator[str], out: asyncio.Queue) -> None:
    """Put speakable sentences on `out` as each one completes, then None. A
    string is split at once; a stream is split as it grows, so the first
    sentence can be spoken while the model is still writing the rest."""
    try:
        if isinstance(text, str):
            for sentence in split_sentences(text):
                out.put_nowait(sentence)
            return
        buffer = ""
        async for chunk in text:
            buffer += chunk
            end = _sentence_end(buffer)
            if end:
                for sentence in split_sentences(buffer[:end]):
                    out.put_nowait(sentence)
                buffer = buffer[end:]
        for sentence in split_sentences(buffer):
            out.put_nowait(sentence)
    finally:
        out.put_nowait(None)


class SpeechEngine(ABC):
    @abstractmethod
    async def speak(self, text: str | AsyncIterator[str]) -> str:
        """Speak sentence by sentence as each renders. `text` may be a stream the
        model is still writing: each sentence is spoken once complete. Returns the
        text actually spoken — all of it, or only the sentences that finished
        playing if cancel() cut it short, so callers never record more than the
        user heard."""

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

    @property
    def speed(self) -> float:
        return self._speed

    @speed.setter
    def speed(self, value: float) -> None:
        """Settable so "slow down" takes effect mid-conversation. Not persisted."""
        self._speed = value

    def _ensure_loaded(self):
        if self._kokoro is None:
            # fp16 GPU model: left alone, ONNX Runtime lands on CPU (~30x slower,
            # worse sounding). Pin CUDA and put its DLLs on PATH before the
            # session exists — it can't be fixed afterwards.
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
        """Synthesise in a thread, but stop waiting if cancel() fires first (the
        thread still runs to completion; it can't be preempted). None if cancel
        won the race or synthesis failed."""
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

    async def _next_sentence(self, sentences: asyncio.Queue) -> str | None:
        """The next complete sentence, or None once the text has ended or
        cancel() fired while waiting for the model to write more."""
        get = asyncio.ensure_future(sentences.get())
        cancel_wait = asyncio.ensure_future(self._cancelled.wait())
        try:
            done, _pending = await asyncio.wait({get, cancel_wait}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            cancel_wait.cancel()
            if not get.done():
                get.cancel()
        # Decided by what finished, not get.cancelled(): cancel() only takes effect
        # on the next loop step, so right after it the task is neither cancelled nor
        # done, and reading its result raised - cutting her off before her first
        # sentence crashed the whole run.
        return get.result() if get in done else None

    async def _next_rendered(self, sentences: asyncio.Queue):
        """(sentence, audio) for the next sentence, or None if the text ended,
        cancel() fired, or synthesis failed."""
        sentence = await self._next_sentence(sentences)
        if sentence is None:
            return None
        audio = await self._render_racing_cancel(sentence)
        return None if audio is None else (sentence, audio)

    async def speak(self, text: str | AsyncIterator[str]) -> str:
        self._cancelled.clear()
        # Read eagerly into an unbounded queue, so the model never waits on
        # playback and the whole reply is known as early as possible.
        sentences: asyncio.Queue = asyncio.Queue()
        reader = asyncio.ensure_future(_feed_sentences(text, sentences))
        spoken: list[str] = []
        ahead = None
        try:
            current = await self._next_rendered(sentences)
            while current is not None and not self._cancelled.is_set():
                sentence, (samples, sample_rate) = current
                # Render the next sentence while this one plays.
                ahead = asyncio.ensure_future(self._next_rendered(sentences))

                # Counted as spoken only once fully played — under-report by a
                # sentence rather than claim one the user never heard.
                if await self._play(samples, sample_rate):
                    spoken.append(sentence)
                if self._cancelled.is_set():
                    break
                current = await ahead
                ahead = None
        finally:
            # Cut off: stop rendering ahead and stop reading the model.
            for task in (ahead, reader):
                if task is not None and not task.done():
                    task.cancel()

        if reader.done() and not reader.cancelled() and reader.exception() is not None:
            await report_error(
                self._bus, reader.exception(), context="reading the reply to speak", source="clio.speech.tts"
            )
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

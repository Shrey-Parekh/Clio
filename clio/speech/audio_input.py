"""Audio input: mic capture, voice-activity detection, turn endpointing.

Determines when the user started and stopped talking. Downstream consumers (STT,
barge-in) work with whole-turn audio from TurnDetector, not raw frames.
"""

from __future__ import annotations

import asyncio
import threading
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


# A live mic sends a block every 32ms even in a silent room, so this long with
# nothing while a reader waits means the stream has stalled. Seen on a USB mic:
# the driver hiccuped, recovered for new streams, and left the open one dead,
# so the wake word went deaf with no error anywhere.
_STALL_S = 3.0


# Opening a stream normally takes well under a second. Found live, 2026-09-30:
# after a boot, the Windows Audio service had stopped answering, and opening
# the mic never returned - on the event loop, so the wake word, the hotkey and
# the chat window all froze with nothing in the log. Now the open runs on its
# own thread: past _OPEN_S she says so and carries on; a fresh attempt every
# _RETRY_S, since a restarted audio service answers new calls, not stuck ones.
_OPEN_S = 10.0
_RETRY_S = 60.0
# ponytail: each stuck attempt keeps its thread until Windows lets go of it;
# capped so a service that never recovers can't pile them up.
_MAX_STUCK = 5
_STUCK_SAID = ("My microphone isn't answering - Windows' audio service looks stuck. "
               "Restarting Windows Audio, or the PC, usually fixes it. I'll keep trying.")


class AudioCapture:
    """Mic capture as an async stream of FRAME_SAMPLES float32 frames at SAMPLE_RATE.
    Reopens the stream if it stops delivering, so a driver hiccup heals itself."""

    def __init__(self, device: int | str | None = None, bus=None):
        self._device = device
        self._bus = bus

    async def _tell(self, text: str) -> None:
        """Into the HUD and chat window, since nothing can be said out loud
        without the audio service either."""
        if self._bus is not None:
            await self._bus.publish("clio.transcript", {"role": "assistant", "text": text},
                                    source="clio.audio")

    async def _open(self, sd, callback):
        """A started stream, however long Windows takes to give one."""
        loop = asyncio.get_running_loop()
        stuck = 0
        said = False
        while True:
            opened: asyncio.Future = loop.create_future()

            def attempt(opened=opened):
                try:
                    stream = sd.InputStream(
                        samplerate=SAMPLE_RATE, blocksize=FRAME_SAMPLES, channels=1,
                        dtype="float32", device=self._device, callback=callback)
                    stream.start()
                except Exception as exc:
                    loop.call_soon_threadsafe(
                        lambda: opened.done() or opened.set_exception(exc))
                    return
                # Too late to be used: close it rather than leak an open mic.
                loop.call_soon_threadsafe(
                    lambda: stream.close() if opened.done() else opened.set_result(stream))

            threading.Thread(target=attempt, name="mic-open", daemon=True).start()
            try:
                stream = await asyncio.wait_for(asyncio.shield(opened), timeout=_OPEN_S)
            except asyncio.TimeoutError:
                stuck += 1
                log.error("Microphone didn't open", extra={"extra_fields": {
                    "device": self._device, "waited_s": _OPEN_S, "stuck_attempts": stuck}})
                if not said:
                    await self._tell(_STUCK_SAID)
                    said = True
                opened.cancel()
                if stuck >= _MAX_STUCK:
                    # No more threads: wait for Windows to let one of them go.
                    log.error("Microphone still stuck, not trying again until restart")
                    await asyncio.Event().wait()
                await asyncio.sleep(_RETRY_S)
                continue
            except Exception:
                log.exception("Microphone failed to open", extra={"extra_fields": {
                    "device": self._device}})
                if not said:
                    await self._tell(_STUCK_SAID)
                    said = True
                await asyncio.sleep(_RETRY_S)
                continue
            if said:
                log.info("Microphone is back")
                await self._tell("My microphone is working again.")
            return stream

    async def frames(self) -> AsyncIterator[np.ndarray]:
        import sounddevice as sd

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[np.ndarray] = asyncio.Queue()

        def _callback(indata, frame_count, time_info, status):
            if status:
                log.warning("Audio input status", extra={"extra_fields": {"status": str(status)}})
            loop.call_soon_threadsafe(queue.put_nowait, indata[:, 0].copy())

        while True:
            stream = await self._open(sd, _callback)
            try:
                while True:
                    try:
                        frame = await asyncio.wait_for(queue.get(), timeout=_STALL_S)
                    except asyncio.TimeoutError:
                        log.warning(
                            "Microphone stopped sending audio, reopening it",
                            extra={"extra_fields": {"device": self._device, "silent_s": _STALL_S}},
                        )
                        break
                    yield frame
            finally:
                # Closed off the loop: a stalled driver can hang close(), and that
                # must not freeze Clio. ponytail: a close that never returns keeps
                # one executor thread, fine unless the mic stalls constantly.
                loop.run_in_executor(None, stream.close)


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

    async def wait_for_onset(
        self, frames: AsyncIterator[np.ndarray], stop: asyncio.Event | None = None
    ) -> list[np.ndarray] | None:
        """Consume frames until min_speech_ms of continuous speech, filtering
        brief blips. Returns the frames from onset onward, or None if `frames`
        ended or `stop` was set first. `stop` lets a caller give up without
        cancelling this coroutine (which would close the shared frame generator
        for every later reader). Resets VAD state."""
        self._vad.reset()

        pending: list[np.ndarray] = []
        speech_run = 0

        async for frame in frames:
            if stop is not None and stop.is_set():
                return None

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

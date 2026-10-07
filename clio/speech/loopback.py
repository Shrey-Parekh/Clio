"""What the PC is playing, as a frame stream (8.5): the other side of a call.

WASAPI loopback - recording an output device as if it were a microphone - is a
documented Windows mode, no driver involved. The installed PortAudio build
can't open it, so this uses `soundcard`, which can. Frames match the mic's
(16kHz mono float32, FRAME_SAMPLES long), so the same VAD and turn detection
cut it into sentences.

Nothing here is written anywhere: frames go to speech-to-text and are dropped.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator

import numpy as np

from clio.core.logging import get_logger
from clio.speech.audio_input import FRAME_SAMPLES, SAMPLE_RATE

log = get_logger("clio.speech.loopback")


async def loopback_frames(stop: threading.Event) -> AsyncIterator[np.ndarray]:
    """Frames of the default output device until `stop` is set. The recorder
    blocks, so it runs on its own thread and hands frames to the loop."""
    import soundcard

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[np.ndarray | None] = asyncio.Queue()

    def _record() -> None:
        try:
            speaker = soundcard.default_speaker()
            device = soundcard.get_microphone(speaker.name, include_loopback=True)
            log.info("Listening to what the PC plays", extra={"extra_fields": {"device": speaker.name}})
            with device.recorder(samplerate=SAMPLE_RATE, channels=1, blocksize=FRAME_SAMPLES) as recorder:
                while not stop.is_set():
                    block = recorder.record(numframes=FRAME_SAMPLES)
                    loop.call_soon_threadsafe(queue.put_nowait, block[:, 0].astype(np.float32))
        except Exception:
            log.exception("Loopback capture failed")
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    threading.Thread(target=_record, name="clio-loopback", daemon=True).start()
    while (frame := await queue.get()) is not None:
        yield frame

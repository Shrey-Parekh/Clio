"""Short non-speech audio cues.

The wake chime exists because of a real finding from the first live mic test:
the gap between saying the wake word and hearing anything back is dominated by
STT and the model, and with no feedback in between you assume it didn't hear
you and just wait. Twelve of the twenty seconds in that first test were the
user waiting on silence. A chime costs nothing - synthesized numpy, no model,
no network - and it fires the instant the wake word matches.
"""

from __future__ import annotations

import numpy as np

from clio.core.logging import get_logger

log = get_logger("clio.speech.cues")

CUE_SAMPLE_RATE = 24000


def _tone(freq_hz: float, duration_s: float, sample_rate: int = CUE_SAMPLE_RATE) -> np.ndarray:
    t = np.linspace(0, duration_s, int(sample_rate * duration_s), endpoint=False)
    wave = np.sin(2 * np.pi * freq_hz * t)

    # Fade the edges or it clicks - a hard start/stop on a sine is a step change.
    fade = max(1, int(sample_rate * 0.008))
    envelope = np.ones_like(wave)
    envelope[:fade] = np.linspace(0.0, 1.0, fade)
    envelope[-fade:] = np.linspace(1.0, 0.0, fade)
    return (wave * envelope).astype(np.float32)


def wake_cue(volume: float = 0.25) -> np.ndarray:
    """Two rising notes - short enough not to delay anything, distinct enough
    to read as "I'm listening" rather than as part of a reply."""
    return np.concatenate([_tone(660.0, 0.07), _tone(880.0, 0.09)]) * volume


def play_wake_cue(volume: float = 0.25) -> None:
    """Fire and forget: starts playback and returns immediately, so the cue
    never sits between the wake word and listening for the actual request.
    A failure here is cosmetic - log it and carry on rather than derailing
    a turn over a chime.
    """
    try:
        import sounddevice as sd

        sd.play(wake_cue(volume), CUE_SAMPLE_RATE)
    except Exception:
        log.warning("Wake cue failed to play", exc_info=True)

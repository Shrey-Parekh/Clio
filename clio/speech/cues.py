"""Short non-speech audio cues — the wake chime acknowledges the wake word while
STT and the model are still working, so there's feedback within a second."""

from __future__ import annotations

import numpy as np

from clio.core.logging import get_logger

log = get_logger("clio.speech.cues")

CUE_SAMPLE_RATE = 24000


def _tone(freq_hz: float, duration_s: float, sample_rate: int = CUE_SAMPLE_RATE) -> np.ndarray:
    t = np.linspace(0, duration_s, int(sample_rate * duration_s), endpoint=False)
    wave = np.sin(2 * np.pi * freq_hz * t)

    # Fade the edges or a hard start/stop on the sine clicks.
    fade = max(1, int(sample_rate * 0.008))
    envelope = np.ones_like(wave)
    envelope[:fade] = np.linspace(0.0, 1.0, fade)
    envelope[-fade:] = np.linspace(1.0, 0.0, fade)
    return (wave * envelope).astype(np.float32)


def wake_cue(volume: float = 0.25) -> np.ndarray:
    """Two rising notes — short enough not to delay the turn, distinct from speech."""
    return np.concatenate([_tone(660.0, 0.07), _tone(880.0, 0.09)]) * volume


def play_wake_cue(volume: float = 0.25) -> None:
    """Start playback and return immediately. Failure is cosmetic, so log it."""
    try:
        import sounddevice as sd

        sd.play(wake_cue(volume), CUE_SAMPLE_RATE)
    except Exception:
        log.warning("Wake cue failed to play", exc_info=True)

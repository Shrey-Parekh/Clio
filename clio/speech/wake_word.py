"""Wake word detection via openWakeWord. Runs continuously on the mic frame stream;
fires when any configured phrase model crosses its threshold.

Each configured phrase gets its own trained model, but detection stays cheap regardless
of phrase count: openWakeWord shares the expensive melspectrogram/embedding computation
across all loaded models, so each additional phrase only adds a small classifier head.
Measured on this machine: 1 model = 1.95% of one core, 6 models = 2.28%.
"""

from __future__ import annotations

from clio.core.config import WakeWordConfig
from clio.core.logging import get_logger

log = get_logger("clio.speech.wake_word")

CHUNK_SAMPLES = 1280  # 80ms @ 16kHz - openWakeWord's expected inference chunk size


class WakeWordDetector:
    def __init__(self, model_paths: dict[str, str], threshold: float = 0.5):
        """model_paths: {phrase: onnx_path}, e.g. from WakeWordConfig.model_paths()."""
        self._model_paths = model_paths
        self._threshold = threshold
        self._slug_to_phrase = {WakeWordConfig.slug(phrase): phrase for phrase in model_paths}
        self._model = None

    def _ensure_loaded(self):
        if self._model is None:
            from openwakeword.model import Model

            log.info(
                "Loading wake word models",
                extra={"extra_fields": {"phrases": list(self._model_paths), "threshold": self._threshold}},
            )
            self._model = Model(wakeword_models=list(self._model_paths.values()), inference_framework="onnx")
        return self._model

    def reset(self) -> None:
        if self._model is not None:
            self._model.reset()

    last_best: float = 0.0

    def process(self, chunk) -> str | None:
        """chunk: CHUNK_SAMPLES int16 mono samples @ 16kHz.
        Returns the triggered phrase (e.g. "Hey Clio"), or None if nothing crossed threshold.
        If multiple models cross threshold in the same chunk, returns the highest-scoring one.
        """
        if chunk.shape[-1] != CHUNK_SAMPLES:
            raise ValueError(f"Wake word chunk must be {CHUNK_SAMPLES} samples, got {chunk.shape[-1]}")

        model = self._ensure_loaded()
        predictions = model.predict(chunk)
        # Kept so a caller can tell "no audio is arriving" from "audio is
        # arriving and nothing sounds like her name" - two failures that look
        # identical from outside and need opposite fixes.
        # float(), not the numpy scalar predict() returns - the JSON log
        # handler cannot serialise float32 and drops the record entirely.
        self.last_best = float(max(predictions.values(), default=0.0))

        triggered_slug = None
        best_score = self._threshold
        for slug, score in predictions.items():
            if score >= best_score:
                best_score = score
                triggered_slug = slug

        if triggered_slug is None:
            return None
        return self._slug_to_phrase.get(triggered_slug, triggered_slug)

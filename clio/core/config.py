"""Typed config: TOML defaults, overridden by CLIO_* env vars, secrets read from .env."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}
_VALID_TTS_ENGINES = {"edge", "kokoro", "elevenlabs", "gemini"}
_VALID_STT_DEVICES = {"cuda", "cpu"}
_VALID_STT_PROVIDERS = {"local", "groq"}
_VALID_LLM_PROVIDERS = {"groq", "gemini"}
_VALID_EFFORTS = {"low", "medium", "high"}
_PROVIDER_SECRET_NAME = {"groq": "GROQ_API_KEY", "gemini": "GEMINI_API_KEY"}


class ConfigError(Exception):
    """Config is missing, malformed, or fails validation."""


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    model_fast: str
    model_default: str
    model_reasoning: str
    effort_fast: str
    effort_default: str
    effort_reasoning: str
    local_fallback_model: str
    local_fallback_host: str

    def model_for(self, tier: str = "default") -> str:
        try:
            return getattr(self, f"model_{tier}")
        except AttributeError as exc:
            raise ConfigError(f"Unknown LLM tier '{tier}'. Use fast, default, or reasoning.") from exc

    def effort_for(self, tier: str = "default") -> str:
        try:
            return getattr(self, f"effort_{tier}")
        except AttributeError as exc:
            raise ConfigError(f"Unknown LLM tier '{tier}'. Use fast, default, or reasoning.") from exc

    def provider_secret_name(self) -> str:
        return _PROVIDER_SECRET_NAME[self.provider]


@dataclass(frozen=True)
class SpeechConfig:
    tts_engine: str
    tts_voice: str
    stt_provider: str
    stt_model: str
    stt_device: str
    stt_groq_model: str
    kokoro_model_path: str
    kokoro_voices_path: str


@dataclass(frozen=True)
class AudioConfig:
    vad_model_path: str
    vad_threshold: float
    vad_min_speech_ms: float
    vad_end_silence_ms: float


@dataclass(frozen=True)
class WakeWordConfig:
    phrases: tuple[str, ...]
    threshold: float


@dataclass(frozen=True)
class PersonaConfig:
    name: str


@dataclass(frozen=True)
class RuntimeConfig:
    log_level: str
    core_port: int


@dataclass(frozen=True)
class Config:
    llm: LLMConfig
    speech: SpeechConfig
    audio: AudioConfig
    wake_word: WakeWordConfig
    persona: PersonaConfig
    runtime: RuntimeConfig

    @staticmethod
    def secret(name: str, *, required: bool = True) -> str:
        """Read a secret directly from the environment (never from TOML)."""
        value = os.environ.get(name, "")
        if required and not value:
            raise ConfigError(
                f"Missing required secret '{name}'. Set it in .env (see .env.example)."
            )
        return value


def _env_override(env_key: str, default: str) -> str:
    return os.environ.get(env_key) or default


def _env_list_override(env_key: str, default: list[str]) -> tuple[str, ...]:
    raw = os.environ.get(env_key)
    items = default if not raw else raw.split(",")
    # Dedupe case-insensitively but keep the first spelling and the original order.
    # Each wake phrase becomes its own always-on model in 1.5, so a duplicate is
    # wasted CPU rather than a harmless repeat.
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        cleaned = item.strip()
        if cleaned and cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            result.append(cleaned)
    return tuple(result)


def load_config(root: Path | None = None) -> Config:
    root = root or PROJECT_ROOT

    env_path = root / ".env"
    if env_path.exists():
        load_dotenv(env_path)

    toml_path = root / "config" / "default.toml"
    if not toml_path.exists():
        raise ConfigError(f"Config file not found: {toml_path}")

    with toml_path.open("rb") as f:
        raw = tomllib.load(f)

    try:
        llm = LLMConfig(
            provider=_env_override("CLIO_LLM_PROVIDER", raw["llm"]["provider"]).lower(),
            model_fast=_env_override("CLIO_LLM_MODEL_FAST", raw["llm"]["model_fast"]),
            model_default=_env_override("CLIO_LLM_MODEL_DEFAULT", raw["llm"]["model_default"]),
            model_reasoning=_env_override("CLIO_LLM_MODEL_REASONING", raw["llm"]["model_reasoning"]),
            effort_fast=_env_override("CLIO_LLM_EFFORT_FAST", raw["llm"]["effort_fast"]).lower(),
            effort_default=_env_override("CLIO_LLM_EFFORT_DEFAULT", raw["llm"]["effort_default"]).lower(),
            effort_reasoning=_env_override("CLIO_LLM_EFFORT_REASONING", raw["llm"]["effort_reasoning"]).lower(),
            local_fallback_model=_env_override(
                "CLIO_LLM_LOCAL_FALLBACK_MODEL", raw["llm"]["local_fallback_model"]
            ),
            local_fallback_host=_env_override(
                "CLIO_LLM_LOCAL_FALLBACK_HOST", raw["llm"]["local_fallback_host"]
            ),
        )
        speech = SpeechConfig(
            tts_engine=_env_override("CLIO_TTS_ENGINE", raw["speech"]["tts_engine"]),
            tts_voice=_env_override("CLIO_TTS_VOICE", raw["speech"]["tts_voice"]),
            stt_provider=_env_override("CLIO_STT_PROVIDER", raw["speech"]["stt_provider"]).lower(),
            stt_model=_env_override("CLIO_STT_MODEL", raw["speech"]["stt_model"]),
            stt_device=_env_override("CLIO_STT_DEVICE", raw["speech"]["stt_device"]),
            stt_groq_model=_env_override("CLIO_STT_GROQ_MODEL", raw["speech"]["stt_groq_model"]),
            kokoro_model_path=str(
                root / _env_override("CLIO_KOKORO_MODEL_PATH", raw["speech"]["kokoro_model_path"])
            ),
            kokoro_voices_path=str(
                root / _env_override("CLIO_KOKORO_VOICES_PATH", raw["speech"]["kokoro_voices_path"])
            ),
        )
        audio = AudioConfig(
            vad_model_path=str(
                root / _env_override("CLIO_VAD_MODEL_PATH", raw["audio"]["vad_model_path"])
            ),
            vad_threshold=float(
                _env_override("CLIO_VAD_THRESHOLD", str(raw["audio"]["vad_threshold"]))
            ),
            vad_min_speech_ms=float(
                _env_override("CLIO_VAD_MIN_SPEECH_MS", str(raw["audio"]["vad_min_speech_ms"]))
            ),
            vad_end_silence_ms=float(
                _env_override("CLIO_VAD_END_SILENCE_MS", str(raw["audio"]["vad_end_silence_ms"]))
            ),
        )
        wake_word = WakeWordConfig(
            phrases=_env_list_override("CLIO_WAKE_PHRASES", raw["wake_word"]["phrases"]),
            threshold=float(_env_override("CLIO_WAKE_THRESHOLD", str(raw["wake_word"]["threshold"]))),
        )
        persona = PersonaConfig(
            name=_env_override("CLIO_PERSONA", raw["persona"]["name"]),
        )
        runtime = RuntimeConfig(
            log_level=_env_override("CLIO_LOG_LEVEL", raw["runtime"]["log_level"]).upper(),
            core_port=int(_env_override("CLIO_CORE_PORT", str(raw["runtime"]["core_port"]))),
        )
    except KeyError as exc:
        raise ConfigError(f"Missing config key {exc} in {toml_path}") from exc
    except ValueError as exc:
        raise ConfigError(f"Malformed config value: {exc}") from exc

    config = Config(llm=llm, speech=speech, audio=audio, wake_word=wake_word, persona=persona, runtime=runtime)
    _validate(config)
    return config


def _validate(config: Config) -> None:
    errors: list[str] = []

    if config.runtime.log_level not in _VALID_LOG_LEVELS:
        errors.append(
            f"runtime.log_level '{config.runtime.log_level}' must be one of {sorted(_VALID_LOG_LEVELS)}"
        )
    if not (1024 <= config.runtime.core_port <= 65535):
        errors.append(f"runtime.core_port {config.runtime.core_port} must be between 1024 and 65535")
    if config.speech.tts_engine not in _VALID_TTS_ENGINES:
        errors.append(
            f"speech.tts_engine '{config.speech.tts_engine}' must be one of {sorted(_VALID_TTS_ENGINES)}"
        )
    elif config.speech.tts_engine == "kokoro":
        for label, path in (
            ("kokoro_model_path", config.speech.kokoro_model_path),
            ("kokoro_voices_path", config.speech.kokoro_voices_path),
        ):
            if not Path(path).is_file():
                errors.append(f"speech.{label} '{path}' does not exist - see .env.example for how to download it")
    if not Path(config.audio.vad_model_path).is_file():
        errors.append(
            f"audio.vad_model_path '{config.audio.vad_model_path}' does not exist - "
            "see .env.example for how to download it"
        )
    if not (0.0 <= config.audio.vad_threshold <= 1.0):
        errors.append(f"audio.vad_threshold {config.audio.vad_threshold} must be between 0.0 and 1.0")
    if config.audio.vad_min_speech_ms <= 0:
        errors.append(f"audio.vad_min_speech_ms {config.audio.vad_min_speech_ms} must be positive")
    if config.audio.vad_end_silence_ms <= 0:
        errors.append(f"audio.vad_end_silence_ms {config.audio.vad_end_silence_ms} must be positive")
    if config.speech.stt_device not in _VALID_STT_DEVICES:
        errors.append(
            f"speech.stt_device '{config.speech.stt_device}' must be one of {sorted(_VALID_STT_DEVICES)}"
        )
    if config.speech.stt_provider not in _VALID_STT_PROVIDERS:
        errors.append(
            f"speech.stt_provider '{config.speech.stt_provider}' must be one of {sorted(_VALID_STT_PROVIDERS)}"
        )
    if not (0.0 <= config.wake_word.threshold <= 1.0):
        errors.append(f"wake_word.threshold {config.wake_word.threshold} must be between 0.0 and 1.0")
    if not config.wake_word.phrases:
        errors.append("wake_word.phrases must not be empty")
    elif any(not phrase.strip() for phrase in config.wake_word.phrases):
        errors.append("wake_word.phrases must not contain blank entries")
    if config.llm.provider not in _VALID_LLM_PROVIDERS:
        errors.append(f"llm.provider '{config.llm.provider}' must be one of {sorted(_VALID_LLM_PROVIDERS)}")
    for tier in ("fast", "default", "reasoning"):
        effort = config.llm.effort_for(tier)
        if effort not in _VALID_EFFORTS:
            errors.append(f"llm.effort_{tier} '{effort}' must be one of {sorted(_VALID_EFFORTS)}")

    if errors:
        raise ConfigError("Invalid configuration:\n  - " + "\n  - ".join(errors))

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


class ConfigError(Exception):
    """Config is missing, malformed, or fails validation."""


@dataclass(frozen=True)
class LLMConfig:
    model_fast: str
    model_default: str
    model_reasoning: str

    def model_for(self, tier: str = "default") -> str:
        try:
            return getattr(self, f"model_{tier}")
        except AttributeError as exc:
            raise ConfigError(f"Unknown LLM tier '{tier}'. Use fast, default, or reasoning.") from exc


@dataclass(frozen=True)
class SpeechConfig:
    tts_engine: str
    tts_voice: str
    stt_model: str
    stt_device: str


@dataclass(frozen=True)
class WakeWordConfig:
    word: str
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
            model_fast=_env_override("CLIO_LLM_MODEL_FAST", raw["llm"]["model_fast"]),
            model_default=_env_override("CLIO_LLM_MODEL_DEFAULT", raw["llm"]["model_default"]),
            model_reasoning=_env_override("CLIO_LLM_MODEL_REASONING", raw["llm"]["model_reasoning"]),
        )
        speech = SpeechConfig(
            tts_engine=_env_override("CLIO_TTS_ENGINE", raw["speech"]["tts_engine"]),
            tts_voice=_env_override("CLIO_TTS_VOICE", raw["speech"]["tts_voice"]),
            stt_model=_env_override("CLIO_STT_MODEL", raw["speech"]["stt_model"]),
            stt_device=_env_override("CLIO_STT_DEVICE", raw["speech"]["stt_device"]),
        )
        wake_word = WakeWordConfig(
            word=_env_override("CLIO_WAKE_WORD", raw["wake_word"]["word"]),
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

    config = Config(llm=llm, speech=speech, wake_word=wake_word, persona=persona, runtime=runtime)
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
    if config.speech.stt_device not in _VALID_STT_DEVICES:
        errors.append(
            f"speech.stt_device '{config.speech.stt_device}' must be one of {sorted(_VALID_STT_DEVICES)}"
        )
    if not (0.0 <= config.wake_word.threshold <= 1.0):
        errors.append(f"wake_word.threshold {config.wake_word.threshold} must be between 0.0 and 1.0")
    if not config.wake_word.word.strip():
        errors.append("wake_word.word must not be empty")

    if errors:
        raise ConfigError("Invalid configuration:\n  - " + "\n  - ".join(errors))

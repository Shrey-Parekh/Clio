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
    tts_speed: float
    tts_device: str
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
    conversation_follow_up_ms: float
    input_device: str

    def input_device_arg(self) -> int | str | None:
        """AudioCapture's device param: None means "let sounddevice pick the
        system default" - a bare int-looking string becomes an int (sounddevice
        indexes devices numerically), anything else passes through as a name."""
        if not self.input_device:
            return None
        try:
            return int(self.input_device)
        except ValueError:
            return self.input_device


@dataclass(frozen=True)
class WakeWordConfig:
    phrases: tuple[str, ...]
    threshold: float
    models_dir: str

    @staticmethod
    def slug(phrase: str) -> str:
        return phrase.lower().replace(" ", "_")

    def model_paths(self) -> dict[str, str]:
        """Map each configured phrase to its trained .onnx model path."""
        return {phrase: str(Path(self.models_dir) / f"{self.slug(phrase)}.onnx") for phrase in self.phrases}


@dataclass(frozen=True)
class MemoryConfig:
    root: str
    recent_turns_on_start: int
    recall_hits: int
    consolidate: bool
    prewarm: bool


@dataclass(frozen=True)
class PersonaConfig:
    name: str
    system_prompt: str


@dataclass(frozen=True)
class LocationConfig:
    """Where "outside" is. Empty by default and never inferred - coordinates
    leave the machine when weather is asked for, so setting them is a choice
    rather than something that happens quietly on first use."""

    name: str
    latitude: float
    longitude: float

    @property
    def configured(self) -> bool:
        return self.latitude != 0.0 or self.longitude != 0.0


@dataclass(frozen=True)
class HotkeyConfig:
    """A global key that triggers a turn, as an alternative to the wake word.
    Optional and defaulted, so a config predating it still starts."""

    enabled: bool
    combo: str


@dataclass(frozen=True)
class MouseConfig:
    """A spare mouse button that triggers a turn. Optional and defaulted, so a
    config predating it still starts; off by default, so it claims no button
    until asked."""

    enabled: bool
    button: str


@dataclass(frozen=True)
class DictationConfig:
    """A hotkey that dictates into the focused window instead of answering.
    Optional and defaulted, so a config predating it still starts."""

    enabled: bool
    combo: str


@dataclass(frozen=True)
class PushToTalkConfig:
    """Hold a key to speak, releasing to end the turn, instead of VAD
    endpointing. Optional and defaulted, so a config predating it still starts;
    off by default, so it claims no key until asked."""

    enabled: bool
    key: str


@dataclass(frozen=True)
class EmailConfig:
    """Reading Gmail (6.6). Every field is defaulted, so a config file predating
    this section still starts - without email, not with a broken one.

    `window_days` and `category` are what stop "how many unread" answering with
    ten thousand: unread means recent and in the Primary tab. `extract_chars`
    decides how much of a message reaches the cloud model, which is why it is a
    setting rather than a constant."""

    important_senders: tuple[str, ...] = ()
    important_domains: tuple[str, ...] = ()
    max_triage: int = 15
    extract_chars: int = 500
    window_days: int = 2
    category: str = "primary"
    signature: str = "Shrey"   # the name a draft is signed off with (6.7)


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
    memory: MemoryConfig
    location: LocationConfig
    hotkey: HotkeyConfig
    mouse: MouseConfig
    dictation: DictationConfig
    push_to_talk: PushToTalkConfig
    # Name -> path or URL, straight from TOML. A plain mapping, because a
    # dataclass around "whatever he decided to name his own things" would only
    # be a second place to edit every time he adds one.
    shortcuts: dict[str, str]
    # The only folders she may look inside. Empty means she says she has
    # nowhere to look, rather than defaulting to the whole user profile.
    file_roots: tuple[Path, ...]
    email: EmailConfig
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
            tts_speed=float(_env_override("CLIO_TTS_SPEED", str(raw["speech"]["tts_speed"]))),
            tts_device=_env_override("CLIO_TTS_DEVICE", raw["speech"].get("tts_device", "cuda")).lower(),
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
            conversation_follow_up_ms=float(
                _env_override(
                    "CLIO_CONVERSATION_FOLLOW_UP_MS", str(raw["audio"]["conversation_follow_up_ms"])
                )
            ),
            input_device=_env_override("CLIO_AUDIO_INPUT_DEVICE", raw["audio"].get("input_device", "")),
        )
        wake_word = WakeWordConfig(
            phrases=_env_list_override("CLIO_WAKE_PHRASES", raw["wake_word"]["phrases"]),
            threshold=float(_env_override("CLIO_WAKE_THRESHOLD", str(raw["wake_word"]["threshold"]))),
            models_dir=str(
                root / _env_override("CLIO_WAKE_MODELS_DIR", raw["wake_word"]["models_dir"])
            ),
        )
        persona = PersonaConfig(
            name=_env_override("CLIO_PERSONA", raw["persona"]["name"]),
            system_prompt=_env_override("CLIO_PERSONA_SYSTEM_PROMPT", raw["persona"]["system_prompt"]).strip(),
        )
        memory = MemoryConfig(
            root=str(root / _env_override("CLIO_MEMORY_ROOT", raw["memory"]["root"])),
            recent_turns_on_start=int(
                _env_override("CLIO_MEMORY_RECENT_TURNS", str(raw["memory"]["recent_turns_on_start"]))
            ),
            recall_hits=int(_env_override("CLIO_MEMORY_RECALL_HITS", str(raw["memory"]["recall_hits"]))),
            consolidate=_env_override(
                "CLIO_MEMORY_CONSOLIDATE", str(raw["memory"]["consolidate"])
            ).strip().lower()
            in {"1", "true", "yes", "on"},
            prewarm=_env_override("CLIO_PREWARM", str(raw["memory"].get("prewarm", True)))
            .strip()
            .lower()
            in {"1", "true", "yes", "on"},
        )
        # Defaulted rather than required: an existing config file without a
        # [location] section must keep starting, just without weather.
        location_raw = raw.get("location", {})
        location = LocationConfig(
            name=_env_override("CLIO_LOCATION_NAME", location_raw.get("name", "")),
            latitude=float(_env_override("CLIO_LATITUDE", str(location_raw.get("latitude", 0.0)))),
            longitude=float(_env_override("CLIO_LONGITUDE", str(location_raw.get("longitude", 0.0)))),
        )
        hotkey_raw = raw.get("hotkey", {})
        hotkey = HotkeyConfig(
            enabled=_env_override("CLIO_HOTKEY_ENABLED", str(hotkey_raw.get("enabled", True)))
            .strip().lower() in {"1", "true", "yes", "on"},
            combo=_env_override("CLIO_HOTKEY_COMBO", str(hotkey_raw.get("combo", "ctrl+alt+c"))),
        )
        mouse_raw = raw.get("mouse", {})
        mouse = MouseConfig(
            enabled=_env_override("CLIO_MOUSE_ENABLED", str(mouse_raw.get("enabled", False)))
            .strip().lower() in {"1", "true", "yes", "on"},
            button=_env_override("CLIO_MOUSE_BUTTON", str(mouse_raw.get("button", "x2"))),
        )
        dictation_raw = raw.get("dictation", {})
        dictation = DictationConfig(
            enabled=_env_override("CLIO_DICTATION_ENABLED", str(dictation_raw.get("enabled", True)))
            .strip().lower() in {"1", "true", "yes", "on"},
            combo=_env_override("CLIO_DICTATION_COMBO", str(dictation_raw.get("combo", "ctrl+alt+d"))),
        )
        ptt_raw = raw.get("push_to_talk", {})
        push_to_talk = PushToTalkConfig(
            enabled=_env_override("CLIO_PTT_ENABLED", str(ptt_raw.get("enabled", False)))
            .strip().lower() in {"1", "true", "yes", "on"},
            key=_env_override("CLIO_PTT_KEY", str(ptt_raw.get("key", "f8"))),
        )
        # Same reason as [location]: optional, so a config predating it starts.
        shortcuts = {str(k): str(v) for k, v in raw.get("shortcuts", {}).items()}
        file_roots = tuple(
            Path(os.path.expandvars(str(r))).expanduser()
            for r in raw.get("files", {}).get("roots", [])
        )
        email_raw = raw.get("email", {})
        email = EmailConfig(
            important_senders=tuple(str(s).lower() for s in email_raw.get("important_senders", [])),
            important_domains=tuple(str(d).lower() for d in email_raw.get("important_domains", [])),
            max_triage=int(email_raw.get("max_triage", 15)),
            extract_chars=int(email_raw.get("extract_chars", 500)),
            window_days=int(email_raw.get("window_days", 2)),
            category=str(email_raw.get("category", "primary")).strip().lower(),
            signature=str(email_raw.get("signature", "Shrey")).strip(),
        )
        runtime = RuntimeConfig(
            log_level=_env_override("CLIO_LOG_LEVEL", raw["runtime"]["log_level"]).upper(),
            core_port=int(_env_override("CLIO_CORE_PORT", str(raw["runtime"]["core_port"]))),
        )
    except KeyError as exc:
        raise ConfigError(f"Missing config key {exc} in {toml_path}") from exc
    except ValueError as exc:
        raise ConfigError(f"Malformed config value: {exc}") from exc

    config = Config(
        llm=llm,
        speech=speech,
        audio=audio,
        wake_word=wake_word,
        persona=persona,
        memory=memory,
        location=location,
        hotkey=hotkey,
        mouse=mouse,
        dictation=dictation,
        push_to_talk=push_to_talk,
        shortcuts=shortcuts,
        file_roots=file_roots,
        email=email,
        runtime=runtime,
    )
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
    if config.hotkey.enabled:
        from clio.input.hotkey import parse_combo
        try:
            parse_combo(config.hotkey.combo)
        except ValueError as exc:
            errors.append(f"hotkey.combo '{config.hotkey.combo}' is unusable: {exc}")
    if config.mouse.enabled:
        from clio.input.mouse import parse_button
        try:
            parse_button(config.mouse.button)
        except ValueError as exc:
            errors.append(f"mouse.button '{config.mouse.button}' is unusable: {exc}")
    if config.dictation.enabled:
        from clio.input.hotkey import parse_combo
        try:
            parse_combo(config.dictation.combo)
        except ValueError as exc:
            errors.append(f"dictation.combo '{config.dictation.combo}' is unusable: {exc}")
    if config.push_to_talk.enabled:
        from clio.input.keyboard import parse_key
        try:
            parse_key(config.push_to_talk.key)
        except ValueError as exc:
            errors.append(f"push_to_talk.key '{config.push_to_talk.key}' is unusable: {exc}")
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
    if config.speech.tts_speed <= 0:
        errors.append(f"speech.tts_speed {config.speech.tts_speed} must be positive")
    if config.speech.tts_device not in _VALID_STT_DEVICES:
        errors.append(
            f"speech.tts_device '{config.speech.tts_device}' must be one of {sorted(_VALID_STT_DEVICES)}"
        )
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
    if config.audio.conversation_follow_up_ms <= 0:
        errors.append(
            f"audio.conversation_follow_up_ms {config.audio.conversation_follow_up_ms} must be positive"
        )
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
    else:
        for phrase, model_path in config.wake_word.model_paths().items():
            if not Path(model_path).is_file():
                errors.append(
                    f"wake_word phrase '{phrase}' has no trained model at '{model_path}' - "
                    "train it (see docs/wake_word_training.md) or remove it from wake_word.phrases"
                )
    if not config.persona.name.strip():
        errors.append("persona.name must not be blank")
    if not config.persona.system_prompt:
        errors.append("persona.system_prompt must not be blank")
    if config.memory.recent_turns_on_start < 0:
        errors.append(
            f"memory.recent_turns_on_start {config.memory.recent_turns_on_start} must not be negative"
        )
    if config.memory.recall_hits < 0:
        errors.append(f"memory.recall_hits {config.memory.recall_hits} must not be negative")
    if config.llm.provider not in _VALID_LLM_PROVIDERS:
        errors.append(f"llm.provider '{config.llm.provider}' must be one of {sorted(_VALID_LLM_PROVIDERS)}")
    for tier in ("fast", "default", "reasoning"):
        effort = config.llm.effort_for(tier)
        if effort not in _VALID_EFFORTS:
            errors.append(f"llm.effort_{tier} '{effort}' must be one of {sorted(_VALID_EFFORTS)}")

    if errors:
        raise ConfigError("Invalid configuration:\n  - " + "\n  - ".join(errors))

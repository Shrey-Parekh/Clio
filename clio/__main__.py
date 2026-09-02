"""Entrypoint: `python -m clio`. Starts the core service, runs until interrupted, exits clean."""

import signal
import sys
import time

from clio.core.config import ConfigError, load_config


def main() -> int:
    print("Clio starting...")

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1

    print(
        f"Loaded config - persona: {config.persona.name}, "
        f"wake word: {config.wake_word.word!r}, "
        f"LLM: {config.llm.model}, "
        f"TTS: {config.speech.tts_engine} ({config.speech.tts_voice}), "
        f"port: {config.runtime.core_port}"
    )

    running = True

    def _stop(signum, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _stop)

    print("Clio is running. Press Ctrl+C to stop.")
    while running:
        time.sleep(0.2)

    print("Clio shutting down...")
    return 0


if __name__ == "__main__":
    sys.exit(main())

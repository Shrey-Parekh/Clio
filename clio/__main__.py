"""Entrypoint: `python -m clio`. Starts the core service, runs until interrupted, exits clean."""

import signal
import sys
import time

from clio.core.config import ConfigError, load_config
from clio.core.logging import get_logger, setup_logging


def main() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1

    setup_logging(level=config.runtime.log_level)
    log = get_logger("clio.startup")

    log.info("Clio starting")
    log.info(
        "Config loaded",
        extra={
            "extra_fields": {
                "persona": config.persona.name,
                "wake_word": config.wake_word.word,
                "llm_model": config.llm.model,
                "tts_engine": config.speech.tts_engine,
                "tts_voice": config.speech.tts_voice,
                "core_port": config.runtime.core_port,
            }
        },
    )

    running = True

    def _stop(signum, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _stop)

    log.info("Clio is running. Press Ctrl+C to stop.")
    while running:
        time.sleep(0.2)

    log.info("Clio shutting down")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Entrypoint: `python -m clio`. Starts the core service, runs until interrupted, exits clean."""

import asyncio
import signal
import sys

from clio.core.config import ConfigError, load_config
from clio.core.events import Event, EventBus
from clio.core.logging import get_logger, setup_logging


async def _run(config) -> int:
    log = get_logger("clio.startup")
    bus = EventBus()

    async def log_every_event(event: Event) -> None:
        log.debug(
            f"Event: {event.name}",
            extra={"extra_fields": {"source": event.source, "payload": event.payload}},
        )

    bus.subscribe("*", log_every_event)

    await bus.publish(
        "clio.started",
        {
            "persona": config.persona.name,
            "wake_word": config.wake_word.word,
            "llm_model": config.llm.model,
            "tts_engine": config.speech.tts_engine,
            "tts_voice": config.speech.tts_voice,
            "core_port": config.runtime.core_port,
        },
        source="clio.startup",
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
        await asyncio.sleep(0.2)

    await bus.publish("clio.stopping", source="clio.startup")
    log.info("Clio shutting down")
    return 0


def main() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1

    setup_logging(level=config.runtime.log_level)
    return asyncio.run(_run(config))


if __name__ == "__main__":
    sys.exit(main())

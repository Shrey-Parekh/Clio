"""Entrypoint: `python -m clio`. Starts the core service, runs until interrupted, exits clean."""

import asyncio
import signal
import sys

from clio.core.config import ConfigError, load_config
from clio.core.errors import describe_error, report_error
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
            "wake_phrases": list(config.wake_word.phrases),
            "llm_model_default": config.llm.model_default,
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
    exit_code = 0
    try:
        while running:
            await asyncio.sleep(0.2)
    except Exception as exc:
        described = await report_error(bus, exc, context="main run loop", source="clio.startup")
        print(f"Clio hit a problem and is stopping: {described.spoken}", file=sys.stderr)
        exit_code = 1

    await bus.publish("clio.stopping", source="clio.startup")
    log.info("Clio shutting down")
    return exit_code


def main() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        described = describe_error(exc)
        print(f"Clio can't start: {described.spoken}", file=sys.stderr)
        print(f"Details: {exc}", file=sys.stderr)
        return 1

    setup_logging(level=config.runtime.log_level)
    return asyncio.run(_run(config))


if __name__ == "__main__":
    sys.exit(main())

"""Entrypoint: `python -m clio`. Starts the core service, runs until interrupted, exits clean."""

import asyncio
import signal
import sys

from clio.core.config import ConfigError, load_config
from clio.core.errors import describe_error, report_error
from clio.core.events import Event, EventBus
from clio.core.logging import get_logger, setup_logging
from clio.core.server import CoreServer
from clio.orchestrator import build_orchestrator
from clio.speech.audio_input import AudioCapture


def _command_handler(orchestrator, config):
    """Maps a frontend command to a core action. Returns a reply payload for the
    commands that ask for data (settings, memory), None for fire-and-forget."""

    async def handle(command: dict):
        cmd = command.get("cmd")
        if cmd == "say":
            await orchestrator.inject_text(str(command.get("text", "")))
        elif cmd == "mute":
            await orchestrator.set_muted(bool(command.get("on", True)))
        elif cmd == "tts_speed":
            orchestrator.set_tts_speed(float(command.get("value", 1.0)))
        elif cmd == "add_fact":
            return {"added": orchestrator.add_fact(str(command.get("text", "")))}
        elif cmd == "get_memory":
            return {"facts": orchestrator.facts()}
        elif cmd == "get_settings":
            return {"persona": config.persona.name,
                    "wake_phrases": list(config.wake_word.phrases),
                    "voice": config.speech.tts_voice,
                    **orchestrator.settings()}
        return None

    return handle


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

    orchestrator = build_orchestrator(config, bus)

    # The frontend's window onto the core. A bind failure (port taken) must not
    # stop the voice loop, so it is logged and Clio runs on without a frontend.
    server = CoreServer(
        bus, config.runtime.core_port, command_handler=_command_handler(orchestrator, config)
    )
    try:
        await server.start()
    except Exception:
        log.exception("Core server failed to start, continuing without a frontend")
        server = None

    try:
        capture = AudioCapture(device=config.audio.input_device_arg())
        run_task = asyncio.ensure_future(orchestrator.run(capture))
        while running and not run_task.done():
            await asyncio.sleep(0.2)

        if run_task.done():
            run_task.result()
        else:
            run_task.cancel()
            try:
                await run_task
            except asyncio.CancelledError:
                pass
    except Exception as exc:
        described = await report_error(bus, exc, context="main run loop", source="clio.startup")
        print(f"Clio hit a problem and is stopping: {described.spoken}", file=sys.stderr)
        exit_code = 1

    await bus.publish("clio.stopping", source="clio.startup")
    if server is not None:
        await server.stop()
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

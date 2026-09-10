"""Wiring every deterministic capability onto the router.

Handlers close over the orchestrator `o` for the state they need (timers,
clipboard, notes, memory, the LLM). Registration order is match order, so
narrower intents register before broader ones.
"""

from __future__ import annotations

import asyncio

from clio.capabilities.assistant import (
    adjust_speed, describe_capabilities, parse_help_request, parse_voice_request,
)
from clio.capabilities.calculate import format_number, parse_calculation
from clio.capabilities.chance import decide, parse_chance_request
from clio.capabilities.clipboard import parse_clipboard_request
from clio.capabilities.clock import answer as clock_answer, parse_clock_request
from clio.capabilities.control import (
    apply as apply_control, describe_action, parse_close, parse_control, parse_media, parse_power,
)
from clio.capabilities.convert import format_conversion, parse_conversion
from clio.capabilities.currency import convert_currency, parse_currency_request
from clio.capabilities.diagnose import explain_failure, is_diagnosis_query
from clio.capabilities.files import look_up, parse_file_request
from clio.capabilities.launch import open_target, resolve as resolve_target
from clio.capabilities.network import describe_network, parse_network_request
from clio.capabilities.notes import parse_note_request
from clio.capabilities.repeat import is_repeat_command
from clio.capabilities.status import is_status_query
from clio.capabilities.stop import is_stop_command
from clio.capabilities.stopwatch import parse_stopwatch_command
from clio.capabilities.system import describe_system, parse_system_query
from clio.capabilities.timer import parse_timer_command, parse_timer_control
from clio.capabilities.weather import describe_weather, is_weather_query
from clio.capabilities.web import (
    WebRequest, answer as web_answer, explain_failure as web_failure, parse_web_request,
)
from clio.core.errors import report_error
from clio.core.logging import get_logger

log = get_logger("clio.registry")


def register_capabilities(o) -> None:
    """Register every deterministic intent on `o._router`."""

    async def stop(_payload: object) -> None:
        return None

    async def start_timer(payload: object) -> str:
        return o._timers.start(float(payload))

    async def repeat(_payload: object) -> str | None:
        if not o._last_plan:
            return "You haven't asked me to do anything yet."
        log.info("Repeating", extra={"extra_fields": {"steps": [m.intent for m in o._last_plan]}})
        # Back through the gate, not run(): a confirm-tier step asks again every time.
        return await o._run_plan(o._last_plan)

    async def diagnose(_payload: object) -> str:
        return explain_failure(o._last_failure)

    async def weather(_payload: object) -> str:
        return await describe_weather(o._location)

    async def currency(payload: object) -> str:
        amount, source, target = payload  # type: ignore[misc]
        return await convert_currency(amount, source, target)

    async def convert_units(payload: object) -> str:
        value, source, target = payload  # type: ignore[misc]
        return format_conversion(value, source, target)

    async def notes(payload: object) -> str:
        request = payload  # type: ignore[assignment]
        if request.kind == "read":
            return o._notes.recent()
        content = request.content
        if not content:
            # Bare "note that down" means her last reply.
            content = next(
                (m["content"] for m in reversed(o._memory.get_messages())
                 if m["role"] == "assistant"),
                "",
            )
            if not content:
                return "Note what down? Nothing's been said yet."
        return o._notes.add(content)

    async def clipboard(payload: object) -> str:
        request = payload  # type: ignore[assignment]
        # Clipboard I/O grabs a global lock with a retrying sleep, so it runs
        # off the loop, like the other blocking capabilities.
        if request.kind == "read":
            return await asyncio.to_thread(o._clipboard.read)
        if request.kind == "restore":
            return await asyncio.to_thread(o._clipboard.restore)

        original, refusal, sensitive = await asyncio.to_thread(o._clipboard.take)
        if refusal:
            return refusal

        # A credential is done locally or not at all. The local model is asked
        # for by name, since every call tries the cloud first.
        provider = o._llm
        if sensitive:
            provider = getattr(o._llm, "local", None)
            if provider is None:
                return ("That looks like a password or a key, and I've got no local model "
                        "to do it with, so I'm not sending it anywhere.")
            log.info("Clipboard transform kept local", extra={"extra_fields": {"reason": "secret"}})

        o._intent_used_llm = True
        result = await provider.complete(
            [
                {"role": "system", "content":
                    "You rewrite text. Return only the rewritten text - no preamble, no "
                    "explanation, no quotes around it. If the instruction does not apply, "
                    "return the text unchanged."},
                {"role": "user", "content": f"{request.instruction}\n\n---\n{original}"},
            ],
            tier="default",
        )
        spoken = await asyncio.to_thread(o._clipboard.replace, original, result.strip())
        return f"{spoken} Did that one locally, it looked like a key." if sensitive else spoken

    async def web(payload: object) -> str:
        request = payload  # type: ignore[assignment]
        if request.kind == "search" and not request.value:
            # "Look it up": the question he asked before it.
            question = next(
                (m["content"] for m in reversed(o._memory.get_messages())
                 if m["role"] == "user" and parse_web_request(m["content"]) is None),
                "",
            )
            if not question:
                return "Look what up? You haven't asked me anything yet."
            request = WebRequest("search", question)
        try:
            spoken, used = await web_answer(request, o._persona_system_prompt, o._llm)
        except Exception as exc:
            # Said, not raised: the network failing is ordinary for a search, and
            # must not end the conversation the way a broken local command does.
            described = await report_error(o._bus, exc, context="web search", source="clio.capabilities.web")
            return web_failure(exc) or described.spoken
        o._intent_used_llm = used
        return spoken

    async def clock(payload: object) -> str:
        kind, value = payload  # type: ignore[misc]
        return clock_answer(kind, value)

    async def chance(payload: object) -> str:
        kind, args = payload  # type: ignore[misc]
        return decide(kind, args)

    async def stopwatch(payload: object) -> str:
        return o._stopwatch.handle(str(payload))

    async def timer_control(payload: object) -> str:
        return o._timers.cancel_all() if payload == "cancel" else o._timers.remaining()

    async def network(payload: object) -> str:
        return await describe_network(str(payload))

    async def help_me(_payload: object) -> str:
        return describe_capabilities(o._router.capabilities())

    async def voice(payload: object) -> str:
        return adjust_speed(o._speaker, str(payload))

    async def files(payload: object) -> str:
        spoken, to_summarise = await look_up(payload, o._file_roots)  # type: ignore[arg-type]
        if not to_summarise:
            return spoken
        # The one deterministic intent that reaches the model; it says so.
        o._intent_used_llm = True
        return await o._llm.complete(
            [
                {"role": "system", "content": o._persona_system_prompt},
                {"role": "user", "content":
                    "Say what this file is and what's in it, out loud, in three sentences "
                    f"at most. No lists, no code, no file paths.\n\n{to_summarise}"},
            ],
            tier="default",
        )

    async def control(payload: object) -> str:
        return await apply_control(payload)  # type: ignore[arg-type]

    async def open_thing(payload: object) -> str:
        return open_target(payload)  # type: ignore[arg-type]

    async def system(payload: object) -> str:
        return await describe_system(str(payload))

    async def calculate(payload: object) -> str:
        return f"That works out to {format_number(float(payload))}."  # type: ignore[arg-type]

    async def status(_payload: object) -> str:
        # From the registry, not hardcoded: a network-bound capability must not
        # be listed as working offline.
        offline_ready = ", ".join(
            c.name for c in o._router.capabilities() if c.offline and c.name != "status"
        )
        state = getattr(o._llm, "using_fallback", None)
        if state is None:
            head = "Haven't needed the cloud model yet this session."
        elif state:
            head = "Running on the local model, the cloud one is unreachable."
        else:
            head = "Cloud model is up."
        tracker = getattr(o._llm, "usage", None)
        spend = tracker.summary() if tracker is not None else ""
        return (
            f"{head} Speech, memory and {offline_ready} all work with no network at all. {spend}"
        ).strip()

    r = o._router
    r.register("repeat", lambda t: True if is_repeat_command(t) else None, repeat)
    r.register("diagnose", lambda t: True if is_diagnosis_query(t) else None, diagnose)
    r.register("status", lambda t: True if is_status_query(t) else None, status)
    r.register("stop", lambda t: True if is_stop_command(t) else None, stop)
    # Control before start, so "cancel the five minute timer" isn't a new timer.
    r.register("timer_control", parse_timer_control, timer_control)
    r.register("timer", parse_timer_command, start_timer)
    r.register("stopwatch", parse_stopwatch_command, stopwatch)
    r.register("clock", parse_clock_request, clock)
    r.register("network", parse_network_request, network)
    r.register("help", parse_help_request, help_me)
    r.register("voice", parse_voice_request, voice)
    r.register("chance", parse_chance_request, chance)
    r.register("clipboard", parse_clipboard_request, clipboard)
    # Before files: "read my notes" isn't a request to read a file called notes.
    r.register("notes", parse_note_request, notes)
    # Before files and open: "find out who won" isn't a file lookup, and a search
    # names things ("look up Chrome's release notes") that open would claim.
    r.register("web", parse_web_request, web, offline=False)
    r.register(
        "weather", lambda t: True if is_weather_query(t) else None, weather, offline=False
    )
    # Currency before units: both say "convert X to Y", only currency knows a
    # rupee isn't a unit of length.
    r.register("currency", parse_currency_request, currency, offline=False)
    r.register("system", parse_system_query, system)
    # power/close/control share a module but not a risk; the describer is what
    # the confirmation reads out.
    r.register("power", parse_power, control, describe=describe_action)
    r.register("close", parse_close, control, describe=describe_action)
    # Media before control: "stop the music" isn't a window command.
    r.register("media", parse_media, control)
    r.register("control", parse_control, control, describe=describe_action)
    r.register("files", lambda t: parse_file_request(t, o._file_roots), files)
    r.register("convert", parse_conversion, convert_units)
    r.register("calculate", parse_calculation, calculate)
    # Last: its verbs are the broadest, so every narrower matcher gets first refusal.
    r.register("open", lambda t: resolve_target(t, o._shortcuts), open_thing)

"""The ten everyday capabilities, and the thing that actually threatens them:
twenty-three matchers competing for the same sentences.

Much of this file is the collision matrix. Each capability is easy alone; what
is hard is "stop" meaning the stop command, "stop the timer" meaning the timer
and "stop the music" meaning playback, all in one router.
Run: python tests/test_everyday.py
"""

import asyncio
import random
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clio.capabilities import control as control_mod  # noqa: E402
from clio.capabilities.assistant import (  # noqa: E402
    adjust_speed, describe_capabilities, parse_help_request, parse_voice_request,
)
from clio.capabilities.chance import decide, parse_chance_request  # noqa: E402
from clio.capabilities.clock import answer, parse_clock_request, speak_time  # noqa: E402
from clio.capabilities.network import parse_network_request  # noqa: E402
from clio.capabilities.stopwatch import Stopwatch, parse_stopwatch_command  # noqa: E402
from clio.capabilities.timer import TimerCapability, parse_timer_control  # noqa: E402
from clio.core.permissions import Permission  # noqa: E402
from clio.llm.memory import ConversationMemory  # noqa: E402
from clio.orchestrator import Orchestrator  # noqa: E402


class FakeLLM:
    def __init__(self):
        self.calls = 0

    async def complete(self, messages, tier="default"):
        self.calls += 1
        return "llm reply"


class FakeSpeaker:
    speed = 1.0


def build():
    llm = FakeLLM()
    orchestrator = Orchestrator(
        wake_detector=None, turn_detector=None, stt=None, llm=llm,
        speaker=FakeSpeaker(), persona_system_prompt="p", follow_up_window_s=1.0,
    )
    orchestrator._memory = ConversationMemory(provider=llm, system_prompt="p")

    async def always_yes(_prompt: str) -> bool:
        return True

    orchestrator._confirm = always_yes
    return orchestrator, llm


async def main():
    done = []
    control_mod.perform = lambda action: done.append(action) or ""

    # --- 1. the clock ---

    noon = datetime(2026, 9, 8, 12, 0)
    assert parse_clock_request("what time is it") == ("time", "")
    assert parse_clock_request("what's the date") == ("date", "")
    assert parse_clock_request("what time is it in tokyo") == ("elsewhere", "tokyo")
    # An unknown place is not a clock question she can answer.
    assert parse_clock_request("what time is it in narnia") is None
    assert answer("day", "", noon) == "It's Tuesday."
    assert answer("date", "", noon) == "It's Tuesday the 8th of September."
    assert speak_time(datetime(2026, 9, 8, 13, 30)) == "half past 1 in the afternoon"
    assert speak_time(datetime(2026, 9, 8, 9, 0)) == "9 o'clock in the morning"
    assert "6 hours" in answer("until", "6pm", noon), answer("until", "6pm", noon)
    assert "108 days" in answer("days_until", "25 december", noon)
    assert parse_clock_request("how long until the heat death of the universe") is None
    print(f"OK  clock: {answer('time', '', datetime(2026, 9, 8, 13, 6))!r}")

    # --- 2. media keys ---

    orchestrator, llm = build()
    for text, kind in [("pause", "play_pause"), ("next", "next_track"),
                       ("skip", "next_track"), ("stop the music", "stop_media"),
                       ("previous track", "previous_track")]:
        done.clear()
        spoken, _ = await orchestrator._handle_utterance(text)
        assert [a.kind for a in done] == [kind], (text, done)
        assert spoken == "", "Windows gives no feedback, so claiming it played would be a guess"
    print("OK  media keys sent, and nothing claimed about what happened")

    # --- 3. cancelling and checking a timer ---

    assert parse_timer_control("cancel the timer") == "cancel"
    assert parse_timer_control("how long is left") == "remaining"
    assert parse_timer_control("set a timer for five minutes") is None

    async def announce(_text):
        return None

    timers = TimerCapability(announce)
    assert "haven't got a timer running" in timers.remaining()
    timers.start(300)
    assert "5 minutes left on it" in timers.remaining()
    assert timers.cancel_all() == "That's the timer cancelled."
    # From the live session: asked five seconds after a ten second timer went
    # off, "There's no timer running" was true and useless. A timer that fired
    # recently is what he is asking about.
    timers._last_fired = (time.monotonic() - 5, 10.0)
    assert "went off just now" in timers.remaining(), timers.remaining()
    timers._last_fired = (time.monotonic() - 600, 10.0)
    assert "haven't got a timer running" in timers.remaining()
    timers._last_fired = None
    # Cancelling clears the deadline immediately, not whenever the loop next
    # gets round to running the cancelled task.
    assert "haven't got a timer running" in timers.remaining()
    print("OK  timers cancel and report what's left")

    # --- 4. the stopwatch ---

    watch = Stopwatch()
    assert parse_stopwatch_command("start a stopwatch") == "start"
    assert not watch.running and "isn't running" in watch.handle("check")
    assert watch.handle("start") == "Right, the stopwatch is running." and watch.running
    assert "Already running" in watch.handle("start"), "restarting would throw away the measurement"
    assert "Stopped it at" in watch.handle("stop") and not watch.running
    print("OK  stopwatch starts, reports and stops without losing a measurement")

    # --- 5. what can you do ---

    assert parse_help_request("what can you do") == "help"
    assert parse_help_request("slow down") is None
    said = describe_capabilities(orchestrator._router.capabilities())
    assert said.startswith("I can tell you the time"), said
    assert "set timers" in said and "other things" in said
    # Every registered capability is either named or counted - a capability
    # that exists but cannot be discovered is one he will never use.
    from clio.capabilities.assistant import _DESCRIPTIONS, _UNLISTED
    undescribed = [c.name for c in orchestrator._router.capabilities()
                   if c.name not in _DESCRIPTIONS and c.name not in _UNLISTED]
    assert not undescribed, f"registered but unnameable: {undescribed}"
    # Spoken, so it must not become a manual read aloud.
    assert len(said) < 320, f"too long to listen to ({len(said)} chars)"
    assert "stop" not in said.split(), "the stop command is not something to advertise"
    print(f"OK  help: {said}")

    # --- 6. per-app volume ---

    for text, kind, value in [("mute chrome", "app_mute", "chrome"),
                              ("unmute spotify", "app_unmute", "spotify")]:
        action = control_mod.parse_control(text)
        assert action.kind == kind and action.value == value, (text, action)
    assert control_mod.parse_control("mute").kind == "mute", "bare mute is still the whole machine"
    print("OK  per-app volume, and 'unmute' is not read as 'mute'")

    # --- 7. closing, which asks first ---

    assert control_mod.parse_close("close the door") is None, "no window, no claim"
    assert control_mod.parse_close("quit whining") is None
    assert control_mod.describe_action(control_mod.Action("close", "chrome")) == "Closing chrome"
    caps = {c.name: c for c in orchestrator._router.capabilities()}
    assert caps["close"].permission is Permission.CONFIRM, "closing can lose unsaved work"
    print("OK  closing asks first, and only for a window that exists")

    # --- 8. chance ---

    rng = random.Random(7)
    assert parse_chance_request("flip a coin") == ("coin", ())
    assert parse_chance_request("roll two dice") == ("dice", (2, 6))
    assert parse_chance_request("roll a d20") == ("dice", (1, 20))
    assert parse_chance_request("pick a number between 1 and 10") == ("number", (1, 10))
    assert parse_chance_request("pick between tea or coffee") == ("pick", ("tea", "coffee"))
    # One option is a question for the model, not a coin toss.
    assert parse_chance_request("pick a good restaurant") is None
    assert decide("coin", (), rng) in (
        "The coin came up heads.", "The coin came up tails."
    )
    numbers = {decide("number", (1, 100), rng) for _ in range(30)}
    assert len(numbers) > 10, "a model would say 7 every time; this must not"
    # Everything here is heard, never read. A bare "Tails." is the whole
    # answer and still lands like a machine reading out a register.
    for kind, args in [("coin", ()), ("dice", (1, 6)), ("dice", (2, 6)),
                       ("number", (1, 10)), ("pick", ("tea", "coffee"))]:
        said = decide(kind, args, rng)
        assert len(said.split()) >= 5, f"too clipped to say out loud: {said!r}"
        assert len(said.split()) <= 12, f"too long for a one-line answer: {said!r}"
    print(f"OK  chance is actually random: {sorted(numbers)[:5]}...")

    # --- 9. how fast she talks ---

    speaker = FakeSpeaker()
    assert parse_voice_request("slow down") == "slower"
    assert parse_voice_request("what can you do") is None
    adjust_speed(speaker, "slower")
    assert speaker.speed < 1.0
    for _ in range(10):
        adjust_speed(speaker, "slower")
    assert speaker.speed >= 0.7, "it must not slow down to a stop"
    assert "as slow as I go" in adjust_speed(speaker, "slower")
    print(f"OK  speaking speed adjusts and stops at {speaker.speed}")

    # --- 10. network ---

    assert parse_network_request("am i online") == "online"
    assert parse_network_request("what's my ip") == "local_ip"
    assert parse_network_request("what's my public ip") == "public_ip"
    assert parse_network_request("what wifi am i on") == "wifi"
    assert parse_network_request("are you online") is None, "that one is about her, not the network"
    print("OK  network questions separated from the ones about her")

    # --- the collision matrix, through the real router ---

    for text, intent in [
        ("stop", "stop"),
        ("stop the timer", "timer_control"),
        ("stop the music", "media"),
        ("cancel", "stop"),
        ("cancel the timer", "timer_control"),
        ("set a timer for five minutes", "timer"),
        ("start a stopwatch", "stopwatch"),
        ("how long is left", "timer_control"),
        ("how long has it been", "stopwatch"),
        ("what time is it", "clock"),
        ("how long until 6pm", "clock"),
        ("am i online", "network"),
        ("what can you do", "help"),
        ("slow down", "voice"),
        ("flip a coin", "chance"),
        ("mute chrome", "control"),
        ("pause", "media"),
        ("what's the weather", "weather"),
        ("how much disk space is left", "system"),
        ("what is 15 percent of 240", "calculate"),
    ]:
        matched = orchestrator._router.match(text)
        assert matched is not None and matched.intent == intent, (
            text, matched.intent if matched else None, intent
        )
    print("OK  twenty sentences, twenty capabilities, no collisions")

    # --- and none of it reaches the model ---

    llm_before = llm.calls
    for text in ["what time is it", "flip a coin", "am i online", "what can you do",
                 "start a stopwatch", "cancel the timer", "slow down", "pause"]:
        await orchestrator._handle_utterance(text)
    assert llm.calls == llm_before, "not one of these may cost an API call"
    print("OK  every one of them answered without the model")

    print("\nAll everyday capability checks passed.")


if __name__ == "__main__":
    asyncio.run(main())

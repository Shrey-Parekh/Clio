"""Meeting notes (8.5).

The microphone, the call audio, speech-to-text, the model and the speaker are
all faked: no real audio, network or keys. What is checked: the start and stop
phrases; a line that begins with her name is a question, not part of the call;
during the call she says nothing out loud - questions and timers come back as
text; at the end only the summary is saved, never the transcript; a summary
that fails keeps the transcript rather than losing the call; and a call that
runs past the limit stops itself.

Run: python tests/test_meeting.py
"""

import asyncio
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clio import orchestrator as orchestrator_module  # noqa: E402
from clio.capabilities.meeting import Meeting, addressed, parse_meeting_request  # noqa: E402
from clio.core.permissions import Permission  # noqa: E402
from clio.llm.memory import ConversationMemory  # noqa: E402
from clio.orchestrator import Orchestrator  # noqa: E402


class Ear:
    """A turn detector over a stream of already-cut sentences."""

    async def wait_for_onset(self, frames, stop=None):
        async for item in frames:
            if stop is not None and stop.is_set():
                return None
            if item:
                return [item]
        return None

    async def capture_until_silence(self, frames, onset):
        return onset[0]


class STT:
    async def transcribe(self, audio):
        return audio


class Speaker:
    def __init__(self):
        self.said = []

    async def speak(self, text, frames, listen_after_s=0.0):
        self.said.append(text)


class LLM:
    def __init__(self, fail_summary=False):
        self.fail_summary, self.saw = fail_summary, []

    async def complete(self, messages, tier="default"):
        content = messages[-1]["content"]
        self.saw.append(content)
        if "His question" in content:
            return "They said the deadline is Friday."
        if self.fail_summary:
            raise ConnectionError("Groq unreachable")
        return "They agreed the launch deadline is Friday; Shrey sends the slides."

    async def stream(self, messages, tier="default"):
        yield await self.complete(messages, tier)


def mic(lines, gap=0.03):
    async def frames():
        for line in lines:
            await asyncio.sleep(gap)
            yield line
        while True:   # a live mic never ends; the loop must notice stop on its own
            await asyncio.sleep(0.01)
            yield ""
    return frames()


async def call(o, mic_lines, them_lines, during=None):
    def fake_loopback(stop):
        async def frames():
            for line in them_lines:
                await asyncio.sleep(0.01)
                yield line
            while not stop.is_set():
                await asyncio.sleep(0.01)
                yield ""
        return frames()

    orchestrator_module.loopback_frames = fake_loopback
    o._frames = mic(mic_lines)
    assert o.start_meeting().startswith("Taking notes")
    if during:
        asyncio.get_running_loop().call_later(0.02, lambda: asyncio.ensure_future(during()))
    await asyncio.wait_for(o._meeting_loop(), timeout=5)


async def main():
    for said in ("start meeting notes", "take notes for this call", "Clio, start taking call notes.",
                 "transcribe this meeting"):
        assert parse_meeting_request(said) == "start", said
    for said in ("stop meeting notes", "stop transcribing", "the call is over", "end the meeting notes"):
        assert parse_meeting_request(said) == "stop", said
    for said in ("take a note", "start the timer", "what did the meeting cover", "stop"):
        assert parse_meeting_request(said) is None, said
    print("OK  start and stop phrases; 'take a note' and plain 'stop' are not meetings")

    assert addressed("Hey Clio, what did they say about the deadline?") == "what did they say about the deadline?"
    assert addressed("Cleo stop meeting notes") == "stop meeting notes"
    assert addressed("I think Clio is great") is None
    assert addressed("Chloe said it's fine") is None, "a person on the call is not her"
    print("OK  a line that starts with her name is for her; one that mentions her isn't")

    base = Path(tempfile.mkdtemp())
    real_loopback = orchestrator_module.loopback_frames
    try:
        m = Meeting(started=datetime(2026, 10, 7, 14, 30))
        m.add("You", "Hello", at=datetime(2026, 10, 7, 14, 31))
        m.add("Them", "Hi there", at=datetime(2026, 10, 7, 14, 31))
        assert m.transcript() == "[14:31] You: Hello\n[14:31] Them: Hi there"
        path = m.save(base, "The summary.", ended=datetime(2026, 10, 7, 15, 12))
        assert path.name == "2026-10-07 1430.md"
        assert path.read_text(encoding="utf-8") == "# Call, 07 October 2026, 14:30 to 15:12\n\nThe summary.\n"
        assert not m.too_long(datetime(2026, 10, 7, 17, 0)) and m.too_long(datetime(2026, 10, 7, 17, 31))
        print("OK  saved as one dated file holding the summary alone")

        llm, speaker, events = LLM(), Speaker(), []
        o = Orchestrator(
            wake_detector=None, turn_detector=Ear(), stt=STT(), llm=llm, speaker=speaker,
            persona_system_prompt="p", follow_up_window_s=1.0, memory_root=str(base / "m"),
            meeting_ear=Ear)
        o._memory = ConversationMemory(provider=llm, system_prompt="p")

        async def emit(name, payload=None):
            events.append((name, payload))

        o._emit = emit
        assert {c.name: c for c in o._router.capabilities()}["meeting"].permission is Permission.FREE
        assert o._router.match("start meeting notes").intent == "meeting"

        async def timer_fires():
            await o._announce("Your 5 minute timer is done.")

        await call(o, ["Hello everyone", "Hey Clio, what did they say about the deadline?",
                       "Clio, stop meeting notes"],
                   ["The launch deadline is Friday."], during=timer_fires)

        assert speaker.said == ["Notes saved. The summary's in the chat window."], speaker.said
        shown = [p["text"] for n, p in events if n == "clio.transcript"]
        assert "They said the deadline is Friday." in shown, "the question was answered as text"
        assert "Your 5 minute timer is done." in shown, "a timer during a call is shown, not said"
        assert ("clio.state", {"state": "meeting"}) in events
        question = next(s for s in llm.saw if "His question" in s)
        assert "Them: The launch deadline is Friday." in question and "You: Hello everyone" in question
        assert "Clio" not in question.split("His question")[0], "what he said to her is not part of the call"
        saved = list((base / "m" / "meetings").glob("*.md"))
        assert len(saved) == 1
        text = saved[0].read_text(encoding="utf-8")
        assert "launch deadline is Friday; Shrey sends the slides" in text
        assert "Hello everyone" not in text, "the transcript is never kept"
        assert o._meeting is None
        print("OK  silent during the call; questions and timers on screen; only the summary saved")

        o._llm = LLM(fail_summary=True)
        o._memory_root = base / "m2"
        await call(o, ["We ship on Friday", "Clio stop meeting notes"], [])
        kept = list((base / "m2" / "meetings").glob("*.md"))[0].read_text(encoding="utf-8")
        assert "We ship on Friday" in kept and "Summary failed" in kept
        assert "kept the transcript instead" in speaker.said[-1], speaker.said[-1]
        print("OK  a summary that fails keeps the transcript rather than losing the call")

        await call(o, ["Clio, stop meeting notes"], [])
        assert "nothing saved" in speaker.said[-1], speaker.said[-1]
        assert o.stop_meeting() == "I'm not taking notes at the moment."
        o._muted = True
        assert "unmute" in o.start_meeting() and o._meeting is None
        o._muted = False
        print("OK  an empty call saves nothing; stopping twice and starting while muted are explained")

        real_too_long = Meeting.too_long
        Meeting.too_long = lambda self, now=None: len(self.lines) >= 2
        try:
            await call(o, ["one", "two", "three"], [])
        finally:
            Meeting.too_long = real_too_long
        assert o._meeting is None, "the time limit ended it without a stop"
        print("OK  a forgotten call stops itself at the limit")

        print("\nAll meeting checks passed.")
    finally:
        orchestrator_module.loopback_frames = real_loopback
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())

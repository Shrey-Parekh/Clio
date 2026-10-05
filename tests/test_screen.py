"""Screen reading (8.1 + 8.2).

The screen grab and Groq are faked: no real screen, network or key. What is
checked: which sentences are about the screen (and which are not); "the whole
screen" asks for the whole monitor; Clio's own windows and minimised windows
are stepped past; a big grab is shrunk to JPEG; what is spoken has no markdown
or reasoning in it; every failure is a sentence, not an exception; and through
the real router, "read this to me" reaches the screen, FREE, not files or open.

Run: python tests/test_screen.py
"""

import asyncio
import io
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402

from clio.capabilities import registry  # noqa: E402
from clio.capabilities.screen import clean, explain_failure, parse_screen_request  # noqa: E402
from clio.core.permissions import Permission  # noqa: E402
from clio.core.screen import LONGEST_SIDE, CaptureError, Window, pick, shrink  # noqa: E402
from clio.llm.provider import LLMError, LLMPermanentError, LLMRateLimited  # noqa: E402
from clio.orchestrator import Orchestrator  # noqa: E402

SCREEN = [
    "what's on my screen", "What is on the screen?", "look at my screen",
    "read this to me", "read this", "can you read this out loud", "what does this say",
    "what does this error mean", "what's this error", "explain this", "explain this error to me",
    "what am I looking at", "hey clio, what's on my screen", "could you look at my screen please",
]
WHOLE = ["read the whole screen", "what's on the entire screen", "look at the full screen"]
NOT_SCREEN = [
    "lock the screen", "extend the screen", "dim the screen", "read the report",
    "explain that", "read that to me", "what does that mean", "what's on my calendar",
    "what went wrong", "explain quantum physics",
]


class FakeLLM:
    def __init__(self, answer="", error=None):
        self.answer, self.error, self.calls = answer, error, []

    async def look(self, jpeg, question, system):
        self.calls.append((jpeg, question, system))
        if self.error:
            raise self.error
        return self.answer


async def main():
    for said in SCREEN:
        request = parse_screen_request(said)
        assert request is not None and not request.whole, said
    for said in WHOLE:
        request = parse_screen_request(said)
        assert request is not None and request.whole, said
    for said in NOT_SCREEN:
        assert parse_screen_request(said) is None, said
    print("OK  screen sentences match, 'whole screen' asks for the monitor, near-misses don't")

    clio_chat = Window(1, "Clio — chat & settings", "clio.exe", (0, 0, 800, 600))
    helper = Window(2, "", "explorer.exe", (0, 0, 1920, 1080))
    parked = Window(3, "Downloads", "explorer.exe", (-32000, -32000, -31840, -31972), minimised=True)
    hidden = Window(4, "Settings", "ApplicationFrameHost.exe", (0, 0, 900, 700), cloaked=True)
    terminal = Window(5, "Windows PowerShell", "WindowsTerminal.exe", (100, 100, 1200, 800))
    assert pick([clio_chat, helper, parked, hidden, terminal]) is terminal
    assert pick([terminal, clio_chat]) is terminal
    assert pick([clio_chat, parked]) is None, "nothing real: the caller takes the monitor"
    print("OK  Clio's own, untitled, minimised and cloaked windows are stepped past")

    jpeg = shrink(Image.new("RGBA", (4000, 2000), "white"))
    image = Image.open(io.BytesIO(jpeg))
    assert image.format == "JPEG" and image.size == (LONGEST_SIDE, 800), image.size
    small = Image.open(io.BytesIO(shrink(Image.new("RGB", (640, 480)))))
    assert small.size == (640, 480), "never enlarged"
    print("OK  a 4000x2000 grab goes out as a 1600x800 JPEG; small ones are left alone")

    assert clean("<think>hmm</think>**ModuleNotFoundError** means `torch`\n## Fix\nisn't installed.") == \
        "ModuleNotFoundError means torch Fix isn't installed."
    assert clean("set my_var first") == "set my_var first", "underscores are part of names"
    print("OK  no reasoning and no markdown reach the speaker")

    assert explain_failure(CaptureError("x")) == "I couldn't take a picture of the screen."
    assert "limit" in explain_failure(LLMRateLimited("429"))
    assert "model_vision" in explain_failure(LLMPermanentError("404"))
    assert "didn't answer" in explain_failure(LLMError("down"))
    print("OK  every failure is a plain sentence")

    # --- through the real router ---

    base = Path(tempfile.mkdtemp())
    real_capture = registry.capture_screen
    try:
        grabs = []

        def fake_capture(whole):
            grabs.append(whole)
            return b"jpeg-bytes", "Windows PowerShell"

        registry.capture_screen = fake_capture
        llm = FakeLLM("That error means **torch** isn't installed. Run pip install torch.")
        o = Orchestrator(
            wake_detector=None, turn_detector=None, stt=None, llm=llm, speaker=None,
            persona_system_prompt="p", follow_up_window_s=1.0, memory_root=str(base / "m"))
        caps = {c.name: c for c in o._router.capabilities()}
        assert caps["screen"].permission is Permission.FREE

        for said in ("read this to me", "what does this error mean", "explain this"):
            matched = o._router.match(said)
            assert matched.intent == "screen", (said, matched.intent)
        matched = o._router.match("what does this error mean")
        spoken = await matched.run()
        assert spoken == "That error means torch isn't installed. Run pip install torch.", spoken
        jpeg, question, system = llm.calls[-1]
        assert jpeg == b"jpeg-bytes" and question == "what does this error mean" and "plain" in system
        assert grabs == [False]
        await o._router.match("read the whole screen").run()
        assert grabs == [False, True]
        print("OK  through the router: FREE, the window's picture and his words go to the model")

        o._llm = FakeLLM(error=LLMError("connection reset"))
        spoken = await o._router.match("what's on my screen").run()
        assert spoken == "I can't see the screen right now - Groq's image model didn't answer.", spoken

        def locked(whole):
            raise CaptureError("screen grab failed")

        registry.capture_screen = locked
        spoken = await o._router.match("what's on my screen").run()
        assert spoken == "I couldn't take a picture of the screen.", spoken
        print("OK  a dead network or a locked screen is said, not raised")

        print("\nAll screen checks passed.")
    finally:
        registry.capture_screen = real_capture
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())

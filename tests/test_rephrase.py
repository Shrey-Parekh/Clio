"""The model rewording a request the router missed (7.6).

The model is faked: these checks are about what happens *around* its answer.
It is only asked when nothing matched and the words sound like an order; its
sentence goes through the real router, so a made-up answer matches nothing;
what it picked is always read back, even for a harmless action; and deleting,
sending and powering off are never on offer.

Run: python tests/test_rephrase.py
"""

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clio.capabilities import files  # noqa: E402
from clio.capabilities.rephrase import EXAMPLES, clean, looks_like_action  # noqa: E402
from clio.core.config import ProjectConfig  # noqa: E402
from clio.core.permissions import Permission  # noqa: E402
from clio.orchestrator import Orchestrator  # noqa: E402


class FakeLLM:
    def __init__(self):
        self.answer = "NONE"
        self.calls = 0
        self.fail = False

    async def complete(self, messages, tier="default"):
        self.calls += 1
        if self.fail:
            raise RuntimeError("groq is down")
        return self.answer


async def main():
    base = Path(tempfile.mkdtemp(prefix="clio-rephrase-"))
    try:
        # --- only orders are worth the extra call ---

        for text in ["can you get the ewaste thing going", "hey clio, start the training",
                     "fire up the ewaste project", "please put on some music"]:
            assert looks_like_action(text), text
        for text in ["what do you think of rust", "i'm tired", "why is the sky blue",
                     "the ewaste thing is broken"]:
            assert not looks_like_action(text), text
        print("OK  only sentences that start like an order are reworded")

        # --- the model's answer, cleaned ---

        assert clean('"run the ewaste project".') == "run the ewaste project"
        assert clean("run the ewaste project\nThis starts the training.") == "run the ewaste project"
        assert clean("NONE") is None and clean("none.") is None and clean("") is None
        assert clean(None) is None
        assert clean("x" * 400) is None, "an essay is not a command"
        print("OK  one line, no quotes, NONE and essays dropped")

        # --- every example really is a phrase the router knows ---

        docs = base / "Documents"
        for name in ("ewaste", "clio"):
            (docs / name).mkdir(parents=True)
        (docs / "resume.pdf").write_bytes(b"%PDF-1.4")
        files.forget_index()
        llm = FakeLLM()
        o = Orchestrator(
            wake_detector=None, turn_detector=None, stt=None, llm=llm, speaker=None,
            persona_system_prompt="p", follow_up_window_s=1.0, file_roots=(docs,),
            memory_root=str(base / "memory"),
            projects={n: ProjectConfig(name=n, path=docs / n, command="python x.py")
                      for n in ("ewaste", "clio")},
        )
        # "close" only claims a window that is open right now, so it can't be
        # checked on a test machine.
        for name, sentence in EXAMPLES.items():
            if name == "close":
                continue
            matched = o._router.match(sentence)
            assert matched is not None and matched.intent == name, (name, matched)
        for never in ("file_delete", "file_move", "send", "power", "repeat", "stop"):
            assert never not in EXAMPLES, f"{never} must come from his own words"
        print("OK  every example matches its own intent; delete, send and power are never offered")

        # --- through the orchestrator ---

        llm.answer = "run the ewaste project"
        guessed = await o._reworded("can you get the ewaste thing going")
        assert guessed.intent == "project_run", guessed
        assert guessed.permission is Permission.CONFIRM
        assert guessed.description.startswith("You mean: run the ewaste project"), guessed.description

        llm.answer = "flip a coin"
        guessed = await o._reworded("do a coin toss for me")
        assert guessed.intent == "chance"
        assert guessed.permission is Permission.CONFIRM, "a guess is read back even when harmless"
        assert guessed.description == "You mean: flip a coin"

        calls = llm.calls
        assert await o._reworded("what do you think of rust") is None
        assert llm.calls == calls, "a question never costs the extra call"

        llm.answer = "delete resume"
        assert await o._reworded("get rid of my resume") is None, "deleting is not offered"
        llm.answer = "NONE"
        assert await o._reworded("do the thing with the stuff") is None
        llm.answer = "launch the rocket to mars"
        assert await o._reworded("launch the rocket") is None, "an answer no parser knows falls to chat"
        # Found live: with no projects registered, "run the ewaste project" was
        # claimed by "open" - which knew nothing called that - and read back as
        # if it would run. A guess that opens nothing is not offered.
        llm.answer = "run the zzqx project"
        assert await o._reworded("can you get the zzqx thing going") is None
        llm.fail = True
        assert await o._reworded("start the ewaste thing") is None, "a model failure is just chat"
        llm.fail = False
        print("OK  guesses are read back; questions, deletes, nonsense and failures fall to chat")

        # Typed: the readback waits for a yes, and nothing has run yet.
        llm.answer = "run the ewaste project"
        o._typed = True
        spoken, used_llm = await o._route("can you get the ewaste thing going")
        assert spoken.startswith("You mean: run the ewaste project") and "Say yes" in spoken, spoken
        assert used_llm, "the rewording call is counted"
        assert o._jobs.running() == [], "nothing runs before he says yes"
        o._typed = False
        # And a sentence the router already knows never reaches the model.
        calls = llm.calls
        await o._route("flip a coin")
        assert llm.calls == calls
        print("OK  typed guesses wait for a yes; known phrases never reach the model")

        print("\nAll rephrase checks passed.")
    finally:
        files.forget_index()
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())

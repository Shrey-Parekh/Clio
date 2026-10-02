"""One real conversation, 2026-10-02, and the three things in it that went wrong.

1. "Stop." silenced her for the rest of the conversation. The stop is handled
   by saying nothing, which left his "Stop." in the history with no reply after
   it. The persona says "if he tells you to stop, say nothing" - so on every
   later turn the model re-read that and stayed silent. Replayed against Groq
   three times: empty reply each time, reasoning "user said stop earlier, so we
   must be silent".
2. "Open Microsoft Word" opened Microsoft Edge. Word is installed as "word";
   "microsoft word" is contained in no app name, and the fuzzy fallback found
   "microsoft edge" the closest string because they share a vendor.
3. "Tell me more about your features" was answered from imagination ("a faster
   brain... juggle code snippets"), because the model is never told what she
   can do and the question missed the help intent.

Run: python tests/test_conversation_gone_wrong.py
"""

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clio.capabilities.assistant import parse_help_request  # noqa: E402
from clio.capabilities.launch import _best_app  # noqa: E402
from clio.orchestrator import Orchestrator  # noqa: E402


class FakeLLM:
    async def complete(self, messages, tier="default"):
        return "reply"


async def main():
    # --- 2. the app he named, not the one that looks like it ---

    apps = {name: f"C:/apps/{name}.lnk" for name in (
        "word", "excel", "microsoft edge", "google chrome", "visual studio code", "spotify")}
    assert _best_app("microsoft word", apps) == "word", _best_app("microsoft word", apps)
    assert _best_app("microsoft excel", apps) == "excel"
    assert _best_app("word document", apps) == "word", "a Word document is opened with Word"
    assert _best_app("microsoft powerpoint", apps) is None, \
        "not installed means not found - never the nearest Microsoft thing"
    # What already worked still does.
    assert _best_app("chrome", apps) == "google chrome"
    assert _best_app("edge", apps) == "microsoft edge"
    assert _best_app("microsoft edge", apps) == "microsoft edge"
    assert _best_app("visual studio cod", apps) == "visual studio code", "a slip of the tongue"
    assert _best_app("spotfy", apps) == "spotify"
    print("OK  'Microsoft Word' opens Word, and an app that isn't installed opens nothing")

    # --- 3. her features are the real ones ---

    for text in ["Alright, tell me more about your features.", "what are your features",
                 "what can you do", "What can you open in my computer?"]:
        assert parse_help_request(text) == "help", text
    assert parse_help_request("open the features folder") is None

    base = Path(tempfile.mkdtemp(prefix="clio-gone-wrong-"))
    o = Orchestrator(
        wake_detector=None, turn_detector=None, stt=None, llm=FakeLLM(), speaker=None,
        persona_system_prompt="PERSONA", follow_up_window_s=1.0, memory_root=str(base / "m"))
    system = o._memory.get_messages()[0]["content"]
    assert system.startswith("PERSONA"), "the persona still leads"
    assert "set timers" in system and "open apps and folders" in system, \
        "the model is told what she can really do"
    assert "can't do that yet" in system, "and to say so when it's not on the list"
    print("OK  questions about her features get the real list, and the model knows it")

    # --- 1. a stop ends one reply, not the conversation ---

    o._memory.add_user("Stop.")
    o._close_silent_turn("Stop.")
    last = o._memory.get_messages()[-1]
    assert last["role"] == "assistant" and "answer whatever he says next" in last["content"], last

    before = len(o._memory.get_messages())
    o._memory.add_user("pause the music")
    o._close_silent_turn("pause the music")
    assert len(o._memory.get_messages()) == before + 1, "only a stop needs closing"
    print("OK  after 'Stop' the history says the stop is finished")

    print("\nAll gone-wrong checks passed.")


if __name__ == "__main__":
    asyncio.run(main())

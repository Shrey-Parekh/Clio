"""Supervised multi-step tasks (7.7).

The model is faked; the steps are real processes in a temp project. What is
checked is the supervision: the plan is read back whole and nothing runs before
the yes; every step must be something she knows, or none of it runs; a slow
step is waited on so the next one finds it finished; a failure stops the chain
and says where; a step that destroys something still asks on its own; and the
time budget ends a plan that runs long.

Run: python tests/test_plans.py
"""

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import clio.orchestrator as orchestrator_module  # noqa: E402
from clio.capabilities import files  # noqa: E402
from clio.capabilities.rephrase import looks_like_plan, parse_steps  # noqa: E402
from clio.core import jobs  # noqa: E402
from clio.core.config import ProjectConfig  # noqa: E402
from clio.core.permissions import Permission  # noqa: E402
from clio.orchestrator import Orchestrator  # noqa: E402


class FakeLLM:
    """The plan comes from the reasoning tier, a single guess from the fast one."""

    def __init__(self):
        self.plan = "NONE"
        self.guess = "NONE"
        self.calls = 0

    async def complete(self, messages, tier="default"):
        self.calls += 1
        return self.guess if tier == "fast" else self.plan


async def wait_idle(runner):
    for _ in range(80):
        if not runner.running():
            return
        await asyncio.sleep(0.25)


async def main():
    base = Path(tempfile.mkdtemp(prefix="clio-plans-"))
    jobs.toast = lambda title, text: None
    try:
        # --- the model's answer ---

        assert parse_steps("1. run git pull in ewaste\n2) run pip list in ewaste\n\n- run the ewaste project") == [
            "run git pull in ewaste", "run pip list in ewaste", "run the ewaste project"]
        assert parse_steps("run git pull in ewaste\nNONE") is None, "a hole means no plan"
        assert parse_steps("") is None and parse_steps(None) is None
        assert looks_like_plan("pull the latest ewaste code, install its requirements and start training")
        assert looks_like_plan("hey clio, start the ewaste project then check my email")
        assert not looks_like_plan("what's up and why is it slow"), "a question is not a plan"
        assert not looks_like_plan("start the ewaste project"), "one part is 7.6, not a plan"
        print("OK  numbered or not, a plan is its lines; a NONE anywhere drops it")

        # --- a real project to run things in ---

        docs = base / "Documents"
        project = docs / "ewaste"
        project.mkdir(parents=True)
        (project / "prep.py").write_text(
            "open('prepped.txt', 'w').write('x')\nprint('prepared')\n", encoding="utf-8")
        (project / "slow.py").write_text(
            "import time\ntime.sleep(3)\nopen('slow_done.txt', 'w').write('x')\nprint('slow done')\n",
            encoding="utf-8")
        (project / "needs_slow.py").write_text(
            "import os\nassert os.path.exists('slow_done.txt'), 'ran too early'\nprint('found it')\n",
            encoding="utf-8")
        (project / "broken.py").write_text("import no_such_module_here\n", encoding="utf-8")
        (project / "after.py").write_text("open('after.txt', 'w').write('x')\n", encoding="utf-8")
        (project / "train.py").write_text("print('training')\n", encoding="utf-8")
        files.forget_index()

        llm = FakeLLM()
        o = Orchestrator(
            wake_detector=None, turn_detector=None, stt=None, llm=llm, speaker=None,
            persona_system_prompt="p", follow_up_window_s=1.0, file_roots=(docs,),
            memory_root=str(base / "memory"),
            projects={"ewaste": ProjectConfig(name="ewaste", path=project,
                                              command="python train.py")},
        )
        shown = []

        async def emit(name, payload=None):
            shown.append((name, (payload or {}).get("text", "")))
        o._emit = emit
        o._typed = True     # the typed path: a pending yes, so the test can say it

        # --- read back whole, nothing before the yes ---

        llm.plan = ("run python prep.py in ewaste\nrun python slow.py in ewaste\n"
                    "run python needs_slow.py in ewaste\nrun the ewaste project")
        spoken, used_llm = await o._route(
            "do prep in ewaste, then slow, then needs_slow and start the ewaste project")
        assert spoken.startswith("Four steps. One, Running python prep.py, in ewaste. "
                                 "Two, Running python slow.py, in ewaste."), spoken
        assert "Say yes" in spoken and used_llm, spoken
        assert not (project / "prepped.txt").exists(), "nothing runs before the yes"
        print("OK  the whole plan is read back, and nothing runs before the yes")

        # --- the yes: in order, slow step waited on, long job handed off ---

        pending = o._take_pending()
        assert pending is not None and pending.permission is Permission.CONFIRM
        reply = await pending.run()
        assert (project / "prepped.txt").exists() and (project / "slow_done.txt").exists()
        assert "found it" in reply, ("the step after the slow one found it finished", reply)
        steps_shown = [t for n, t in shown if t.startswith("Step ")]
        assert steps_shown[0] == "Step 1 of 4: Running python prep.py, in ewaste", steps_shown
        assert len(steps_shown) == 4, steps_shown
        await wait_idle(o._jobs)
        print("OK  steps run in order, a slow one is waited on, progress goes to the window")

        # --- a failure stops the chain ---

        llm.plan = "run python broken.py in ewaste\nrun python after.py in ewaste"
        await o._route("run the broken one and then the after one")
        reply = await o._take_pending().run()
        assert "Stopped at step 1" in reply and "no_such_module_here" in reply, reply
        assert "haven't done the step after it" in reply, reply
        assert not (project / "after.txt").exists(), "nothing after a failure runs"
        print("OK  a failure stops the chain and says what didn't run")

        # --- every step must be something she knows ---

        calls = llm.calls
        llm.plan = "run python prep.py in ewaste\nlaunch the rocket to mars"
        spoken, _ = await o._route("do prep in ewaste and launch the rocket")
        assert "couldn't turn 'launch the rocket to mars'" in spoken, spoken
        assert o._take_pending() is None, "nothing waits for a yes"
        assert llm.calls == calls + 1, "one call for the plan, no second guess"
        # Found live: his ewaste isn't registered, so the plan's last step
        # matched nothing. The refusal says the fix, not just "couldn't".
        llm.plan = "run git pull in ewaste\nrun the sorter project"
        spoken, _ = await o._route("pull the sorter code and start training")
        assert "sorter isn't one of your registered projects" in spoken, spoken
        assert "register sorter" in spoken, spoken
        # Found live, one run in three: "start training" became a script name
        # nobody said. A file in a guessed command must be one he named.
        llm.plan = "run python prep.py in ewaste\nrun python train.py in ewaste"
        spoken, _ = await o._route("do prep in ewaste and start training")
        assert "couldn't turn 'run python train.py in ewaste'" in spoken, spoken
        # And when the planner gives up on several parts, a single guess must
        # not quietly do half of it.
        llm.plan, llm.guess = "NONE", "run python prep.py in ewaste"
        assert await o._route("do prep in ewaste and then the other thing") is None
        llm.plan = "\n".join(["flip a coin"] * 7)
        spoken, _ = await o._route("do a coin toss seven times, then again and again")
        assert "more than I'll run in one go" in spoken, spoken
        llm.plan, llm.guess = "NONE", "NONE"
        assert await o._route("do the thing and the other thing") is None, "no plan is conversation"
        print("OK  a step she doesn't know, or too many, and nothing runs")

        # --- one step is 7.6, a destroying step asks on its own ---

        llm.plan = "flip a coin"
        spoken, _ = await o._route("do a coin toss and tell me")
        assert spoken.startswith("You mean: flip a coin"), spoken
        o._take_pending()

        llm.plan = "run python prep.py in ewaste\nrun pip uninstall requests in ewaste"
        await o._route("do prep in ewaste and then remove requests")
        reply = await o._take_pending().run()
        assert "Stopped at step 2" in reply and "needs its own yes" in reply, reply
        print("OK  a one-step plan is a single guess; uninstalling still asks on its own")

        # --- the time budget ---

        orchestrator_module._PLAN_BUDGET_S = 1.0
        llm.plan = "run python slow.py in ewaste\nrun python after.py in ewaste"
        await o._route("do the slow bit then the after one")
        reply = await o._take_pending().run()
        assert "Stopped at step 1" in reply and "carrying on in the background" in reply, reply
        assert not (project / "after.txt").exists()
        await wait_idle(o._jobs)
        print("OK  past the budget, a slow step carries on alone and the rest waits")

        print("\nAll plan checks passed.")
    finally:
        files.forget_index()
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())

"""Registering projects and running them (7.1, 7.2).

These start real processes - short-lived Python ones, in a temp folder - because
the interesting failures are real: a job that outlives its turn, a pid that gets
reused, a log that is still being written while she reads it.

What matters most here is what she refuses. A command is looked up, never
assembled from a sentence, so an unregistered name must fall through rather than
run anything.

Run: python tests/test_projects.py
"""

import asyncio
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clio.capabilities.projects import (  # noqa: E402
    ProjectCapability, ProjectRequest, parse_project_request,
)
from clio.core.config import ProjectConfig  # noqa: E402
from clio.core.jobs import Job, JobRunner  # noqa: E402
from clio.core.permissions import Permission  # noqa: E402
from clio.llm.memory import ConversationMemory  # noqa: E402
from clio.orchestrator import Orchestrator  # noqa: E402

PYTHON = sys.executable


class FakeLLM:
    async def complete(self, messages, tier="default"):
        return "it's training."


async def main():
    root = Path(tempfile.mkdtemp(prefix="clio-projects-"))
    said = []

    async def announce(text):
        said.append(text)

    try:
        # --- what counts as a project request ---

        for text, expected in [
            ("what projects do you know", ProjectRequest("list")),
            ("what projects can you see", ProjectRequest("scan")),
            ("register the ewaste project", ProjectRequest("register", "ewaste")),
            ("run the ewaste training", ProjectRequest("run", "ewaste training")),
            ("start ewaste", ProjectRequest("run", "ewaste")),
            ("is the ewaste training still running", ProjectRequest("status", "ewaste training")),
            ("how's the training going", ProjectRequest("status", "training")),
            ("what's running", ProjectRequest("status")),
            ("stop the training", ProjectRequest("stop", "training")),
            ("show me the training log", ProjectRequest("log", "training")),
        ]:
            assert parse_project_request(text) == expected, (text, parse_project_request(text))

        for text in ["what time is it", "run", "stop", "start a timer for five minutes"]:
            assert parse_project_request(text) is None, text
        print("OK  project sentences matched, and bare 'run' or 'stop' claimed nothing")

        # --- a real job, started and watched ---

        work = root / "work"
        work.mkdir()
        (work / "train.py").write_text(
            "import time\n"
            "for i in range(1, 4):\n"
            "    print(f'epoch {i}/3', flush=True)\n"
            "    time.sleep(0.3)\n"
            "print('done', flush=True)\n", encoding="utf-8")

        runner = JobRunner(root / "memory", announce=announce)
        job = runner.start("trainer", f'"{PYTHON}" train.py', work)
        assert runner.is_alive(job), "it should still be going"
        assert (root / "memory" / "jobs.json").exists(), "a job outlives the process that made it"
        assert runner.find("trainer") is not None
        assert runner.find("the trainer") is not None, "he won't say the config key exactly"

        # Progress is read out of the log, not guessed.
        await asyncio.sleep(0.7)
        assert runner.progress(job) in ("1 of 3", "2 of 3", "3 of 3"), runner.progress(job)

        runner.watch(job)
        for _ in range(40):
            if said:
                break
            await asyncio.sleep(0.25)
        assert said == ["trainer has finished."], said
        assert runner.running() == [], "a finished job is swept, not left in the file"
        print(f"OK  a real job ran, reported progress from its log, and announced: {said[0]!r}")

        # --- a job that ends badly says so ---

        (work / "broken.py").write_text("raise ValueError('nope')\n", encoding="utf-8")
        said.clear()
        bad = runner.start("broken", f'"{PYTHON}" broken.py', work)
        runner.watch(bad)
        for _ in range(40):
            if said:
                break
            await asyncio.sleep(0.25)
        assert said and "ends in an error" in said[0], said
        print(f"OK  a failed job is reported as one: {said[0]!r}")

        # --- a pid that got reused is not somebody else's process to kill ---

        stale = Job(name="ghost", pid=job.pid, command="x", folder=str(work),
                    log=job.log, started=time.time(), created=time.time() + 5_000)
        assert JobRunner.is_alive(stale) is False, "same pid, different start, different process"
        print("OK  a reused pid is treated as gone rather than killed")

        # --- stopping one that is genuinely running ---

        (work / "forever.py").write_text(
            "import time\nwhile True:\n    time.sleep(0.2)\n", encoding="utf-8")
        long_running = runner.start("forever", f'"{PYTHON}" forever.py', work)
        assert runner.is_alive(long_running)
        assert runner.stop("forever") == "Stopped forever."
        await asyncio.sleep(0.5)
        assert not runner.is_alive(long_running), "it should actually be dead"
        assert runner.stop("forever") == "There's no forever running."
        print("OK  a running job is stopped, and stopping nothing says so")

        # --- the registry: proposing, approving, and only then running ---

        config_file = root / "config.toml"
        config_file.write_text("# his own comments\n[runtime]\nlog_level='INFO'\n",
                               encoding="utf-8")

        scanned = root / "code"
        (scanned / "sorter" / "sorter").mkdir(parents=True)
        (scanned / "sorter" / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        (scanned / "sorter" / "sorter" / "__main__.py").write_text("print(1)", encoding="utf-8")
        (scanned / "notes").mkdir()        # no manifest: not a project
        (scanned / "notes" / "a.txt").write_text("hi", encoding="utf-8")

        capability = ProjectCapability({}, (scanned,), runner, config_path=config_file)
        assert "don't know how to run anything yet" in capability.listing()
        assert capability.resolve("sorter") is None, "nothing is runnable before he approves it"

        spoken = capability.scan()
        assert "sorter" in spoken and "python -m sorter" in spoken, spoken
        assert "notes" not in spoken, "a folder with no way to run it is not proposed"
        assert capability.resolve("sorter") is None, "scanning proposes; it must not register"
        assert config_file.read_text(encoding="utf-8").count("[projects") == 0

        spoken = capability.register("sorter")
        assert "Registered sorter" in spoken, spoken
        assert capability.resolve("sorter") is not None
        written = config_file.read_text(encoding="utf-8")
        assert "[projects.sorter]" in written and 'command = "python -m sorter"' in written
        assert "# his own comments" in written, "his config is appended to, never rewritten"
        assert "Registered" not in capability.register("sorter"), "registering twice is a no-op"
        assert "haven't got a dishwasher" in capability.register("dishwasher")
        print(f"OK  proposed, approved, then written into config: {spoken!r}")

        # --- the readback the gate speaks, and running a registered project ---

        capability = ProjectCapability(
            {"trainer": ProjectConfig(name="trainer", path=work,
                                      command=f'"{PYTHON}" train.py', aliases=("the training",))},
            (scanned,), runner, config_path=config_file)

        readback = capability.describe_run("the training")
        assert readback.startswith("Running trainer:") and "work" in readback, readback
        assert capability.describe_run("dishwasher") == "", "nothing to read back, nothing to confirm"

        said.clear()
        spoken = await capability.run("the training")
        assert "trainer is running" in spoken, spoken
        assert "already running" in await capability.run("trainer"), "no starting it twice"
        status = await capability.status("trainer")
        assert "has been going" in status, status
        await asyncio.sleep(0.6)      # let it actually print something first
        spoken, tail = await capability.log_tail("trainer")
        assert "epoch" in tail, tail
        assert "Stopped trainer" in await capability.stop("trainer")

        assert "I don't know a project called dishwasher" in await capability.run("dishwasher")
        print(f"OK  readback, run, status and stop: {readback!r}")

        # --- through the real router ---

        orchestrator = Orchestrator(
            wake_detector=None, turn_detector=None, stt=None, llm=FakeLLM(), speaker=None,
            persona_system_prompt="p", follow_up_window_s=1.0,
            memory_root=str(root / "memory"),
            projects={"trainer": ProjectConfig(name="trainer", path=work, command="echo hi",
                                               aliases=("the training",))},
        )
        orchestrator._memory = ConversationMemory(provider=FakeLLM(), system_prompt="p")

        caps = {c.name: c for c in orchestrator._router.capabilities()}
        assert caps["project_run"].permission is Permission.CONFIRM
        assert caps["project_stop"].permission is Permission.CONFIRM
        assert caps["projects"].permission is Permission.FREE

        matched = orchestrator._router.match("run the training")
        assert matched is not None and matched.intent == "project_run", matched
        assert "Running trainer" in matched.description, matched.description

        matched = orchestrator._router.match("stop the training")
        assert matched is not None and matched.intent == "project_stop", matched

        # An unregistered name must not become a job - it belongs to whatever
        # owns those words otherwise.
        matched = orchestrator._router.match("run the dishwasher")
        assert matched is None or matched.intent != "project_run", matched
        matched = orchestrator._router.match("stop the timer")
        assert matched is not None and matched.intent == "timer_control", matched
        matched = orchestrator._router.match("what's running")
        assert matched is not None and matched.intent == "projects", matched
        print("OK  free to look, confirm to run or stop, and unregistered names fall through")

        print("\nAll project checks passed.")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())

"""The coding agent (9.2).

Real git, in a throwaway repo; Claude Code itself is faked by a job runner
that edits the agent's copy the way it would. No network, no Claude usage.
What is checked: the phrases; starting is refused with the reason when the
project is unknown, isn't a git repo, or an agent's work is already waiting;
the agent works in its own worktree and branch while his folder stays exactly
as it was; Claude Code is called directly with the task as one argument, only
tests and read-only git allowed, no web; when it ends, its work is committed on
its branch and reported; merging and throwing away ask first, merging refuses a
dirty folder; a run that changed nothing tidies itself away; and one that ended
while Clio was closed is still reported.

Run: python tests/test_agent.py
"""

import asyncio
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clio.capabilities.agent import CodingAgent, parse_agent_request  # noqa: E402
from clio.core.permissions import Permission  # noqa: E402
from clio.orchestrator import Orchestrator  # noqa: E402


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


class Job:
    def __init__(self, folder, log):
        self.name, self.folder, self.log_text, self.started, self.alive = "agent", folder, log, time.time(), False


class FakeJobs:
    """Starts "Claude Code" by running `work` on its folder, synchronously."""

    def __init__(self, work, said="I fixed the off-by-one in parse.py. All 3 tests pass."):
        self.work, self.said, self.job, self.commands = work, said, None, []

    def start(self, name, command, folder):
        self.commands.append(command)
        self.work(Path(folder))
        self.job = Job(folder, self.said)
        return self.job

    def find(self, name):
        return self.job

    def is_alive(self, job):
        return job.alive

    def tail(self, job, lines=30):
        return job.log_text

    def forget(self, name):
        self.job = None

    def stop(self, name):
        if self.job:
            self.job.alive = False


def fix(folder):
    (folder / "parse.py").write_text("def parse(x):\n    return x + 1\n", encoding="utf-8")


async def main():
    for said, kind in (("get the agent to fix the failing test in ewaste", "start"),
                       ("have claude fix the bug in the parser in ewaste", "start"),
                       ("how's the agent doing", "status"), ("stop the agent", "stop"),
                       ("what did the agent change", "show"), ("merge the agent's work", "merge"),
                       ("throw away the agent's work", "discard")):
        request = parse_agent_request(said)
        assert request is not None and request.kind == kind, (said, request)
    request = parse_agent_request("have claude fix the bug in the parser in ewaste")
    assert (request.task, request.where) == ("fix the bug in the parser", "ewaste"), request
    assert parse_agent_request("stop the music") is None
    print("OK  agent phrases; the task runs to the last 'in', the project after it")

    base = Path(tempfile.mkdtemp())
    repo = base / "ewaste"
    try:
        repo.mkdir()
        git("init", "-q", "-b", "main", cwd=repo)
        git("config", "user.email", "t@example.com", cwd=repo)
        git("config", "user.name", "Test", cwd=repo)
        (repo / "parse.py").write_text("def parse(x):\n    return x\n", encoding="utf-8")
        git("add", "-A", cwd=repo)
        git("commit", "-q", "-m", "start", cwd=repo)
        plain = base / "notes"
        plain.mkdir()
        folders = {"ewaste": repo, "notes": plain}
        jobs = FakeJobs(fix)
        claude = base / "claude.exe"
        claude.write_text("", encoding="utf-8")
        announced = []

        async def announce(text):
            announced.append(text)

        agent = CodingAgent(base / "memory", jobs, folders.get, announce=announce, claude=lambda: str(claude))

        assert "can't find a project called nowhere" in agent.plan(parse_agent_request(
            "get the agent to fix it in nowhere"))[0]
        assert "isn't a git repo" in agent.plan(parse_agent_request("get the agent to fix it in notes"))[0]
        assert "can't find Claude Code" in CodingAgent(base / "m0", jobs, folders.get, claude=lambda: None).plan(
            parse_agent_request("get the agent to fix it in ewaste"))[0]
        print("OK  an unknown project, a folder that isn't a repo, or no Claude Code is said, not started")

        o = Orchestrator(wake_detector=None, turn_detector=None, stt=None, llm=None, speaker=None,
                         persona_system_prompt="p", follow_up_window_s=1.0, memory_root=str(base / "m"))
        o._agent = agent
        caps = {c.name: c for c in o._router.capabilities()}
        assert caps["agent"].permission is Permission.FREE
        for name in ("agent_start", "agent_merge", "agent_discard"):
            assert caps[name].permission is Permission.CONFIRM, name

        matched = o._router.match("get the agent to fix the off by one in ewaste")
        assert matched.intent == "agent_start", matched.intent
        assert matched.description.startswith("Starting Claude Code on ewaste, in its own copy, to fix the off by one")
        said = await matched.run()
        assert "started on ewaste" in said, said
        command = jobs.commands[-1]
        assert command[0] == str(claude) and not command[0].endswith(".cmd"), "the program itself, no shim"
        assert command[1] == "-p" and command[2].startswith("fix the off by one\n"), "the task is one argument"
        assert "Bash(npm test:*)" in command and "Bash(git diff:*)" in command
        assert "WebFetch" in command[command.index("--disallowedTools"):]
        assert not any(c.startswith("Bash(git push") or c == "Bash" for c in command)
        run = agent.current()
        worktree = Path(run.worktree)
        assert worktree.is_dir() and run.branch.startswith("clio/agent-fix-the-off-by-one")
        assert git("status", "--porcelain", cwd=repo) == "" and "x + 1" not in (repo / "parse.py").read_text()
        assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo) == "main", "his branch is untouched"
        print("OK  started on CONFIRM, in its own worktree; his folder and branch untouched; no shell, no web")

        await agent._watcher
        assert announced and announced[-1].startswith("The agent's done on ewaste: 1 file changed. "
                                                     "I fixed the off-by-one in parse.py."), announced
        assert git("log", "-1", "--format=%s", run.branch, cwd=repo) == "Agent: fix the off by one"
        refused = o._router.match("get the agent to add docs in ewaste")
        assert refused.intent == "agent" and "still waiting" in await refused.run()
        spoken = await o._router.match("what did the agent change").run()
        assert spoken.startswith("1 file changed. I fixed"), spoken
        print("OK  when it ends: committed on its branch, reported; a second agent waits for review")

        (repo / "scratch.txt").write_text("mine", encoding="utf-8")
        matched = o._router.match("merge the agent's work")
        assert matched.intent == "agent" and "uncommitted changes" in await matched.run()
        (repo / "scratch.txt").unlink()
        matched = o._router.match("merge the agent's work")
        assert matched.intent == "agent_merge", matched.intent
        assert "into main - 1 file changed, locally, not pushed" in matched.description, matched.description
        assert await matched.run() == "Merged into main. Not pushed - that's yours to do."
        assert "x + 1" in (repo / "parse.py").read_text() and not worktree.exists()
        assert agent.current() is None and run.branch not in git("branch", cwd=repo)
        print("OK  merging refuses a dirty folder, then asks first, lands locally and tidies up")

        agent._jobs = FakeJobs(lambda folder: (folder / "docs.md").write_text("# Docs", encoding="utf-8"))
        await o._router.match("get the agent to write docs in ewaste").run()
        await agent._watcher
        matched = o._router.match("throw away the agent's work")
        assert matched.intent == "agent_discard" and "can't be undone" in matched.description
        assert "as you left it" in await matched.run()
        assert agent.current() is None and not (repo / "docs.md").exists()
        print("OK  throwing it away asks first and leaves his project as it was")

        agent._jobs = FakeJobs(lambda folder: None, said="Nothing needed changing.")
        await o._router.match("get the agent to check the tests in ewaste").run()
        await agent._watcher
        assert "without changing anything" in announced[-1] and agent.current() is None
        print("OK  a run that changed nothing reports so and tidies itself away")

        agent._jobs = FakeJobs(lambda folder: (folder / "notes.md").write_text("n", encoding="utf-8"))
        await o._router.match("get the agent to write notes in ewaste").run()
        agent._watcher.cancel()
        agent._jobs.job = None   # Clio was closed; startup swept the job list
        agent._watcher = None
        agent.watch()
        await agent._watcher
        assert announced[-1].startswith("The agent's done on ewaste: 1 file changed."), announced[-1]
        await o._router.match("throw away the agent's work").run()
        print("OK  an agent that finished while Clio was closed is still reported")

        print("\nAll agent checks passed.")
    finally:
        subprocess.run(["git", "worktree", "prune"], cwd=repo, capture_output=True)
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())

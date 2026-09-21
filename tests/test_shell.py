"""Spoken commands (7.5) - the riskiest thing in Clio, so mostly a test of refusal.

Real processes in a temp folder. The most important check here is not that a
command runs; it is that `a&&echo PWNED` reaches a program as one plain
argument, because there is no shell anywhere to interpret it. That is the design
in one assertion, and it holds even if a filter above it ever has a gap.

Run: python tests/test_shell.py
"""

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clio.capabilities import files, shellcmd  # noqa: E402
from clio.capabilities.projects import ProjectCapability  # noqa: E402
from clio.capabilities.shellcmd import ShellCommands, parse_command  # noqa: E402
from clio.core import jobs, shell  # noqa: E402
from clio.core.config import ProjectConfig  # noqa: E402
from clio.core.jobs import JobRunner  # noqa: E402
from clio.core.permissions import Permission  # noqa: E402
from clio.llm.memory import ConversationMemory  # noqa: E402
from clio.orchestrator import Orchestrator  # noqa: E402

PYTHON = sys.executable


class FakeLLM:
    async def complete(self, messages, tier="default"):
        return "reply"


async def main():
    base = Path(tempfile.mkdtemp(prefix="clio-shell-"))
    jobs.toast = lambda title, text: None      # no real notifications from a test
    try:
        # --- what may run at all ---

        for words in (["git", "status"], ["pip", "install", "requests"],
                      ["pip", "install", "numpy==2.1.0"], ["python", "train.py", "--epochs", "5"],
                      ["npm", "run", "dev"], ["cargo", "build", "--release"]):
            assert shell.vet(words).refusal == "", (words, shell.vet(words).refusal)

        for tool in ("cmd", "powershell", "wsl", "bash", "curl", "certutil", "npx",
                     "rm", "del", "format", "reg", "diskpart", "shutdown", "netsh"):
            refusal = shell.vet([tool, "x"]).refusal
            assert refusal.startswith(f"I won't run {tool}"), (tool, refusal)
        assert "isn't something I'm set up to run" in shell.vet(["ffmpeg", "-i", "x"]).refusal
        print("OK  known tools pass; interpreters, downloaders and system tools refused by name")

        for words in (["git", "status;", "del", "x"], ["pip", "install", "a&&b"],
                      ["git", "log", "|", "more"], ["python", "x.py", ">", "out.txt"],
                      ["git", "log", "`whoami`"], ["pip", "install", "$(evil)"]):
            assert "characters I don't pass on" in shell.vet(words).refusal, words
        for words in (["python", "C:\\Windows\\evil.py"], ["python", "/etc/passwd"],
                      ["python", "..\\..\\secrets.py"]):
            assert "not with paths out of it" in shell.vet(words).refusal, words
        for words in (["pip", "install", "https://evil.example/pkg.whl"],
                      ["pip", "install", "git+https://github.com/x/y"]):
            assert "install from a link" in shell.vet(words).refusal, words
        assert "code typed inline" in shell.vet(["python", "-c", "print"]).refusal
        assert "code typed inline" in shell.vet(["node", "-e", "x"]).refusal
        print("OK  chaining, redirects, escaping paths, URLs and inline code all refused")

        # --- what runs free, and what gets a warning ---

        assert shell.vet(["git", "status"]).read_only
        assert shell.vet(["git", "log", "--oneline", "-5"]).read_only
        assert shell.vet(["pip", "list"]).read_only
        assert shell.vet(["git", "branch"]).read_only
        assert not shell.vet(["git", "branch", "-D", "old"]).read_only, \
            "deleting a branch starts with the word that lists them"
        assert shell.vet(["nvidia-smi"]).read_only
        assert not shell.vet(["nvidia-smi", "-r"]).read_only, "nvidia-smi -r resets the GPU"
        assert not shell.vet(["pip", "install", "x"]).read_only

        assert "haven't committed" in shell.vet(["git", "reset", "--hard"]).warning
        assert "isn't tracking" in shell.vet(["git", "clean", "-fd"]).warning
        assert "overwrite history" in shell.vet(["git", "push", "--force"]).warning
        assert "deletes a branch" in shell.vet(["git", "branch", "-D", "old"]).warning
        assert "removes a package" in shell.vet(["pip", "uninstall", "x"]).warning
        assert shell.vet(["git", "reset", "HEAD", "x.py"]).warning == "", \
            "a soft reset destroys nothing"
        print("OK  looking runs free, and destructive commands say what they destroy")

        # --- THE check: there is no shell ---

        work = base / "work"
        work.mkdir()
        (work / "echo_args.py").write_text(
            "import sys\nfor a in sys.argv[1:]:\n    print('ARG=' + a)\n", encoding="utf-8")
        runner = JobRunner(base / "memory")
        job = runner.start("argcheck", [PYTHON, "echo_args.py", "a&&echo", "PWNED|more"], work)
        for _ in range(40):
            if not runner.is_alive(job):
                break
            await asyncio.sleep(0.1)
        printed = runner.tail(job, 20)
        assert "ARG=a&&echo" in printed and "ARG=PWNED|more" in printed, printed
        assert "\nPWNED\n" not in f"\n{printed}\n", "a shell would have run echo PWNED"
        print("OK  metacharacters arrive as plain text - there is no shell to interpret them")

        # --- Python tools go through the interpreter, never the launcher ---

        # Found live, 2026-09-21: this project was once called Jarvis. Its venv
        # was created there and the folder renamed, so every pip.exe / pytest.exe
        # launcher still points at Documents\Jarvis\.venv - they exit 1 with no
        # output at all. "Run pip list in clio" said "didn't print anything".
        venv_project = base / "venvproj"
        (venv_project / ".venv" / "Scripts").mkdir(parents=True)
        interpreter = venv_project / ".venv" / "Scripts" / "python.exe"
        interpreter.write_bytes(b"")
        assert shell.command_for("pip", venv_project) == [str(interpreter), "-m", "pip"]
        assert shell.command_for("pytest", venv_project) == [str(interpreter), "-m", "pytest"]
        assert shell.command_for("python", venv_project) == [str(interpreter)]
        # Everything else is still found on the PATH, venv or not.
        expected_git = [shutil.which("git")] if shutil.which("git") else None
        assert shell.command_for("git", venv_project) == expected_git
        print("OK  pip and pytest run as 'python -m', so a renamed venv can't break them")

        # --- the sentence ---

        assert parse_command("run pip install requests in ewaste") == (
            ["pip", "install", "requests"], "ewaste")
        assert parse_command("in the clio project run git status") == (["git", "status"], "clio")
        assert parse_command("what's the git status of clio") == (["git", "status"], "clio")
        assert parse_command("Run git checkout Feature-X in clio") == (
            ["git", "checkout", "Feature-X"], "clio"), "branch names keep their case"
        assert parse_command("run powershell in clio") == (["powershell"], "clio"), \
            "a refused tool is still claimed, so he hears why"
        for text in ["run the tests in clio", "run the ewaste training", "what time is it",
                     "run to the shop in a minute"]:
            assert parse_command(text) is None, (text, parse_command(text))
        print("OK  only sentences that name a program are commands")

        # --- where it may run ---

        docs = base / "Documents"
        project_dir = docs / "ewaste"
        project_dir.mkdir(parents=True)
        (base / "Outside").mkdir()
        roots = (docs,)
        files.forget_index()
        projects = ProjectCapability(
            {"ewaste": ProjectConfig(name="ewaste", path=project_dir, command="python x.py",
                                     aliases=("the training",))},
            roots, runner)
        commands = ShellCommands(roots, projects, runner)

        request = commands.resolve("run pip install requests in the training")
        assert request.folder == project_dir, "a registered project's alias resolves"
        assert commands.resolve("run git status in documents").folder == docs.resolve()
        outside = commands.resolve("run git status in outside")
        assert "can't find a outside" in outside.refusal, outside
        assert ShellCommands.describe(request) == "Running pip install requests, in ewaste"
        warned = commands.resolve("run git reset --hard in ewaste")
        assert ShellCommands.describe(warned).endswith(
            "Careful - it throws away changes you haven't committed"), ShellCommands.describe(warned)
        print("OK  runs only in registered projects and the configured roots")

        # --- running: inline, failing, and handed off when slow ---

        (project_dir / "hello.py").write_text("print('hello from ewaste')\n", encoding="utf-8")
        spoken, output = await commands.run(commands.resolve("run python hello.py in ewaste"))
        assert spoken == "Done. hello from ewaste", spoken
        assert runner.running() == [], "a command that finished inline is not left as a job"

        (project_dir / "broken.py").write_text("import no_such_module_here\n", encoding="utf-8")
        spoken, _ = await commands.run(commands.resolve("run python broken.py in ewaste"))
        assert spoken == "That didn't work: the no_such_module_here module isn't installed.", spoken

        shellcmd.INLINE_S = 0.5
        (project_dir / "slow.py").write_text("import time\ntime.sleep(2)\nprint('done')\n",
                                             encoding="utf-8")
        spoken, _ = await commands.run(commands.resolve("run python slow.py in ewaste"))
        assert "carrying on in the background" in spoken, spoken
        assert any(j.name == "python slow.py" for j in runner.running()), runner.running()
        for _ in range(40):
            if not runner.running():
                break
            await asyncio.sleep(0.25)
        print("OK  quick commands answer inline, failures are explained, slow ones become jobs")

        # --- through the real router ---

        orchestrator = Orchestrator(
            wake_detector=None, turn_detector=None, stt=None, llm=FakeLLM(), speaker=None,
            persona_system_prompt="p", follow_up_window_s=1.0, file_roots=roots,
            memory_root=str(base / "memory2"),
            projects={"ewaste": ProjectConfig(name="ewaste", path=project_dir,
                                              command="python x.py")},
        )
        orchestrator._memory = ConversationMemory(provider=FakeLLM(), system_prompt="p")
        caps = {c.name: c for c in orchestrator._router.capabilities()}
        assert caps["shell"].permission is Permission.CONFIRM
        assert caps["shell_read"].permission is Permission.FREE
        assert caps["shell_blocked"].permission is Permission.FREE

        assert orchestrator._router.match("what's the git status of ewaste").intent == "shell_read"
        matched = orchestrator._router.match("run pip install requests in ewaste")
        assert matched.intent == "shell", matched
        assert matched.description == "Running pip install requests, in ewaste", matched.description
        assert orchestrator._router.match("run powershell in ewaste").intent == "shell_blocked"
        # Still a project run, not a command: no program named.
        assert orchestrator._router.match("run the ewaste").intent == "project_run"
        matched = orchestrator._router.match("run the tests in ewaste")
        assert matched is None or not matched.intent.startswith("shell"), matched
        print("OK  free to look, confirm to run, refusals never reach the gate")

        print("\nAll shell command checks passed.")
    finally:
        files.forget_index()
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())

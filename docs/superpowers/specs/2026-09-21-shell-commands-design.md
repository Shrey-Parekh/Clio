# 7.5 — Shell commands

Design, 2026-09-21. Built the same day.

## The problem

"Run pip install requests in ewaste." "What's the git status of clio." The
useful version of this is one sentence away from the dangerous version: a
microphone, a transcriber that mishears, and a program that will do whatever the
text says. Every earlier capability had a natural limit - a file index, a
registry, a mailbox. This one's natural limit is the whole machine.

So the design question was never how to run a command. It was what has to be
true for a sentence to become a process at all.

## The decision

**There is no shell.** A vetted command becomes an argument list handed
straight to the process with `shell=False`. `;`, `&&`, `|`, `>` and backticks
are therefore not dangerous characters that a filter removed - they are ordinary
characters with nothing there to interpret them. A filter over a shell is a
losing game, because shells have more syntax than any list remembers. Not having
one is not a game.

On top of that, decided with him:

1. **Only known tools can be the first word** - `git`, `pip`, `python`, `node`,
   `npm`, `cargo`, `go`, `make`, `dotnet`, `pytest`, `ruff`, `uv`, `poetry`,
   `nvidia-smi`. Anything else is refused *by name*, with the reason.
2. **Arguments are checked.** Letters, digits and `. _ - / \ : @ + =` only. No
   absolute paths, no `..`, nothing starting `http:`, `https:`, `git+`, `file:`.
3. **Read-only commands run free** - `git status/log/diff/show`, `pip
   list/show/freeze`, `npm list`, bare `git branch`, bare `nvidia-smi`. Kept
   exact, because `git branch` lists but `git branch -D x` deletes, and
   `nvidia-smi -r` resets the GPU.
4. **Everything else reads back the exact command and folder** and waits for a
   yes. Commands that destroy something say what: `git reset --hard`, `git
   clean`, `git push --force`, `pip uninstall`.
5. **Only in a registered project or inside the configured roots.**
6. **Short commands answer inline; anything past ten seconds becomes a job**, so
   a slow `npm install` gets status, logs, "stop it" and an announcement.

## What is refused, and why

| Refused | Why |
|---|---|
| `cmd`, `powershell`, `pwsh`, `bash`, `wsl`, `sh`, `start`, `runas` | each opens a second shell, which is the one thing this is built to avoid |
| `curl`, `wget`, `certutil`, `bitsadmin`, `npx` | downloads and runs things from the internet |
| `rm`, `del`, `erase`, `rd`, `rmdir`, `format` | deleting goes through 7.4 and the Recycle Bin |
| `reg`, `bcdedit`, `diskpart`, `netsh`, `sc`, `msiexec`, `shutdown`, ... | changes how Windows itself is set up |
| `schtasks` | reminders own their scheduled tasks |
| `python -c`, `node -e` | inline code is a script with no file to read first |
| an argument that is a URL | a package manager pointed at a link is `curl` in disguise |

A sentence is only claimed when its first word is one of these tools, allowed or
refused. "Run the tests in clio" is not a command - "the" is not a program - so
it carries on to conversation rather than being refused as one.

## Where the tool comes from

A project's own virtual environment wins, so `pip install requests in ewaste`
installs into ewaste.

**Python tools run as modules through the interpreter** - `python -m pip`, never
the `pip.exe` in the venv's Scripts folder. This was found live, not designed in:
the project was first called Jarvis, the venv was created there and the folder
renamed, and every launcher in `.venv\Scripts` still points at
`Documents\Jarvis\.venv\Scripts\python.exe`. They exit with code 1 and print
nothing. "Run pip list in clio" answered "didn't print anything". Going through
the interpreter works whether or not the launchers do.

## Testing

`tests/test_shell.py`, real processes in a temp folder. The central assertion
runs a script with the arguments `a&&echo` and `PWNED|more` and checks they
arrive as plain text, with no `PWNED` line - the design in one check, which
holds even if a filter above it ever has a gap. Around it: every refusal, the
free list kept exact, destructive warnings, folder fencing, inline output, a
failure explained in plain words, and a slow command handed to the job runner.

## Out of scope

Pipes and redirects (by design), multi-word arguments such as a commit message
with spaces, commands the model composes rather than him (7.7), and installing
applications (7.8, through `winget`).

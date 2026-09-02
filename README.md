# Clio

A personal voice assistant for Windows. Wake word or hotkey, natural speech in and out,
and a growing set of capabilities that do real work on this machine and my accounts.

- **Brief:** [PROJECT_BRIEF.md](PROJECT_BRIEF.md) - what it is and the rules it's built under.
- **Plan:** [ROADMAP.md](ROADMAP.md) - phases and tasks, in build order.

## Setup

```
py -3.11 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Fill in `.env`, then:

```
python -m clio
```

## Layout

| Path | Purpose |
|---|---|
| `clio/` | Core service - voice loop, LLM harness, capability registry |
| `ui/` | Desktop frontend - tray, overlay, chat window |
| `config/` | Persona, voice, and capability configuration |
| `docs/` | Design notes written as each phase lands |

## Git workflow

- `main` is the working branch. Each roadmap task lands as one atomic commit, in
  order, and gets pushed once it's verified - see [ROADMAP.md](ROADMAP.md) for
  what "done" means per task.
- Once a phase involves something riskier than the previous ones (a rewrite, a
  breaking config change, UI work that takes a few sittings), it gets a short-lived
  `phase/N-name` branch, merged back to `main` and deleted once it's done.
- Commit subjects are `Phase X.Y: what changed`, or a plain description for anything
  outside the phase numbering. Body explains what changed and, where it matters,
  why - not a restatement of the diff.
- No secrets, no generated models, no `logs/` or other runtime output ever committed
  - see `.gitignore`.

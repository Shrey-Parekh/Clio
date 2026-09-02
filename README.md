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

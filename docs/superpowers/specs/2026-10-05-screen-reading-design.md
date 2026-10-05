# Screen reading (8.1 + 8.2) - design

Approved by Shrey 2026-10-05.

## What it does

He asks about what he is looking at, and she answers out loud:

- "what's on my screen", "look at my screen", "what am I looking at"
- "read this to me", "what does this say"
- "what does this error mean", "explain this"

Adding "the whole screen" to any of these ("read the whole screen") captures the
full monitor instead of one window.

She takes one screenshot, shrinks it, sends it to Groq's image model with his
sentence as the question, and speaks the answer. She never captures on her own.

## Decisions (his)

- **8.1 and 8.2 together.** A screenshot alone does nothing useful.
- **The window in front, not the whole monitor.** Sharper after shrinking, cheaper,
  and other windows (chats, email) never leave the PC unless he says "whole screen".
- **FREE, no readback.** Asking is the permission. Only that one window is sent,
  and nothing is saved to disk.

## Pieces

### `clio/core/screen.py` - capture

- `front_window()` - the foreground window, via `ctypes` (`GetForegroundWindow`).
  If it belongs to Clio itself (process `clio.exe`, the chat window he may have
  typed into), walk down the Z-order (`GetWindow(GW_HWNDNEXT)`) to the first
  visible, non-minimised, non-Clio window with a size.
- `window_rect(hwnd)` - `DwmGetWindowAttribute(DWMWA_EXTENDED_FRAME_BOUNDS)`, which
  excludes the invisible resize border that `GetWindowRect` includes. The process
  is made per-monitor DPI aware so the rectangle is in real pixels.
- `capture(whole: bool) -> (bytes, str)` - Pillow `ImageGrab.grab(bbox=...,
  all_screens=True)`. Whole screen means the monitor that holds the front window
  (`MonitorFromWindow`). A minimised or zero-size window falls back to that
  monitor. Returns JPEG bytes and a label for the log ("Chrome", "whole screen").
- Shrink: longest side at most 1600px (`Image.thumbnail`, LANCZOS), JPEG quality
  85, in memory only. Nothing is written to disk.

Clio's HUD already excludes itself from capture with `SetWindowDisplayAffinity`.

### `clio/llm/vision.py` - the model call

- `look(config, jpeg: bytes, question: str) -> str` - one non-streaming Groq call
  to `config.llm.model_vision`, the image as a base64 `data:` URL plus a short
  instruction: answer out loud in a few plain sentences; when asked to read, read
  the main text, not the menus.
- Qwen models have put reasoning inline before (see the `[llm]` note on
  `qwen3.6-27b`), so any `<think>...</think>` is stripped before anything is
  spoken.
- Raises `VisionError` with a plain reason; the caller speaks it.
- No Ollama fallback: his Ollama has no model that can read images.

### `clio/capabilities/screen.py` - the voice side

- `parse_screen_request(text)` returns `{"whole": bool, "question": text}` or
  `None`. Fixed phrases only, so the fast path never calls a model to decide.
- Handler runs capture and `look` through `asyncio.to_thread`, speaks a short
  answer, and puts the full answer in the chat window.
- The answer joins conversation memory as text, so "so how do I fix it" carries
  on as ordinary conversation without a second picture.

### Wiring

- `config/default.toml` `[llm] model_vision = "qwen/qwen3.8-27b"`, with a
  `CLIO_LLM_MODEL_VISION` override.
- `registry.py`: `screen` registered with `offline=False`, before `files` and
  `open` (both of which might otherwise claim "read this" style sentences).
- `permissions.py`: `screen` FREE. `assistant.py` `_DESCRIPTIONS`: one line.
- `requirements.txt`: Pillow moves from tooling to a runtime dependency, with the
  reason (screen grab, shrink and JPEG in a few lines; by hand it is a BMP-to-PNG
  encoder and larger uploads).

## Failures, spoken

| What happened | What she says |
|---|---|
| No network, Groq down, timeout | "I can't see the screen right now - Groq's image model didn't answer." |
| Rate limited | "I've hit Groq's limit for pictures - try again in a minute." |
| Model gone (404 / bad request) | "Groq's image model isn't available any more - the model_vision setting needs changing." |
| Capture failed (locked screen, secure desktop) | "I couldn't take a picture of the screen." |

The Groq key is already checked at startup, so a missing key never reaches here.

## Tests - `tests/test_screen.py`

Fakes for the grab and the Groq call; no real screen, network or key.

- Every phrase above matches; "the whole screen" sets `whole`; near-misses
  ("lock the screen", "extend the screen", "read the report") do not.
- Clio's own window is skipped for the one behind it.
- A 4000x2000 image comes out at most 1600 wide, as JPEG.
- `<think>` is stripped.
- Each failure maps to its sentence; none raises.
- The registry order: "read this to me" reaches `screen`, not `files` or `open`.

## Live check

A real capture of a real window (a terminal showing a real error, then a web
page) sent to real Groq, the answer checked against what is on screen. Say
plainly if not tried by voice.

## Not in this task

- 8.3 using what is on screen to resolve vague requests.
- 8.4 a daily budget and counter.
- Reading image files (the parked item) - reuses `look`, a small follow-up.

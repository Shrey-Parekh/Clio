# Clio desktop shell (Tauri v2)

A window onto the running core. It connects to the core's WebSocket server
(`ws://127.0.0.1:8765`, matching `runtime.core_port` in `config/default.toml`)
and shows Clio's live events. The core only needs to be running — start it with
`python -m clio`.

## Run it

```
cd frontend
npm install
npm run dev        # tauri dev - builds the Rust shell and opens the window
```

First `npm run dev` compiles the Rust side (slow once, cached after) and needs
[Tauri's prerequisites](https://tauri.app/start/prerequisites/): the Rust
toolchain (installed) and WebView2 (ships with Windows 11).

## Build an installer

```
npm run build      # tauri build
```

## Layout

- `index.html` — the always-on HUD (the amber iris). `panel.html` — the chat /
  settings / memory window. Both are frameless, vanilla JS, no bundler.
- `lattice.js` — the `<clio-lattice>` canvas iris (dependency-free). `clio-bus.js`
  — the auto-reconnecting WebSocket adapter both pages share.
- `src-tauri/` — the Rust shell: `tauri.conf.json` (windows + CSP), `src/main.rs`
  (tray, windows, capture-excluded + always-on-top HUD, autostart), `capabilities/`
  (v2 permissions).

The placeholder icon in `src-tauri/icons/` is a solid colour; replace it with a
real icon set via `npm run tauri icon path/to/icon.png`.

## Scope

Phase 5 complete: the shell + core link (5.1), tray (5.2), capture-excluded HUD
(5.3), chat window (5.4), settings + memory (5.5). The link is bidirectional —
the pages send `say` / `mute` / `tts_speed` / `add_fact` and read back settings
and memory over the same socket.

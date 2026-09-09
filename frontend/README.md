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

- `index.html`, `src/` — the web layer (vanilla JS, no bundler). `src/main.js`
  holds the WebSocket client.
- `src-tauri/` — the Rust shell: `tauri.conf.json` (window + CSP), `src/main.rs`
  (opens the window), `capabilities/` (v2 permissions).

The placeholder icon in `src-tauri/icons/` is a solid colour; replace it with a
real icon set via `npm run tauri icon path/to/icon.png`.

## Scope

This is 5.1 - the shell and the core link. The tray (5.2), the capture-excluded
HUD (5.3), the chat window (5.4) and settings (5.5) build on it.

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

- `ui/` — everything the webview loads, and nothing else. It lives in its own
  folder because `frontendDist` used to point at `frontend/`, which swept in
  `node_modules` and the Rust target directory; Tauri refuses to bundle that, so
  `npm run build` had never once succeeded and only `tauri dev` was ever run.
- `ui/index.html` — the always-on HUD (the amber iris). `ui/panel.html` — the
  chat / settings / memory window. Both are frameless, vanilla JS, no bundler.
- `ui/lattice.js` — the `<clio-lattice>` canvas iris (dependency-free).
  `ui/clio-bus.js` — the auto-reconnecting WebSocket adapter both pages share.
- `ui/window.js` — moving, resizing, fullscreen and remembering where a window
  was. Frameless windows get none of that for free, and the drag region was
  written in Electron's `-webkit-app-region` spelling, which Tauri ignores.
- `src-tauri/` — the Rust shell: `tauri.conf.json` (windows + CSP), `src/main.rs`
  (tray, windows, capture-excluded + always-on-top HUD, autostart), `capabilities/`
  (v2 permissions — every window command the UI calls must be listed there).

## Startup

Only a **release** build registers itself to start with Windows. A debug build
loads its UI from the dev server `tauri dev` runs, and that address is compiled
in — so registering one for startup means every boot opens a window pointing at
a server nobody started ("can't reach this page", then a port number). A debug
build now clears the entry instead, which also repairs a machine that has the
bad path registered.

The placeholder icon in `src-tauri/icons/` is a solid colour; replace it with a
real icon set via `npm run tauri icon path/to/icon.png`.

## Scope

Phase 5 complete: the shell + core link (5.1), tray (5.2), capture-excluded HUD
(5.3), chat window (5.4), settings + memory (5.5). The link is bidirectional —
the pages send `say` / `mute` / `tts_speed` / `add_fact` and read back settings
and memory over the same socket.

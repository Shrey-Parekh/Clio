// Clio's desktop shell.
//
// The main window is the HUD: always on top, and content-protected so it stays
// out of screen shares and recordings (set_content_protected calls Windows'
// SetWindowDisplayAffinity). A tray icon shows/hides it, opens the chat &
// settings panel, toggles mute, and quits. Autostart registers Clio to launch
// with Windows. The webview talks to the Python core over WebSocket.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use tauri::{
    menu::{Menu, MenuItem, PredefinedMenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Emitter, Manager,
};
use tauri_plugin_autostart::MacosLauncher;
#[cfg(not(debug_assertions))]
use tauri_plugin_autostart::ManagerExt;

use std::fs::OpenOptions;
use std::io::Write;
use std::net::TcpStream;
use std::path::Path;
use std::process::{Child, Command};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

fn show_window(app: &AppHandle, label: &str) {
    if let Some(win) = app.get_webview_window(label) {
        let _ = win.show();
        let _ = win.set_focus();
    }
}

// The Python core - microphone, wake word, voice - is its own process. Without
// this, the window came up at login and she never heard the wake word, because
// nothing had started her. So the shell starts the core, unless one is already
// listening on 8765 (a core started by hand while developing).
//
// ponytail: the repo path is baked in at compile time (CARGO_MANIFEST_DIR), so
// the exe only works from this checkout. Packaging for another machine needs a
// configured path instead.
//
// Found live, 2026-09-25: after a reboot the window said "no core" and the log
// held nothing from that boot. The core had been started once, at the busiest
// moment of login, and whatever stopped it went to a pythonw that has no
// console - so it vanished. Now the shell watches the core and starts it again
// if it dies, and everything the core prints goes to logs/core-console.log, so
// the next failure leaves its reason behind.
fn repo() -> Option<&'static Path> {
    Path::new(env!("CARGO_MANIFEST_DIR")).parent()?.parent()
}

fn note(repo: &Path, line: &str) {
    let secs = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0);
    if let Ok(mut file) = OpenOptions::new().create(true).append(true)
        .open(repo.join("logs").join("core-console.log"))
    {
        let _ = writeln!(file, "[clio shell, unix {secs}] {line}");
    }
}

fn spawn_core(repo: &Path) -> std::io::Result<Child> {
    let _ = std::fs::create_dir_all(repo.join("logs"));
    let path = repo.join("logs").join("core-console.log");
    // The core echoes its whole log to the console, ~15 KB a minute, so this
    // file starts over past 5 MB. It only has to hold the last failure; the
    // real log is logs/clio.jsonl, which rotates.
    let too_big = std::fs::metadata(&path).map(|m| m.len() > 5_000_000).unwrap_or(false);
    let log = OpenOptions::new().create(true).write(true)
        .append(!too_big).truncate(too_big).open(&path)?;
    let mut command = Command::new(repo.join(".venv").join("Scripts").join("pythonw.exe"));
    command.args(["-m", "clio"]).current_dir(repo)
        .stdout(log.try_clone()?).stderr(log);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    command.spawn()
}

struct Core {
    child: Mutex<Option<Child>>,
    quitting: AtomicBool,
}

/// Keeps one core running for as long as the shell is. A core started by hand
/// (something already on 8765) is left alone. Five failures in a row, each
/// inside a minute, and it stops trying - a core that cannot start at all
/// should not be restarted forever.
fn supervise(core: Arc<Core>) {
    let Some(repo) = repo() else { return };
    let mut quick_failures: u64 = 0;
    loop {
        if core.quitting.load(Ordering::SeqCst) {
            return;
        }
        if TcpStream::connect("127.0.0.1:8765").is_ok() {
            thread::sleep(Duration::from_secs(5));
            continue;
        }
        match spawn_core(repo) {
            Ok(child) => {
                note(repo, &format!("started the core, pid {}", child.id()));
                *core.child.lock().unwrap() = Some(child);
            }
            Err(err) => {
                note(repo, &format!("could not start the core: {err}"));
                quick_failures += 1;
                thread::sleep(Duration::from_secs(5 * quick_failures));
                continue;
            }
        }
        let started = Instant::now();
        let status = loop {
            thread::sleep(Duration::from_secs(2));
            let mut guard = core.child.lock().unwrap();
            match guard.as_mut() {
                None => return, // taken by Quit
                Some(child) => {
                    if let Ok(Some(status)) = child.try_wait() {
                        *guard = None;
                        break status;
                    }
                }
            }
        };
        if core.quitting.load(Ordering::SeqCst) {
            return;
        }
        let lasted = started.elapsed();
        note(repo, &format!("the core exited ({status}) after {}s", lasted.as_secs()));
        quick_failures = if lasted > Duration::from_secs(60) { 0 } else { quick_failures + 1 };
        if quick_failures >= 5 {
            note(repo, "giving up after five quick failures in a row");
            return;
        }
        thread::sleep(Duration::from_secs(5 * quick_failures.max(1)));
    }
}

fn main() {
    let core = Arc::new(Core { child: Mutex::new(None), quitting: AtomicBool::new(false) });
    let watched = Arc::clone(&core);
    thread::spawn(move || supervise(watched));

    tauri::Builder::default()
        .plugin(tauri_plugin_autostart::init(MacosLauncher::LaunchAgent, None))
        .manage(core)
        .setup(|app| {
            if let Some(win) = app.get_webview_window("main") {
                let _ = win.set_always_on_top(true);
                // Keep the HUD off screen shares and recordings.
                let _ = win.set_content_protected(true);
            }
            // Launch with Windows - but only a real, built Clio.
            //
            // A debug build loads its UI from the dev server `tauri dev` runs,
            // and that address is compiled in. Registering *this* exe for
            // startup means that at every boot Windows opens a window pointing
            // at a server nobody started: "can't reach this page", followed by
            // a port number. That is exactly what was happening.
            //
            // So a debug build leaves the entry alone - it neither adds itself
            // nor removes the release build's. (Found live, 2026-09-21: a debug
            // exe built before this rule still re-registered itself, so the
            // boot window came back. Rebuilding debug replaces that exe.)
            #[cfg(not(debug_assertions))]
            let _ = app.autolaunch().enable();

            let show = MenuItem::with_id(app, "show", "Show Clio", true, None::<&str>)?;
            let panel = MenuItem::with_id(app, "panel", "Chat & settings", true, None::<&str>)?;
            let mute = MenuItem::with_id(app, "mute", "Mute / unmute", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "Quit Clio", true, None::<&str>)?;
            let menu = Menu::with_items(
                app,
                &[
                    &show,
                    &panel,
                    &PredefinedMenuItem::separator(app)?,
                    &mute,
                    &PredefinedMenuItem::separator(app)?,
                    &quit,
                ],
            )?;

            TrayIconBuilder::with_id("clio-tray")
                .icon(app.default_window_icon().unwrap().clone())
                .tooltip("Clio")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| match event.id().as_ref() {
                    "show" => show_window(app, "main"),
                    "panel" => show_window(app, "panel"),
                    "mute" => {
                        // The core owns the mute state; the webview relays it.
                        let _ = app.emit("tray-mute", ());
                    }
                    "quit" => {
                        // Quitting Clio quits her - a core left behind would
                        // keep the microphone open with no window to show it.
                        // The venv's pythonw.exe is a launcher that starts the
                        // real interpreter as a child, so kill the whole tree.
                        let core = app.state::<Arc<Core>>();
                        core.quitting.store(true, Ordering::SeqCst);
                        if let Some(child) = core.child.lock().unwrap().take() {
                            let mut kill = Command::new("taskkill");
                            kill.args(["/PID", &child.id().to_string(), "/T", "/F"]);
                            #[cfg(windows)]
                            {
                                use std::os::windows::process::CommandExt;
                                kill.creation_flags(0x0800_0000);
                            }
                            let _ = kill.status();
                        }
                        app.exit(0)
                    }
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        show_window(tray.app_handle(), "main");
                    }
                })
                .build(app)?;

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running Clio");
}

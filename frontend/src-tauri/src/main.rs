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

fn show_window(app: &AppHandle, label: &str) {
    if let Some(win) = app.get_webview_window(label) {
        let _ = win.show();
        let _ = win.set_focus();
    }
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_autostart::init(MacosLauncher::LaunchAgent, None))
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
                    "quit" => app.exit(0),
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

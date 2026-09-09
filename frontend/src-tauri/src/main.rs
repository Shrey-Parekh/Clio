// Clio's desktop shell. The window loads the web layer, which talks to the core
// over WebSocket; Rust only opens the window for now. The tray, the HUD's
// capture exclusion and the rest arrive in 5.2 onward.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    tauri::Builder::default()
        .run(tauri::generate_context!())
        .expect("error while running Clio");
}

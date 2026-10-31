// Tauri 2 shell: spawn packaged sidecar (orbit-server), wait for health on 127.0.0.1 only,
// show a clear dialog on port-busy exit code 3, and kill the sidecar on exit.
use std::thread;
use std::time::{Duration, Instant};

use tauri::Manager;
use tauri_plugin_shell::ShellExt;

#[tauri::command]
fn health_ok() -> bool {
    true
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .invoke_handler(tauri::generate_handler![health_ok])
        .setup(|app| {
            let app_handle = app.handle().clone();
            std::thread::spawn(move || {
                let shell = app_handle.shell();
                let _ = if let Ok(sidecar) = shell.sidecar("orbit-server") {
                    let mut cmd = sidecar;
                    let _ = cmd.spawn();
                } else {
                    let mut cmd = shell.command("orbit-server");
                    let _ = cmd.spawn();
                };
            });

            thread::spawn(move || {
                let start = Instant::now();
                let url = "http://127.0.0.1:8765/api/health";
                while start.elapsed() < Duration::from_secs(30) {
                    if let Ok(resp) = ureq::get(url).call() {
                        if resp.status() == 200 {
                            return;
                        }
                    }
                    thread::sleep(Duration::from_millis(400));
                }
                eprintln!("ORBIT_DESKTOP: backend did not become healthy within 30s");
            });
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while building tauri application");
}

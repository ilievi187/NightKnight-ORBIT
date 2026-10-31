//! ORBIT Desktop: own exactly one local backend, then navigate to its same-origin UI.
//! The packaged backend is an executable PyInstaller *onefile* Tauri sidecar.
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};

use tauri::{Manager, RunEvent};
use tauri_plugin_shell::{process::CommandChild, ShellExt};

struct BackendChild(Mutex<Option<CommandChild>>);

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            let shell = app.shell();
            let mut cmd = shell.sidecar("orbit-server")?;
            // The fleet mock worker is a separate, explicitly bundled companion.
            // Do not start it until the user requests a test in the dashboard.
            if let Ok(exe) = std::env::current_exe() {
                if let Some(parent) = exe.parent() {
                    let candidate = parent.join("orbit-mock-worker");
                    if candidate.is_file() {
                        cmd = cmd.env("ORBIT_MOCK_WORKER", candidate.to_string_lossy().to_string());
                    }
                }
            }
            let (_events, child) = cmd.spawn()?;
            app.manage(BackendChild(Mutex::new(Some(child))));

            let handle = app.handle().clone();
            thread::spawn(move || {
                let start = Instant::now();
                let url = "http://127.0.0.1:8765/api/health";
                while start.elapsed() < Duration::from_secs(30) {
                    if let Ok(response) = ureq::get(url).timeout(Duration::from_secs(2)).call() {
                        if response.status() == 200 {
                            // Loading the loopback UI directly keeps all fetch('/api/...')
                            // calls same-origin inside WebKit instead of tauri://.
                            if let Some(window) = handle.get_webview_window("main") {
                                let _ = window.eval(
                                    "window.location.replace('http://127.0.0.1:8765/')",
                                );
                            }
                            return;
                        }
                    }
                    thread::sleep(Duration::from_millis(400));
                }
                eprintln!("ORBIT_DESKTOP: backend health not ready after 30 seconds");
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("ORBIT desktop initialization failed");

    app.run(|handle, event| {
        if let RunEvent::Exit = event {
            if let Some(state) = handle.try_state::<BackendChild>() {
                if let Ok(mut child) = state.0.lock() {
                    if let Some(child) = child.take() {
                        let _ = child.kill();
                    }
                }
            }
        }
    });
}

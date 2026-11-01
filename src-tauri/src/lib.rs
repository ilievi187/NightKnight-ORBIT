//! Native AppImage bootstrap: show a real status screen until an owned backend is healthy.
//! Do not load bundled web/index.html: its /assets/* paths are served by FastAPI, not tauri://.
use std::net::{SocketAddr, TcpStream};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};

use tauri::{Manager, RunEvent};
use tauri_plugin_shell::{process::{CommandChild, CommandEvent}, ShellExt};

struct BackendChild(Mutex<Option<CommandChild>>);

fn show_failure(handle: &tauri::AppHandle, message: &str) {
    eprintln!("ORBIT_DESKTOP_ERROR: {message}");
    if let Some(window) = handle.get_webview_window("main") {
        if let Ok(encoded) = serde_json::to_string(message) {
            let _ = window.eval(format!(
                "var s=document.getElementById('boot-status');if(s){{s.textContent={encoded};s.style.color='#ff9da9'}};"
            ));
        }
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            let handle = app.handle().clone();
            let port: u16 = std::env::var("ORBIT_PORT").ok()
                .and_then(|x| x.parse().ok()).unwrap_or(8765);
            let addr: SocketAddr = format!("127.0.0.1:{port}").parse()?;
            // Never silently connect our window to an unrelated/older process.
            if TcpStream::connect_timeout(&addr, Duration::from_millis(200)).is_ok() {
                show_failure(&handle, &format!(
                    "Port {port} is already in use. Close only the old ORBIT backend and relaunch. Heretic/Hermes can stay running."
                ));
                return Ok(());
            }

            let shell = app.shell();
            let mut cmd = match shell.sidecar("orbit-server") {
                Ok(cmd) => cmd,
                Err(e) => {
                    show_failure(&handle, &format!("Backend sidecar is missing from AppImage: {e}"));
                    return Ok(());
                }
            };
            // No model/GPU process gets spawned by ORBIT; only our own local API backend.
            if let Ok(exe) = std::env::current_exe() {
                if let Some(parent) = exe.parent() {
                    let companion = parent.join("orbit-mock-worker");
                    if companion.is_file() {
                        cmd = cmd.env("ORBIT_MOCK_WORKER", companion.to_string_lossy().to_string());
                    }
                }
            }

            let (mut events, child) = match cmd.spawn() {
                Ok(pair) => pair,
                Err(e) => {
                    show_failure(&handle, &format!("Could not start packaged ORBIT backend: {e}"));
                    return Ok(());
                }
            };
            app.manage(BackendChild(Mutex::new(Some(child))));

            // Listen to the child process stderr! Earlier versions dropped this receiver,
            // hiding import/permission/boot errors and leaving a misleading white screen.
            let diagnostic_handle = handle.clone();
            thread::spawn(move || {
                while let Some(event) = events.blocking_recv() {
                    match event {
                        CommandEvent::Stderr(bytes) => {
                            eprintln!("ORBIT_BACKEND_STDERR: {}", String::from_utf8_lossy(&bytes));
                        }
                        CommandEvent::Stdout(bytes) => {
                            eprintln!("ORBIT_BACKEND_STDOUT: {}", String::from_utf8_lossy(&bytes));
                        }
                        CommandEvent::Error(error) => {
                            show_failure(&diagnostic_handle, &format!("Backend I/O error: {error}"));
                        }
                        CommandEvent::Terminated(payload) => {
                            show_failure(&diagnostic_handle,
                                &format!("ORBIT backend exited (code {:?}). Check terminal ORBIT_BACKEND_STDERR.", payload.code));
                            break;
                        }
                        _ => {}
                    }
                }
            });

            let ready_handle = handle.clone();
            thread::spawn(move || {
                let url = format!("http://127.0.0.1:{port}/api/health");
                let target = format!("http://127.0.0.1:{port}/");
                let start = Instant::now();
                while start.elapsed() < Duration::from_secs(45) {
                    if let Ok(response) = ureq::get(&url).timeout(Duration::from_secs(2)).call() {
                        if response.status() == 200 {
                            if let Some(window) = ready_handle.get_webview_window("main") {
                                if let Ok(parsed) = tauri::Url::parse(&target) {
                                    if let Err(e) = window.navigate(parsed) {
                                        show_failure(&ready_handle, &format!("Backend ready but navigation failed: {e}"));
                                    }
                                }
                            }
                            return;
                        }
                    }
                    thread::sleep(Duration::from_millis(350));
                }
                show_failure(&ready_handle,
                    "Backend did not become healthy. Check ORBIT_BACKEND_STDERR in the terminal; do not restart Heretic.");
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

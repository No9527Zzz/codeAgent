#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};
use tauri::Manager;

const SERVICE_HOST: &str = "127.0.0.1";
const SERVICE_PORT: u16 = 8765;

struct ServiceProcess(Mutex<Option<Child>>);

fn project_root() -> PathBuf {
    if let Ok(path) = std::env::var("CODEAGENT_ROOT") {
        return PathBuf::from(path);
    }
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).parent().expect("missing project root").to_path_buf()
}

fn service_is_healthy() -> bool {
    let address = (SERVICE_HOST, SERVICE_PORT).to_socket_addrs().ok().and_then(|mut addresses| addresses.next());
    let Some(address) = address else { return false };
    let Ok(mut stream) = TcpStream::connect_timeout(&address, Duration::from_millis(300)) else { return false };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(500)));
    let _ = stream.write_all(b"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n");
    let mut response = String::new();
    stream.read_to_string(&mut response).is_ok() && response.contains("\"ok\": true")
}

fn start_service() -> Result<Option<Child>, String> {
    if service_is_healthy() { return Ok(None); }
    let root = project_root();
    let script = root.join("server").join("main.py");
    if !script.exists() { return Err(format!("找不到本地 Agent 服务：{}", script.display())); }
    let python = std::env::var("CODEAGENT_PYTHON").unwrap_or_else(|_| "python".to_string());
    let child = Command::new(python).arg(&script).current_dir(&root).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).spawn().map_err(|error| format!("启动本地 Agent 服务失败：{error}"))?;
    let deadline = Instant::now() + Duration::from_secs(10);
    while Instant::now() < deadline {
        if service_is_healthy() { return Ok(Some(child)); }
        std::thread::sleep(Duration::from_millis(250));
    }
    Err("本地 Agent 服务启动超时，请检查 Python 环境和服务日志".to_string())
}

fn stop_service(state: &ServiceProcess) {
    if let Ok(mut process) = state.0.lock() {
        if let Some(mut child) = process.take() { let _ = child.kill(); let _ = child.wait(); }
    }
}

#[tauri::command]
fn app_version() -> &'static str { "0.1.0" }

#[tauri::command]
fn service_status() -> bool { service_is_healthy() }

pub fn run() {
    tauri::Builder::default()
        .manage(ServiceProcess(Mutex::new(None)))
        .setup(|app| {
            let state = app.state::<ServiceProcess>();
            match start_service() {
                Ok(child) => { if let Ok(mut process) = state.0.lock() { *process = child; } Ok(()) }
                Err(error) => { eprintln!("{error}"); Err(error.into()) }
            }
        })
        .invoke_handler(tauri::generate_handler![app_version, service_status])
        .build(tauri::generate_context!())
        .expect("error while building CodeAgent")
        .run(|app, event| {
            if let tauri::RunEvent::ExitRequested { .. } = event { stop_service(&app.state::<ServiceProcess>()); }
        });
}

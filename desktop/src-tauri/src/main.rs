use serde::Deserialize;
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};
use tauri::{Manager, RunEvent, WebviewUrl, WebviewWindow, WebviewWindowBuilder, WindowEvent};
use uuid::Uuid;

const MAX_AUTOMATIC_RESTARTS: usize = 1;
const READY_TIMEOUT: Duration = Duration::from_secs(45);

#[derive(Debug, Deserialize)]
struct RuntimeManifest {
    schema_version: u32,
    fixture: bool,
    target: String,
    python: PythonManifest,
    app: AppManifest,
    patchright: PatchrightManifest,
}

#[derive(Debug, Deserialize)]
struct PythonManifest {
    interpreter: String,
}

#[derive(Debug, Deserialize)]
struct AppManifest {
    directory: String,
}

#[derive(Debug, Deserialize)]
struct PatchrightManifest {
    browser_executable: String,
}

struct BackendState {
    child: Mutex<Option<Child>>,
    stopping: AtomicBool,
    retry_requested: AtomicBool,
    port: u16,
    token: String,
    instance_id: String,
    data_dir: PathBuf,
    log_dir: PathBuf,
    runtime_dir: PathBuf,
}

impl BackendState {
    fn origin(&self) -> String {
        format!("http://127.0.0.1:{}", self.port)
    }
}

fn choose_port() -> Result<u16, String> {
    if let Ok(raw) = std::env::var("DOLA_DESKTOP_API_PORT") {
        let port = raw
            .parse::<u16>()
            .map_err(|_| "DOLA_DESKTOP_API_PORT must be a valid TCP port".to_string())?;
        if port == 0 {
            return Err("DOLA_DESKTOP_API_PORT cannot be zero".into());
        }
        return Ok(port);
    }
    let listener = TcpListener::bind(("127.0.0.1", 0)).map_err(|error| error.to_string())?;
    listener
        .local_addr()
        .map(|address| address.port())
        .map_err(|error| error.to_string())
}

fn make_private_dir(path: &Path) -> Result<(), String> {
    fs::create_dir_all(path).map_err(|error| format!("{}: {error}", path.display()))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o700))
            .map_err(|error| format!("{}: {error}", path.display()))?;
    }
    Ok(())
}

fn contained_path(root: &Path, relative: &str) -> Result<PathBuf, String> {
    let candidate = root.join(relative);
    let root = root
        .canonicalize()
        .map_err(|error| format!("runtime directory: {error}"))?;
    let candidate = candidate
        .canonicalize()
        .map_err(|error| format!("runtime component {}: {error}", candidate.display()))?;
    if !candidate.starts_with(&root) {
        return Err(format!("runtime path escapes bundle: {relative}"));
    }
    Ok(candidate)
}

fn load_manifest(runtime_dir: &Path) -> Result<RuntimeManifest, String> {
    let body = fs::read_to_string(runtime_dir.join("runtime-manifest.json"))
        .map_err(|error| format!("runtime manifest: {error}"))?;
    let manifest: RuntimeManifest =
        serde_json::from_str(&body).map_err(|error| format!("runtime manifest: {error}"))?;
    if manifest.schema_version != 1 || manifest.fixture {
        return Err("bundled runtime is incomplete; rebuild the desktop package".into());
    }
    let expected = if cfg!(target_os = "macos") {
        "macos-arm64"
    } else if cfg!(target_os = "windows") {
        "windows-x64"
    } else {
        "unsupported"
    };
    if manifest.target != expected {
        return Err(format!(
            "runtime target {} does not match {expected}",
            manifest.target
        ));
    }
    Ok(manifest)
}

fn log_file(path: &Path) -> Result<File, String> {
    let file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .map_err(|error| format!("{}: {error}", path.display()))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        file.set_permissions(fs::Permissions::from_mode(0o600))
            .map_err(|error| error.to_string())?;
    }
    Ok(file)
}

fn spawn_backend(state: &BackendState) -> Result<Child, String> {
    let manifest = load_manifest(&state.runtime_dir)?;
    let python = contained_path(&state.runtime_dir, &manifest.python.interpreter)?;
    let app_dir = contained_path(&state.runtime_dir, &manifest.app.directory)?;
    let browser = contained_path(&state.runtime_dir, &manifest.patchright.browser_executable)?;
    if !python.is_file() || !browser.is_file() || !app_dir.is_dir() {
        return Err("bundled runtime is missing Python, application, or Chromium".into());
    }
    let stdout = log_file(&state.log_dir.join("backend.log"))?;
    let stderr = stdout.try_clone().map_err(|error| error.to_string())?;
    let mut command = Command::new(python);
    command
        .current_dir(&app_dir)
        .arg("-m")
        .arg("dola_gateway.desktop_entry")
        .env("PYTHONPATH", &app_dir)
        .env("PYTHONUTF8", "1")
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .env("DOLA_HOST", "127.0.0.1")
        .env("DOLA_PORT", state.port.to_string())
        .env("DOLA_PUBLIC_BASE", state.origin())
        .env("DOLA_STATE_DIR", &state.data_dir)
        .env("DOLA_LOG_DIR", &state.log_dir)
        .env("DOLA_BROWSER_EXECUTABLE", browser)
        .env("DOLA_DESKTOP_TOKEN", &state.token)
        .env("DOLA_INSTANCE_ID", &state.instance_id)
        .env("DOLA_PARENT_PID", std::process::id().to_string())
        .env(
            "DOLA_LOCAL_API_ENABLED",
            if std::env::var_os("DOLA_DESKTOP_API_PORT").is_some() {
                "1"
            } else {
                "0"
            },
        )
        .stdin(Stdio::null())
        .stdout(Stdio::from(stdout))
        .stderr(Stdio::from(stderr));
    #[cfg(unix)]
    unsafe {
        use std::os::unix::process::CommandExt;
        command.pre_exec(|| {
            if libc::setpgid(0, 0) == 0 {
                Ok(())
            } else {
                Err(std::io::Error::last_os_error())
            }
        });
    }
    command.spawn().map_err(|error| format!("start backend: {error}"))
}

fn http_request(port: u16, request: &str, timeout: Duration) -> Result<String, String> {
    let address = format!("127.0.0.1:{port}");
    let mut stream = TcpStream::connect_timeout(
        &address.parse().map_err(|_| "invalid loopback address")?,
        timeout,
    )
    .map_err(|error| error.to_string())?;
    stream.set_read_timeout(Some(timeout)).ok();
    stream.set_write_timeout(Some(timeout)).ok();
    stream
        .write_all(request.as_bytes())
        .map_err(|error| error.to_string())?;
    let mut response = String::new();
    stream
        .read_to_string(&mut response)
        .map_err(|error| error.to_string())?;
    Ok(response)
}

fn readiness_matches(state: &BackendState) -> bool {
    let request = format!(
        "GET /health/ready HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nConnection: close\r\n\r\n",
        state.port
    );
    let response = match http_request(state.port, &request, Duration::from_secs(1)) {
        Ok(value) => value,
        Err(_) => return false,
    };
    if !response.starts_with("HTTP/1.1 200") {
        return false;
    }
    let body = response.split("\r\n\r\n").nth(1).unwrap_or("");
    serde_json::from_str::<serde_json::Value>(body)
        .ok()
        .and_then(|value| value.get("instance_id").and_then(|item| item.as_str()).map(str::to_owned))
        .is_some_and(|instance| instance == state.instance_id)
}

fn wait_until_ready(state: &BackendState, timeout: Duration) -> Result<(), String> {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if state.stopping.load(Ordering::SeqCst) {
            return Err("application is closing".into());
        }
        {
            let mut guard = state.child.lock().map_err(|_| "backend lock poisoned")?;
            if let Some(child) = guard.as_mut() {
                if let Some(status) = child.try_wait().map_err(|error| error.to_string())? {
                    return Err(format!("backend exited during startup ({status})"));
                }
            }
        }
        if readiness_matches(state) {
            return Ok(());
        }
        thread::sleep(Duration::from_millis(200));
    }
    Err("backend did not become ready in 45 seconds".into())
}

fn backend_url(state: &BackendState) -> String {
    format!("{}/#desktop_token={}", state.origin(), state.token)
}

fn show_startup(window: &WebviewWindow, startup_url: &tauri::Url, state: &str, detail: &str) {
    let mut url = startup_url.clone();
    url.set_query(Some(&format!(
        "state={}&detail={}",
        state,
        percent_encode(detail)
    )));
    let _ = window.navigate(url);
}

fn percent_encode(value: &str) -> String {
    value
        .bytes()
        .flat_map(|byte| {
            if byte.is_ascii_alphanumeric() || b"-_.~".contains(&byte) {
                format!("{}", byte as char).into_bytes()
            } else {
                format!("%{byte:02X}").into_bytes()
            }
        })
        .map(char::from)
        .collect()
}

fn supervise_backend(state: Arc<BackendState>, window: WebviewWindow, startup_url: tauri::Url) {
    let mut automatic_restarts = 0usize;
    loop {
        state.retry_requested.store(false, Ordering::SeqCst);
        show_startup(
            &window,
            &startup_url,
            if automatic_restarts == 0 { "starting" } else { "restarting" },
            "Starting the private local service…",
        );
        match spawn_backend(&state) {
            Ok(child) => {
                *state.child.lock().expect("backend lock") = Some(child);
            }
            Err(error) => {
                show_startup(&window, &startup_url, "failed", &error);
                wait_for_retry(&state);
                continue;
            }
        }
        match wait_until_ready(&state, READY_TIMEOUT) {
            Ok(()) => {
                if let Ok(url) = backend_url(&state).parse() {
                    let _ = window.navigate(url);
                }
            }
            Err(error) => {
                stop_child(&state);
                show_startup(&window, &startup_url, "failed", &error);
                wait_for_retry(&state);
                continue;
            }
        }

        loop {
            if state.stopping.load(Ordering::SeqCst) {
                return;
            }
            let exited = {
                let mut guard = state.child.lock().expect("backend lock");
                guard
                    .as_mut()
                    .and_then(|child| child.try_wait().ok().flatten())
            };
            if let Some(status) = exited {
                *state.child.lock().expect("backend lock") = None;
                if automatic_restarts < MAX_AUTOMATIC_RESTARTS {
                    automatic_restarts += 1;
                    show_startup(
                        &window,
                        &startup_url,
                        "restarting",
                        &format!("The local service stopped ({status}). Restarting once…"),
                    );
                    thread::sleep(Duration::from_millis(600));
                    break;
                }
                show_startup(
                    &window,
                    &startup_url,
                    "failed",
                    &format!("The local service stopped ({status}). You can retry safely."),
                );
                wait_for_retry(&state);
                automatic_restarts = 0;
                break;
            }
            if state.retry_requested.swap(false, Ordering::SeqCst) {
                stop_child(&state);
                automatic_restarts = 0;
                break;
            }
            thread::sleep(Duration::from_millis(300));
        }
    }
}

fn wait_for_retry(state: &BackendState) {
    while !state.stopping.load(Ordering::SeqCst) {
        if state.retry_requested.swap(false, Ordering::SeqCst) {
            return;
        }
        thread::sleep(Duration::from_millis(200));
    }
}

fn request_graceful_shutdown(state: &BackendState) {
    let request = format!(
        "POST /api/admin/shutdown HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nX-Admin-Key: {}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
        state.port, state.token
    );
    let _ = http_request(state.port, &request, Duration::from_secs(1));
}

fn stop_child(state: &BackendState) {
    let mut guard = match state.child.lock() {
        Ok(value) => value,
        Err(_) => return,
    };
    let Some(child) = guard.as_mut() else { return };
    let deadline = Instant::now() + Duration::from_secs(4);
    while Instant::now() < deadline {
        if child.try_wait().ok().flatten().is_some() {
            *guard = None;
            return;
        }
        thread::sleep(Duration::from_millis(100));
    }
    #[cfg(unix)]
    unsafe {
        libc::kill(-(child.id() as i32), libc::SIGTERM);
    }
    thread::sleep(Duration::from_millis(400));
    if child.try_wait().ok().flatten().is_none() {
        #[cfg(unix)]
        unsafe {
            libc::kill(-(child.id() as i32), libc::SIGKILL);
        }
        #[cfg(windows)]
        {
            // taskkill's /T flag cleans up Chromium descendants if graceful
            // shutdown could not finish the process tree in time.
            let _ = Command::new("taskkill")
                .args(["/PID", &child.id().to_string(), "/T", "/F"])
                .status();
        }
        #[cfg(not(any(unix, windows)))]
        {
            let _ = child.kill();
        }
    }
    let _ = child.wait();
    *guard = None;
}

fn create_recovery_report(state: &BackendState) -> Result<PathBuf, String> {
    let support_dir = state.data_dir.join("support");
    make_private_dir(&support_dir)?;
    let destination = support_dir.join(format!("desktop-recovery-{}.txt", Uuid::new_v4()));
    let body = format!(
        "Dola Gateway desktop recovery report\ninstance={}\nport={}\nplatform={}\narchitecture={}\nruntime_available={}\nbackend_log_available={}\n\nThis report contains lifecycle metadata only. The in-app support bundle additionally includes scrubbed log tails.\n",
        state.instance_id,
        state.port,
        std::env::consts::OS,
        std::env::consts::ARCH,
        state.runtime_dir.join("runtime-manifest.json").is_file(),
        state.log_dir.join("backend.log").is_file(),
    );
    fs::write(&destination, body).map_err(|error| error.to_string())?;
    Ok(destination)
}

fn build_window(app: &tauri::App, state: Arc<BackendState>) -> tauri::Result<WebviewWindow> {
    let navigation_state = Arc::clone(&state);
    WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
        .title("Dola Gateway")
        .inner_size(1180.0, 780.0)
        .min_inner_size(760.0, 560.0)
        .center()
        .on_navigation(move |url| {
            if url.scheme() == "dola" {
                match url.host_str().unwrap_or_default() {
                    "retry" => navigation_state.retry_requested.store(true, Ordering::SeqCst),
                    "support" => {
                        if let Ok(report) = create_recovery_report(&navigation_state) {
                            let _ = open::that_detached(report.parent().unwrap_or(&report));
                        }
                    }
                    _ => {}
                }
                return false;
            }
            if url.scheme() == "http"
                && matches!(url.host_str(), Some("127.0.0.1"))
                && url.port() == Some(navigation_state.port)
            {
                return true;
            }
            if matches!(url.scheme(), "http" | "https") {
                let _ = open::that_detached(url.as_str());
                return false;
            }
            (url.scheme() == "tauri" && url.host_str() == Some("localhost"))
                || (url.scheme() == "http" && url.host_str() == Some("tauri.localhost"))
        })
        .build()
}

fn main() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.unminimize();
                let _ = window.set_focus();
            }
        }))
        .setup(|app| {
            let data_dir = app.path().app_data_dir()?;
            let log_dir = app.path().app_log_dir()?;
            make_private_dir(&data_dir).map_err(std::io::Error::other)?;
            make_private_dir(&log_dir).map_err(std::io::Error::other)?;
            let runtime_dir = if let Some(value) = std::env::var_os("DOLA_DESKTOP_RUNTIME_DIR") {
                PathBuf::from(value)
            } else {
                app.path().resource_dir()?.join("runtime")
            };
            let state = Arc::new(BackendState {
                child: Mutex::new(None),
                stopping: AtomicBool::new(false),
                retry_requested: AtomicBool::new(false),
                port: choose_port().map_err(std::io::Error::other)?,
                token: Uuid::new_v4().to_string() + &Uuid::new_v4().simple().to_string(),
                instance_id: Uuid::new_v4().to_string(),
                data_dir,
                log_dir,
                runtime_dir,
            });
            let window = build_window(app, Arc::clone(&state))?;
            let startup_url = window.url()?;
            let close_state = Arc::clone(&state);
            let close_app = app.handle().clone();
            window.on_window_event(move |event| {
                if let WindowEvent::CloseRequested { api, .. } = event {
                    api.prevent_close();
                    if close_state.stopping.swap(true, Ordering::SeqCst) {
                        return;
                    }
                    let state = Arc::clone(&close_state);
                    let app = close_app.clone();
                    thread::spawn(move || {
                        request_graceful_shutdown(&state);
                        stop_child(&state);
                        app.exit(0);
                    });
                }
            });
            app.manage(Arc::clone(&state));
            thread::spawn(move || supervise_backend(state, window, startup_url));
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to build Dola Gateway desktop application");

    app.run(|handle, event| {
        if let RunEvent::Exit = event {
            if let Some(state) = handle.try_state::<Arc<BackendState>>() {
                state.stopping.store(true, Ordering::SeqCst);
                stop_child(&state);
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn percent_encoding_does_not_leak_control_characters() {
        assert_eq!(percent_encode("failed: a/b? c"), "failed%3A%20a%2Fb%3F%20c");
    }

    #[test]
    fn fixed_port_rejects_zero() {
        std::env::set_var("DOLA_DESKTOP_API_PORT", "0");
        assert!(choose_port().is_err());
        std::env::remove_var("DOLA_DESKTOP_API_PORT");
    }
}

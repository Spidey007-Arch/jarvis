//! Starts, watches and restarts the Python backend and the desktop agent.
//!
//! Both run as hidden child processes. The shell never trusts a process
//! just because it spawned it: "online" is only reported after the backend's
//! /health endpoint answers, and the agent only counts as connected when the
//! backend itself says it is receiving the agent's heartbeats.

use serde::{Deserialize, Serialize};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};
use tauri::{AppHandle, Emitter, Manager};

#[cfg(windows)]
use std::os::windows::process::CommandExt;

const PORT_FIRST: u16 = 8765;
const PORT_LAST: u16 = 8775;
const MAX_RESTARTS: usize = 5;
const RESTART_WINDOW: Duration = Duration::from_secs(120);
const BACKEND_READY_TIMEOUT: Duration = Duration::from_secs(45);
const AGENT_CONNECT_TIMEOUT: Duration = Duration::from_secs(30);
/// A backend whose process is still alive but slow to answer (e.g. the PC is under heavy load)
/// is busy, not dead: only restart it after this many consecutive unanswered checks (~1 minute).
const HEALTH_FAILURES_BEFORE_RESTART: u32 = 15;
const HEALTH_TIMEOUT: Duration = Duration::from_secs(3);
/// A visible window that hasn't checked in with the backend for this long is frozen or blank: reload it.
const UI_SILENT_RELOAD_S: f64 = 45.0;
const UI_RELOADS_PER_MIN: usize = 3;

#[derive(Clone, Serialize, Deserialize)]
pub struct ConnInfo {
    pub port: u16,
    pub token: String,
    pub pid: u32,
}

#[derive(Clone, Serialize, PartialEq)]
pub struct Status {
    /// starting | online | degraded | reconnecting | failed
    pub overall: String,
    /// starting | online | offline
    pub backend: String,
    /// waiting | starting | online | failed
    pub agent: String,
    pub port: u16,
    pub detail: String,
    pub backend_restarts: u32,
    pub agent_restarts: u32,
    pub attached_to_existing: bool,
    pub failed: bool,
    pub ever_online: bool,
}

struct Health {
    pid: u32,
    agent_connected: bool,
    ui_age_s: Option<f64>,
}

pub struct Supervisor {
    status: Mutex<Status>,
    info: Mutex<Option<ConnInfo>>,
    backend_child: Mutex<Option<Child>>,
    agent_child: Mutex<Option<Child>>,
    owns_backend: AtomicBool,
    shutdown_flag: AtomicBool,
    restart_requested: AtomicBool,
    data_dir: PathBuf,
}

impl Supervisor {
    pub fn new() -> Self {
        let base = std::env::var("LOCALAPPDATA")
            .map(PathBuf::from)
            .unwrap_or_else(|_| std::env::temp_dir());
        let data_dir = base.join("JARVIS");
        let _ = fs::create_dir_all(data_dir.join("logs"));
        Supervisor {
            status: Mutex::new(Status {
                overall: "starting".into(),
                backend: "starting".into(),
                agent: "waiting".into(),
                port: 0,
                detail: String::new(),
                backend_restarts: 0,
                agent_restarts: 0,
                attached_to_existing: false,
                failed: false,
                ever_online: false,
            }),
            info: Mutex::new(None),
            backend_child: Mutex::new(None),
            agent_child: Mutex::new(None),
            owns_backend: AtomicBool::new(false),
            shutdown_flag: AtomicBool::new(false),
            restart_requested: AtomicBool::new(false),
            data_dir,
        }
    }

    pub fn status(&self) -> Status {
        self.status.lock().unwrap().clone()
    }

    pub fn connection(&self) -> Option<ConnInfo> {
        self.info.lock().unwrap().clone()
    }

    pub fn request_restart(&self) {
        self.restart_requested.store(true, Ordering::SeqCst);
    }

    // ---------- logging ----------

    fn log(&self, file: &str, msg: &str) {
        let line = format!("{} {}\n", timestamp(), msg);
        let dir = self.data_dir.join("logs");
        if let Ok(mut f) = OpenOptions::new().create(true).append(true).open(dir.join(format!("{file}.log"))) {
            let _ = f.write_all(line.as_bytes());
        }
    }

    fn log_error(&self, file: &str, msg: &str) {
        self.log(file, msg);
        self.log("error", &format!("[{file}] {msg}"));
    }

    fn stdio_log(&self, name: &str) -> Option<File> {
        OpenOptions::new()
            .create(true)
            .append(true)
            .open(self.data_dir.join("logs").join(format!("{name}-stdio.log")))
            .ok()
    }

    fn tail_of(&self, name: &str) -> String {
        let path = self.data_dir.join("logs").join(format!("{name}-stdio.log"));
        match fs::read_to_string(path) {
            Ok(s) => {
                let t = s.trim_end();
                let start = t.len().saturating_sub(600);
                let start = (start..t.len()).find(|i| t.is_char_boundary(*i)).unwrap_or(0);
                t[start..].to_string()
            }
            Err(_) => String::new(),
        }
    }

    // ---------- status ----------

    fn set<F: FnOnce(&mut Status)>(&self, app: &AppHandle, f: F) {
        let mut s = self.status.lock().unwrap();
        let before = s.clone();
        f(&mut s);
        s.overall = overall_of(&s);
        if *s != before {
            let _ = app.emit("status", s.clone());
        }
    }

    // ---------- lifecycle ----------

    pub fn start(self: &Arc<Self>, app: AppHandle) {
        let me = self.clone();
        thread::spawn(move || me.run(app));
    }

    fn run(self: Arc<Self>, app: AppHandle) {
        self.log("startup", &format!("supervisor started (shell pid {})", std::process::id()));
        let mut backend_restarts: Vec<Instant> = Vec::new();
        let mut agent_restarts: Vec<Instant> = Vec::new();
        let mut bad_health: u32 = 0;
        let mut agent_wait_since: Option<Instant> = None;
        let mut ui_reloads: Vec<Instant> = Vec::new();

        while !self.shutdown_flag.load(Ordering::SeqCst) {
            if self.restart_requested.swap(false, Ordering::SeqCst) {
                self.log("startup", "manual restart requested; retry counters reset");
                backend_restarts.clear();
                agent_restarts.clear();
                self.kill_agent();
                self.kill_backend();
                *self.info.lock().unwrap() = None;
                self.set(&app, |s| {
                    s.failed = false;
                    s.detail.clear();
                    s.backend = "starting".into();
                    s.agent = "waiting".into();
                });
            }

            if self.status.lock().unwrap().failed {
                thread::sleep(Duration::from_millis(500));
                continue;
            }

            let info = self.info.lock().unwrap().clone();
            match info {
                None => {
                    self.set(&app, |s| {
                        s.backend = "starting".into();
                        s.port = 0;
                    });
                    if let Some(found) = self.attach_existing() {
                        self.log("startup", &format!("attached to running backend on port {}", found.port));
                        self.owns_backend.store(false, Ordering::SeqCst);
                        let port = found.port;
                        *self.info.lock().unwrap() = Some(found);
                        self.set(&app, |s| {
                            s.port = port;
                            s.attached_to_existing = true;
                        });
                    } else if !within_budget(&mut backend_restarts) {
                        let msg = format!(
                            "Backend failed to stay up after {MAX_RESTARTS} attempts in {}s.",
                            RESTART_WINDOW.as_secs()
                        );
                        self.log_error("startup", &msg);
                        self.set(&app, |s| {
                            s.failed = true;
                            s.backend = "offline".into();
                            s.detail = msg.clone();
                        });
                    } else {
                        let attempts = backend_restarts.len() as u32;
                        self.set(&app, |s| s.backend_restarts = attempts.saturating_sub(1));
                        match self.start_backend() {
                            Ok(found) => {
                                self.log("startup", &format!("backend ready on port {}", found.port));
                                let port = found.port;
                                *self.info.lock().unwrap() = Some(found);
                                self.set(&app, |s| {
                                    s.port = port;
                                    s.attached_to_existing = false;
                                    s.detail.clear();
                                });
                            }
                            Err(e) => {
                                self.log_error("startup", &format!("backend start failed: {e}"));
                                self.set(&app, |s| {
                                    s.backend = "offline".into();
                                    s.detail = e.clone();
                                });
                                thread::sleep(Duration::from_secs(2));
                            }
                        }
                    }
                    bad_health = 0;
                }
                Some(info) => {
                    match self.health(info.port) {
                        Some(h) if h.pid == info.pid => {
                            bad_health = 0;
                            self.watch_window(&app, h.ui_age_s, &mut ui_reloads);
                            self.set(&app, |s| {
                                s.backend = "online".into();
                                s.ever_online = true;
                                if s.agent == "online" && !h.agent_connected {
                                    s.agent = "starting".into();
                                }
                            });
                            self.supervise_agent(&app, &info, h.agent_connected, &mut agent_restarts, &mut agent_wait_since);
                        }
                        other => {
                            bad_health += 1;
                            let exited = self.backend_child_exited();
                            if other.is_some() {
                                self.log_error("startup", "backend pid changed; treating as restart");
                            }
                            if bad_health == 5 {
                                self.log("startup", "backend is slow to respond (PC under load?); waiting before restarting it");
                            }
                            if bad_health >= HEALTH_FAILURES_BEFORE_RESTART || exited || other.is_some() {
                                self.log_error(
                                    "startup",
                                    &format!("backend lost (failed checks: {bad_health}, process exited: {exited}); restarting"),
                                );
                                self.set(&app, |s| {
                                    s.backend = "offline".into();
                                    s.agent = "waiting".into();
                                });
                                self.kill_agent();
                                self.kill_backend();
                                *self.info.lock().unwrap() = None;
                                agent_wait_since = None;
                                bad_health = 0;
                            }
                        }
                    }
                }
            }
            thread::sleep(Duration::from_millis(1000));
        }
    }

    fn supervise_agent(
        &self,
        app: &AppHandle,
        info: &ConnInfo,
        agent_connected: bool,
        restarts: &mut Vec<Instant>,
        wait_since: &mut Option<Instant>,
    ) {
        if agent_connected {
            *wait_since = None;
            self.set(app, |s| {
                s.agent = "online".into();
            });
            return;
        }

        let alive = {
            let mut guard = self.agent_child.lock().unwrap();
            match guard.as_mut() {
                Some(c) => matches!(c.try_wait(), Ok(None)),
                None => false,
            }
        };

        if alive {
            let since = wait_since.get_or_insert_with(Instant::now);
            if since.elapsed() > AGENT_CONNECT_TIMEOUT {
                self.log_error("agent", "agent is running but never connected; restarting it");
                self.kill_agent();
                *wait_since = None;
            }
            self.set(app, |s| s.agent = "starting".into());
            return;
        }

        if self.agent_child.lock().unwrap().take().is_some() {
            self.log_error("agent", &format!("agent process exited. Output tail:\n{}", self.tail_of("agent")));
        }

        if !within_budget(restarts) {
            let msg = format!("Agent failed to stay up after {MAX_RESTARTS} attempts in {}s.", RESTART_WINDOW.as_secs());
            self.log_error("agent", &msg);
            self.set(app, |s| {
                s.agent = "failed".into();
                s.detail = msg.clone();
            });
            return;
        }

        let count = restarts.len() as u32;
        match self.spawn_agent(info) {
            Ok(child) => {
                self.log("agent", &format!("agent started (pid {})", child.id()));
                *self.agent_child.lock().unwrap() = Some(child);
                *wait_since = Some(Instant::now());
                self.set(app, |s| {
                    s.agent = "starting".into();
                    s.agent_restarts = count.saturating_sub(1);
                });
            }
            Err(e) => {
                self.log_error("agent", &format!("agent start failed: {e}"));
                self.set(app, |s| {
                    s.agent = "failed".into();
                    s.detail = e.clone();
                });
            }
        }
    }

    // ---------- backend ----------

    fn attach_existing(&self) -> Option<ConnInfo> {
        for port in PORT_FIRST..=PORT_LAST {
            // If we can bind it, nothing is listening there. This check is instant, whereas
            // connecting to a closed port on Windows waits out the connect timeout.
            if TcpListener::bind(("127.0.0.1", port)).is_ok() {
                continue;
            }
            let Some(h) = self.health(port) else { continue };
            let Some(info) = self.read_runtime() else { continue };
            if info.port == port && info.pid == h.pid {
                return Some(info);
            }
        }
        None
    }

    fn read_runtime(&self) -> Option<ConnInfo> {
        let text = fs::read_to_string(self.data_dir.join("runtime.json")).ok()?;
        serde_json::from_str(&text).ok()
    }

    fn pick_port(&self) -> Result<u16, String> {
        for port in PORT_FIRST..=PORT_LAST {
            if TcpListener::bind(("127.0.0.1", port)).is_ok() {
                return Ok(port);
            }
        }
        Err(format!("No free port between {PORT_FIRST} and {PORT_LAST}."))
    }

    fn start_backend(&self) -> Result<ConnInfo, String> {
        let port = self.pick_port()?;
        let _ = fs::remove_file(self.data_dir.join("runtime.json"));
        let launch = launch_for("backend")?;
        let mut cmd = Command::new(&launch.program);
        cmd.args(&launch.args)
            .current_dir(&launch.cwd)
            .env("JARVIS_PORT", port.to_string())
            .env("JARVIS_DATA_DIR", &self.data_dir)
            .env("JARVIS_PARENT_PID", std::process::id().to_string())
            .env("PYTHONUNBUFFERED", "1")
            .env("PYTHONUTF8", "1");
        if let Some(p) = &launch.python_path {
            cmd.env("PYTHONPATH", p);
        }
        hide_window(&mut cmd);
        redirect(&mut cmd, self.stdio_log("backend"));
        let mut child = cmd
            .spawn()
            .map_err(|e| format!("Could not launch backend ({}): {e}", launch.program.display()))?;
        self.log("startup", &format!("backend launched (pid {}, port {port})", child.id()));

        let deadline = Instant::now() + BACKEND_READY_TIMEOUT;
        loop {
            if let Ok(Some(code)) = child.try_wait() {
                return Err(format!("Backend exited during startup ({code}). Output:\n{}", self.tail_of("backend")));
            }
            if let Some(h) = self.health(port) {
                if let Some(info) = self.read_runtime() {
                    if info.port == port && info.pid == h.pid {
                        *self.backend_child.lock().unwrap() = Some(child);
                        self.owns_backend.store(true, Ordering::SeqCst);
                        return Ok(info);
                    }
                }
            }
            if Instant::now() > deadline {
                let _ = child.kill();
                let _ = child.wait();
                return Err(format!(
                    "Backend did not become healthy within {}s. Output:\n{}",
                    BACKEND_READY_TIMEOUT.as_secs(),
                    self.tail_of("backend")
                ));
            }
            thread::sleep(Duration::from_millis(250));
        }
    }

    fn backend_child_exited(&self) -> bool {
        let mut guard = self.backend_child.lock().unwrap();
        match guard.as_mut() {
            Some(c) => !matches!(c.try_wait(), Ok(None)),
            None => false,
        }
    }

    fn kill_backend(&self) {
        if let Some(info) = self.info.lock().unwrap().clone() {
            if self.owns_backend.load(Ordering::SeqCst) {
                // Ask nicely first so it can flush logs and close the agent socket.
                http_request("POST", info.port, "/shutdown", Some(&info.token), Duration::from_millis(800));
            }
        }
        if let Some(mut child) = self.backend_child.lock().unwrap().take() {
            let deadline = Instant::now() + Duration::from_secs(3);
            loop {
                match child.try_wait() {
                    Ok(Some(_)) => break,
                    _ if Instant::now() > deadline => {
                        let _ = child.kill();
                        let _ = child.wait();
                        break;
                    }
                    _ => thread::sleep(Duration::from_millis(100)),
                }
            }
        }
    }

    // ---------- agent ----------

    fn spawn_agent(&self, info: &ConnInfo) -> Result<Child, String> {
        let launch = launch_for("agent")?;
        let mut cmd = Command::new(&launch.program);
        cmd.args(&launch.args)
            .current_dir(&launch.cwd)
            .env("JARVIS_PORT", info.port.to_string())
            .env("JARVIS_TOKEN", &info.token)
            .env("JARVIS_DATA_DIR", &self.data_dir)
            .env("JARVIS_PARENT_PID", std::process::id().to_string())
            .env("PYTHONUNBUFFERED", "1")
            .env("PYTHONUTF8", "1");
        if let Some(p) = &launch.python_path {
            cmd.env("PYTHONPATH", p);
        }
        hide_window(&mut cmd);
        redirect(&mut cmd, self.stdio_log("agent"));
        cmd.spawn()
            .map_err(|e| format!("Could not launch agent ({}): {e}", launch.program.display()))
    }

    fn kill_agent(&self) {
        if let Some(mut child) = self.agent_child.lock().unwrap().take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }

    /// Reload the window if it is on screen but has stopped checking in (frozen, blank or never loaded).
    /// Hidden or minimized windows are left alone: Windows slows their timers on purpose.
    fn watch_window(&self, app: &AppHandle, ui_age_s: Option<f64>, reloads: &mut Vec<Instant>) {
        let Some(age) = ui_age_s else { return };
        if age < UI_SILENT_RELOAD_S {
            return;
        }
        let Some(w) = app.get_webview_window("main") else { return };
        if !w.is_visible().unwrap_or(false) || w.is_minimized().unwrap_or(true) {
            return;
        }
        reloads.retain(|t| t.elapsed() < Duration::from_secs(60));
        if reloads.len() >= UI_RELOADS_PER_MIN {
            return;
        }
        reloads.push(Instant::now());
        self.log_error("crash", &format!("window silent for {age:.0}s while visible; reloading it"));
        if w.reload().is_err() {
            let _ = w.eval("window.location.reload()");
        }
    }

    // ---------- health ----------

    fn health(&self, port: u16) -> Option<Health> {
        let body = http_request("GET", port, "/health", None, HEALTH_TIMEOUT)?;
        let v: serde_json::Value = serde_json::from_str(&body).ok()?;
        if v.get("service")?.as_str()? != "jarvis-backend" {
            return None;
        }
        Some(Health {
            pid: v.get("pid")?.as_u64()? as u32,
            agent_connected: v.get("agent_connected").and_then(|x| x.as_bool()).unwrap_or(false),
            ui_age_s: v.get("ui_age_s").and_then(|x| x.as_f64()),
        })
    }

    // ---------- shutdown ----------

    pub fn shutdown(&self) {
        if self.shutdown_flag.swap(true, Ordering::SeqCst) {
            return;
        }
        self.log("startup", "shutdown requested");
        self.kill_agent();
        self.kill_backend();
        self.log("startup", "shutdown complete");
    }
}

// ---------- helpers ----------

fn overall_of(s: &Status) -> String {
    if s.failed {
        return "failed".into();
    }
    if s.backend == "online" {
        if s.agent == "online" {
            "online".into()
        } else if s.agent == "failed" {
            "degraded".into()
        } else if s.ever_online && s.agent != "starting" && s.agent != "waiting" {
            "degraded".into()
        } else {
            "starting".into()
        }
    } else if s.ever_online {
        "reconnecting".into()
    } else {
        "starting".into()
    }
}

fn within_budget(v: &mut Vec<Instant>) -> bool {
    v.retain(|t| t.elapsed() < RESTART_WINDOW);
    if v.len() >= MAX_RESTARTS {
        return false;
    }
    v.push(Instant::now());
    true
}

struct Launch {
    program: PathBuf,
    args: Vec<String>,
    cwd: PathBuf,
    /// Extra PYTHONPATH entry (development only; packaged builds bundle it).
    python_path: Option<PathBuf>,
}

fn launch_for(name: &str) -> Result<Launch, String> {
    if cfg!(debug_assertions) {
        // Development: run the Python packages straight from the repository.
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .map(|p| p.to_path_buf())
            .ok_or("cannot resolve project root")?;
        let venv_python = root.join(".venv").join("Scripts").join("python.exe");
        let program = if venv_python.exists() { venv_python } else { PathBuf::from("python") };
        let (module, dir) = match name {
            "backend" => ("jarvis_backend", "backend"),
            "agent" => ("jarvis_agent", "agent"),
            other => return Err(format!("unknown service {other}")),
        };
        let cwd = root.join(dir);
        if !cwd.exists() {
            return Err(format!("{} does not exist", cwd.display()));
        }
        Ok(Launch { program, args: vec!["-m".into(), module.into()], cwd, python_path: Some(root.join("common")) })
    } else {
        // Release: packaged sidecars sit next to the app executable.
        let exe = std::env::current_exe().map_err(|e| e.to_string())?;
        let dir = exe.parent().ok_or("cannot resolve app directory")?.to_path_buf();
        let program = dir.join(format!("jarvis-{name}")).join(format!("jarvis-{name}.exe"));
        if !program.exists() {
            return Err(format!("Missing packaged component: {}", program.display()));
        }
        let cwd = program.parent().unwrap().to_path_buf();
        Ok(Launch { program, args: vec![], cwd, python_path: None })
    }
}

#[cfg(windows)]
fn hide_window(cmd: &mut Command) {
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    cmd.creation_flags(CREATE_NO_WINDOW);
}

#[cfg(not(windows))]
fn hide_window(_cmd: &mut Command) {}

fn redirect(cmd: &mut Command, file: Option<File>) {
    cmd.stdin(Stdio::null());
    match file {
        Some(f) => {
            let err = f.try_clone();
            cmd.stdout(Stdio::from(f));
            match err {
                Ok(e) => cmd.stderr(Stdio::from(e)),
                Err(_) => cmd.stderr(Stdio::null()),
            };
        }
        None => {
            cmd.stdout(Stdio::null());
            cmd.stderr(Stdio::null());
        }
    }
}

/// Minimal blocking HTTP/1.1 client for localhost. Returns the body on a 200.
fn http_request(method: &str, port: u16, path: &str, token: Option<&str>, timeout: Duration) -> Option<String> {
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let mut stream = TcpStream::connect_timeout(&addr, timeout).ok()?;
    stream.set_read_timeout(Some(timeout)).ok()?;
    stream.set_write_timeout(Some(timeout)).ok()?;
    let auth = token.map(|t| format!("Authorization: Bearer {t}\r\n")).unwrap_or_default();
    let req = format!(
        "{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n{auth}Content-Length: 0\r\nConnection: close\r\n\r\n"
    );
    stream.write_all(req.as_bytes()).ok()?;
    let mut raw = Vec::new();
    let _ = stream.read_to_end(&mut raw);
    let text = String::from_utf8_lossy(&raw).to_string();
    if !text.starts_with("HTTP/1.1 200") && !text.starts_with("HTTP/1.0 200") {
        return None;
    }
    text.split_once("\r\n\r\n").map(|(_, body)| body.to_string())
}

fn timestamp() -> String {
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let (h, m, s) = ((secs / 3600) % 24, (secs / 60) % 60, secs % 60);
    // Civil date from days since 1970-01-01 (Howard Hinnant's algorithm).
    let z = (secs / 86400) as i64 + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let mo = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = yoe + era * 400 + if mo <= 2 { 1 } else { 0 };
    format!("{y:04}-{mo:02}-{d:02} {h:02}:{m:02}:{s:02}Z")
}

use crate::browser_network::{NetworkProxy, public_ip, resolve_public};
use anyhow::{Context, Result, bail, ensure};
use futures_util::{SinkExt, StreamExt};
use huifa_core::{App, process::ProcessTree};
use serde::Deserialize;
use serde_json::{Value, json};
use std::{
    collections::HashMap,
    path::PathBuf,
    process::Stdio,
    sync::Arc,
    time::{Duration, Instant},
};
use tokio::{
    process::{Child, Command},
    sync::{Mutex, broadcast, mpsc, oneshot},
};
use tokio_tungstenite::tungstenite::Message;

const WIDTH: u32 = 1280;
const HEIGHT: u32 = 800;
const IDLE_LIMIT: Duration = Duration::from_secs(15 * 60);

pub struct Browser {
    app: Arc<App>,
    executable: PathBuf,
    directory: PathBuf,
    running: Mutex<Option<Running>>,
    events: broadcast::Sender<Value>,
    shutdown: tokio_util::sync::CancellationToken,
}

struct Running {
    _proxy: NetworkProxy,
    child: Child,
    guard: ProcessTree,
    profile: PathBuf,
    channel: mpsc::Sender<Rpc>,
    driver: tokio::task::JoinHandle<()>,
    session: String,
    target_url: String,
    touched: Instant,
}

struct Rpc {
    method: &'static str,
    params: Value,
    session: Option<String>,
    reply: oneshot::Sender<Result<Value>>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Start {
    pub url: String,
}

#[derive(Deserialize)]
#[serde(tag = "type", rename_all = "camelCase", deny_unknown_fields)]
pub enum Input {
    Click {
        x: f64,
        y: f64,
    },
    Key {
        key: String,
    },
    Text {
        text: String,
    },
    Scroll {
        #[serde(rename = "deltaY")]
        delta_y: f64,
    },
}

impl Browser {
    pub fn new(app: Arc<App>, executable: PathBuf, directory: PathBuf) -> Result<Arc<Self>> {
        std::fs::create_dir_all(&directory)?;
        ensure!(
            !std::fs::symlink_metadata(&directory)?
                .file_type()
                .is_symlink(),
            "browser storage must not be a link"
        );
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&directory, std::fs::Permissions::from_mode(0o700))?;
        }
        // Profiles contain unencrypted live login state and are never reused across starts.
        for entry in std::fs::read_dir(&directory)? {
            let entry = entry?;
            if entry.file_type()?.is_dir()
                && uuid::Uuid::parse_str(&entry.file_name().to_string_lossy()).is_ok()
            {
                std::fs::remove_dir_all(entry.path())?;
            }
        }
        let (events, _) = broadcast::channel(8);
        let browser = Arc::new(Self {
            app,
            executable,
            directory: directory.canonicalize()?,
            running: Mutex::new(None),
            events,
            shutdown: tokio_util::sync::CancellationToken::new(),
        });
        let weak = Arc::downgrade(&browser);
        tokio::spawn(async move {
            loop {
                tokio::time::sleep(Duration::from_secs(30)).await;
                let Some(browser) = weak.upgrade() else { break };
                let mut state = browser.running.lock().await;
                if state
                    .as_ref()
                    .is_some_and(|r| r.touched.elapsed() >= IDLE_LIMIT || r.driver.is_finished())
                {
                    if let Some(running) = state.take() {
                        cleanup(running).await;
                    }
                    let _ = browser
                        .events
                        .send(json!({"kind":"browser.status","data":{"active":false}}));
                }
            }
        });
        Ok(browser)
    }

    pub fn subscribe(&self) -> broadcast::Receiver<Value> {
        self.events.subscribe()
    }

    pub async fn status(&self) -> Value {
        let state = self.running.lock().await;
        match state.as_ref() {
            Some(running) => {
                json!({"active":true,"targetUrl":running.target_url,"width":WIDTH,"height":HEIGHT})
            }
            None => json!({"active":false}),
        }
    }

    pub async fn start(&self, input: Start) -> Result<Value> {
        ensure!(!self.shutdown.is_cancelled(), "server is shutting down");
        let url = login_url(&input.url)?;
        let parsed = reqwest::Url::parse(&url)?;
        resolve_public(
            parsed.host_str().unwrap(),
            parsed.port_or_known_default().unwrap(),
        )
        .await?;
        let mut state = self.running.lock().await;
        ensure!(state.is_none(), "a remote login session is already active");
        let profile = self.directory.join(uuid::Uuid::new_v4().to_string());
        std::fs::create_dir(&profile)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&profile, std::fs::Permissions::from_mode(0o700))?;
        }
        let result = self.launch(profile.clone(), &url).await;
        match result {
            Ok(running) => {
                *state = Some(running);
                let _ = self.events.send(json!({"kind":"browser.status","data":{"active":true,"targetUrl":url,"width":WIDTH,"height":HEIGHT}}));
                Ok(json!({"active":true,"targetUrl":url,"width":WIDTH,"height":HEIGHT}))
            }
            Err(error) => {
                let _ = std::fs::remove_dir_all(profile);
                Err(error)
            }
        }
    }

    async fn launch(&self, profile: PathBuf, url: &str) -> Result<Running> {
        let proxy = NetworkProxy::start()?;
        let mut command = Command::new(&self.executable);
        command
            .args([
                "--headless=new",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                "--remote-debugging-address=127.0.0.1",
                "--remote-debugging-port=0",
                "--window-size=1280,800",
                "--disable-dev-shm-usage",
                "--disable-background-networking",
                "--disable-sync",
                "--proxy-bypass-list=<-loopback>",
                "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
                "--disable-quic",
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            ])
            .arg(format!("--proxy-server=socks5://{}", proxy.address))
            .arg(format!("--user-data-dir={}", profile.display()))
            .arg("about:blank")
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .kill_on_drop(true);
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;
            command.as_std_mut().process_group(0);
            // Container isolation and the non-root UID provide the sandbox where user namespaces are unavailable.
            if std::env::var_os("HUIFA_CONTAINER").is_some() {
                command.arg("--no-sandbox");
            }
        }
        #[cfg(windows)]
        command.creation_flags(0x08000000);
        let mut child = command
            .spawn()
            .context("cannot start Chromium; install the container's Chromium package")?;
        let guard = ProcessTree::attach(child.id().context("Chromium has no process ID")?)?;
        let startup = async {
            let endpoint = loop {
                if child.try_wait()?.is_some() {
                    bail!("Chromium exited during startup");
                }
                if let Ok(text) =
                    tokio::fs::read_to_string(profile.join("DevToolsActivePort")).await
                {
                    let mut lines = text.lines();
                    if let (Some(port), Some(path)) = (lines.next(), lines.next()) {
                        let port: u16 = port.parse()?;
                        ensure!(
                            path.starts_with("/devtools/browser/")
                                && !path.contains(['\r', '\n', '?', '#']),
                            "invalid Chromium endpoint"
                        );
                        break format!("ws://127.0.0.1:{port}{path}");
                    }
                }
                tokio::time::sleep(Duration::from_millis(100)).await;
            };
            let (socket, _) = tokio_tungstenite::connect_async(&endpoint).await?;
            let (channel, receiver) = mpsc::channel(16);
            let events = self.events.clone();
            let driver = tokio::spawn(cdp_driver(socket, receiver, events));
            let target = rpc(
                &channel,
                "Target.createTarget",
                json!({"url":"about:blank"}),
                None,
            )
            .await?;
            let target_id = target["targetId"]
                .as_str()
                .context("Chromium did not create a login tab")?;
            let attached = rpc(
                &channel,
                "Target.attachToTarget",
                json!({"targetId":target_id,"flatten":true}),
                None,
            )
            .await?;
            let session = attached["sessionId"]
                .as_str()
                .context("Chromium did not attach a login tab")?
                .to_owned();
            rpc(&channel, "Page.enable", json!({}), Some(&session)).await?;
            rpc(&channel, "Network.enable", json!({}), Some(&session)).await?;
            rpc(
                &channel,
                "Emulation.setDeviceMetricsOverride",
                json!({"width":WIDTH,"height":HEIGHT,"deviceScaleFactor":1,"mobile":false}),
                Some(&session),
            )
            .await?;
            rpc(&channel,"Page.startScreencast",json!({"format":"jpeg","quality":65,"maxWidth":WIDTH,"maxHeight":HEIGHT,"everyNthFrame":1}),Some(&session)).await?;
            rpc(
                &channel,
                "Page.navigate",
                json!({"url":url}),
                Some(&session),
            )
            .await?;
            Ok::<_, anyhow::Error>((channel, driver, session))
        };
        let result = tokio::select! {
            result = tokio::time::timeout(Duration::from_secs(30), startup) => result,
            _ = self.shutdown.cancelled() => {
                guard.terminate(); let _ = child.kill().await; let _ = child.wait().await;
                bail!("server is shutting down");
            }
        };
        match result {
            Ok(Ok((channel, driver, session))) => Ok(Running {
                _proxy: proxy,
                child,
                guard,
                profile,
                channel,
                driver,
                session,
                target_url: url.into(),
                touched: Instant::now(),
            }),
            error => {
                guard.terminate();
                let _ = child.kill().await;
                let _ = child.wait().await;
                match error {
                    Ok(Err(error)) => Err(error),
                    _ => Err(anyhow::anyhow!("Chromium startup timed out")),
                }
            }
        }
    }

    pub async fn input(&self, input: Input) -> Result<()> {
        let mut state = self.running.lock().await;
        let running = state
            .as_mut()
            .context("start a remote login session first")?;
        for (method, params) in input_commands(input)? {
            rpc(&running.channel, method, params, Some(&running.session)).await?;
        }
        running.touched = Instant::now();
        Ok(())
    }

    pub async fn save(&self, name: &str) -> Result<Value> {
        ensure!(
            !name.trim().is_empty() && name.len() <= 128 && !name.chars().any(char::is_control),
            "invalid cookie name"
        );
        let mut state = self.running.lock().await;
        let running = state
            .as_mut()
            .context("start a remote login session first")?;
        let result = rpc(
            &running.channel,
            "Network.getAllCookies",
            json!({}),
            Some(&running.session),
        )
        .await?;
        let text = netscape(&result["cookies"])?;
        let meta = self
            .app
            .command("cookies.import", json!({"name":name.trim(),"text":text}))
            .await?;
        if let Some(running) = state.take() {
            cleanup(running).await;
        }
        let _ = self
            .events
            .send(json!({"kind":"browser.status","data":{"active":false}}));
        Ok(meta)
    }

    pub async fn stop(&self) {
        let mut state = self.running.lock().await;
        if let Some(running) = state.take() {
            cleanup(running).await;
        }
        let _ = self
            .events
            .send(json!({"kind":"browser.status","data":{"active":false}}));
    }

    pub async fn shutdown(&self) {
        self.shutdown.cancel();
        self.stop().await;
    }
}

async fn cleanup(mut running: Running) {
    let _ = tokio::time::timeout(
        Duration::from_secs(2),
        rpc(&running.channel, "Browser.close", json!({}), None),
    )
    .await;
    if tokio::time::timeout(Duration::from_secs(3), running.child.wait())
        .await
        .is_err()
    {
        running.guard.terminate();
        let _ = running.child.kill().await;
        let _ = running.child.wait().await;
    }
    running.guard.terminate();
    running.driver.abort();
    let _ = running.driver.await;
    let _ = tokio::fs::remove_dir_all(&running.profile).await;
}

async fn rpc(
    channel: &mpsc::Sender<Rpc>,
    method: &'static str,
    params: Value,
    session: Option<&str>,
) -> Result<Value> {
    let (reply, receiver) = oneshot::channel();
    let request = Rpc {
        method,
        params,
        session: session.map(str::to_owned),
        reply,
    };
    tokio::time::timeout(Duration::from_secs(10), async {
        channel
            .send(request)
            .await
            .context("remote login session ended")?;
        receiver
            .await
            .context("remote login session ended")?
            .with_context(|| format!("Chromium command failed: {method}"))
    })
    .await
    .context("Chromium command timed out")?
}

async fn cdp_driver(
    socket: tokio_tungstenite::WebSocketStream<
        tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>,
    >,
    mut receiver: mpsc::Receiver<Rpc>,
    events: broadcast::Sender<Value>,
) {
    let (mut writer, mut reader) = socket.split();
    let mut next_id = 0u64;
    let mut pending: HashMap<u64, oneshot::Sender<Result<Value>>> = HashMap::new();
    loop {
        tokio::select! {
            request = receiver.recv() => {
                let Some(request) = request else { break };
                next_id += 1;
                let mut message = json!({"id":next_id,"method":request.method,"params":request.params});
                if let Some(session) = request.session { message["sessionId"] = Value::String(session); }
                if writer.send(Message::Text(message.to_string().into())).await.is_err() { break; }
                pending.retain(|_, reply| !reply.is_closed());
                pending.insert(next_id,request.reply);
            }
            message = reader.next() => {
                let Some(Ok(message)) = message else { break };
                let Message::Text(text) = message else { continue };
                let Ok(value) = serde_json::from_str::<Value>(&text) else { continue };
                if let Some(id) = value["id"].as_u64() {
                    if let Some(reply) = pending.remove(&id) {
                        let result = if value.get("error").is_some() { Err(anyhow::anyhow!("Chromium rejected the login action")) } else { Ok(value["result"].clone()) };
                        let _ = reply.send(result);
                    }
                } else if value["method"] == "Page.screencastFrame" {
                    let params = &value["params"];
                    if let Some(data) = params["data"].as_str().filter(|d| d.len() <= 4 * 1024 * 1024) {
                        let _ = events.send(json!({"kind":"browser.frame","data":{"data":data,"mimeType":"image/jpeg","width":WIDTH,"height":HEIGHT}}));
                    }
                    next_id += 1;
                    let ack = json!({"id":next_id,"method":"Page.screencastFrameAck","sessionId":value["sessionId"],"params":{"sessionId":params["sessionId"]}});
                    if writer.send(Message::Text(ack.to_string().into())).await.is_err() { break; }
                }
            }
        }
    }
    for (_, reply) in pending {
        let _ = reply.send(Err(anyhow::anyhow!("remote login session ended")));
    }
    let _ = events.send(json!({"kind":"browser.status","data":{"active":false}}));
}

fn login_url(text: &str) -> Result<String> {
    ensure!(text.len() <= 2048, "login URL is too long");
    let url = reqwest::Url::parse(text)?;
    ensure!(
        matches!(url.scheme(), "http" | "https")
            && url.host_str().is_some()
            && url.username().is_empty()
            && url.password().is_none(),
        "login URL must be an HTTP or HTTPS website"
    );
    let host = url.host_str().unwrap();
    ensure!(
        !matches!(host, "localhost" | "localhost.localdomain") && !host.ends_with(".local"),
        "local login URLs are unavailable"
    );
    if let Ok(ip) = host.trim_matches(['[', ']']).parse::<std::net::IpAddr>() {
        ensure!(public_ip(ip), "local login URLs are unavailable");
    }
    Ok(url.to_string())
}

fn input_commands(input: Input) -> Result<Vec<(&'static str, Value)>> {
    match input {
        Input::Click { x, y } => {
            ensure!(
                x.is_finite()
                    && y.is_finite()
                    && x >= 0.0
                    && y >= 0.0
                    && x < WIDTH as f64
                    && y < HEIGHT as f64,
                "click is outside the browser viewport"
            );
            Ok(vec![
                (
                    "Input.dispatchMouseEvent",
                    json!({"type":"mousePressed","x":x,"y":y,"button":"left","clickCount":1}),
                ),
                (
                    "Input.dispatchMouseEvent",
                    json!({"type":"mouseReleased","x":x,"y":y,"button":"left","clickCount":1}),
                ),
            ])
        }
        Input::Text { text } => {
            ensure!(
                !text.is_empty() && text.len() <= 8192 && !text.contains('\0'),
                "input text must be 1 to 8192 bytes"
            );
            Ok(vec![("Input.insertText", json!({"text":text}))])
        }
        Input::Key { key } => {
            let code = match key.as_str() {
                "Enter" => 13,
                "Tab" => 9,
                "Backspace" => 8,
                "Escape" => 27,
                "ArrowLeft" => 37,
                "ArrowUp" => 38,
                "ArrowRight" => 39,
                "ArrowDown" => 40,
                "Delete" => 46,
                "Home" => 36,
                "End" => 35,
                _ => bail!("unsupported browser key"),
            };
            Ok(vec![
                (
                    "Input.dispatchKeyEvent",
                    json!({"type":"keyDown","key":key,"windowsVirtualKeyCode":code,"nativeVirtualKeyCode":code}),
                ),
                (
                    "Input.dispatchKeyEvent",
                    json!({"type":"keyUp","key":key,"windowsVirtualKeyCode":code,"nativeVirtualKeyCode":code}),
                ),
            ])
        }
        Input::Scroll { delta_y } => {
            ensure!(
                delta_y.is_finite() && delta_y.abs() <= 2000.0,
                "scroll delta is too large"
            );
            Ok(vec![(
                "Input.dispatchMouseEvent",
                json!({"type":"mouseWheel","x":WIDTH/2,"y":HEIGHT/2,"deltaX":0,"deltaY":delta_y}),
            )])
        }
    }
}

fn netscape(cookies: &Value) -> Result<String> {
    let cookies = cookies
        .as_array()
        .context("Chromium returned invalid cookies")?;
    ensure!(
        !cookies.is_empty() && cookies.len() <= 10000,
        "no cookies to save, or too many cookies"
    );
    let mut text = String::from("# Netscape HTTP Cookie File\n# Saved by Huifa Downloader Rust\n");
    for cookie in cookies {
        let domain = cookie["domain"].as_str().context("invalid cookie domain")?;
        let path = cookie["path"].as_str().context("invalid cookie path")?;
        let name = cookie["name"].as_str().context("invalid cookie name")?;
        let value = cookie["value"].as_str().context("invalid cookie value")?;
        ensure!(
            !domain.is_empty()
                && !path.is_empty()
                && !name.is_empty()
                && ![domain, path, name, value]
                    .iter()
                    .any(|s| s.chars().any(char::is_control)),
            "invalid cookie content"
        );
        let prefix = if cookie["httpOnly"].as_bool().unwrap_or(false) {
            "#HttpOnly_"
        } else {
            ""
        };
        let subdomains = if domain.starts_with('.') {
            "TRUE"
        } else {
            "FALSE"
        };
        let secure = if cookie["secure"].as_bool().unwrap_or(false) {
            "TRUE"
        } else {
            "FALSE"
        };
        let expires = cookie["expires"].as_f64().unwrap_or(0.0).max(0.0) as i64;
        text.push_str(&format!(
            "{prefix}{domain}\t{subdomains}\t{path}\t{secure}\t{expires}\t{name}\t{value}\n"
        ));
        ensure!(text.len() <= 2 * 1024 * 1024, "cookies exceed 2 MiB");
    }
    Ok(text)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn remote_input_and_cookie_validation() -> Result<()> {
        assert!(login_url("file:///etc/passwd").is_err());
        assert!(login_url("http://127.0.0.1").is_err());
        assert!(login_url("http://[::1]").is_err());
        assert!(login_url("http://[::ffff:127.0.0.1]").is_err());
        assert!(login_url("http://[::ffff:192.168.1.1]").is_err());
        assert!(login_url("https://www.youtube.com").is_ok());
        assert!(input_commands(Input::Click { x: -1.0, y: 0.0 }).is_err());
        assert_eq!(
            input_commands(Input::Click { x: 100.0, y: 200.0 })?.len(),
            2
        );
        assert!(
            input_commands(Input::Key {
                key: "ExecuteScript".into()
            })
            .is_err()
        );
        assert!(
            input_commands(Input::Text {
                text: "x".repeat(8193)
            })
            .is_err()
        );
        let text = netscape(
            &json!([{"domain":".example.com","path":"/","name":"session","value":"secret","httpOnly":true,"secure":true,"expires":-1}]),
        )?;
        assert!(text.contains("#HttpOnly_.example.com\tTRUE\t/\tTRUE\t0\tsession\tsecret"));
        assert!(
            netscape(&json!([{"domain":"x","path":"/","name":"session","value":"bad\nrow"}]))
                .is_err()
        );
        Ok(())
    }
}

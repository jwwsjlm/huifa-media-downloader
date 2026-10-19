use anyhow::{Context, Result, bail, ensure};
use huifa_core::{
    App,
    browser::{Cdp, netscape},
    process::ProcessTree,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    io::{Read, Write},
    path::{Path, PathBuf},
    process::Stdio,
    time::Duration,
};
use tauri::{
    AppHandle, Manager, Webview, WebviewBuilder, WebviewUrl, WebviewWindow, WebviewWindowBuilder,
    WindowBuilder,
};
use tokio::{
    process::{Child, Command},
    sync::Mutex,
};

pub(super) mod navigation;
#[cfg(all(test, windows, feature = "login-smoke"))]
mod smoke;

pub struct LoginBrowser {
    directory: PathBuf,
    system: Option<(String, PathBuf)>,
    running: Mutex<Option<Session>>,
    synced: std::sync::Mutex<SyncState>,
    preferred: std::sync::Mutex<Preferences>,
    preference_error: std::sync::Mutex<Option<String>>,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
struct Preferences {
    url: String,
    mode: String,
}

impl Default for Preferences {
    fn default() -> Self {
        Self {
            url: "https://www.youtube.com/".into(),
            mode: default_login_mode().into(),
        }
    }
}

impl Preferences {
    fn new(url: &str, mode: &str) -> Result<Self> {
        ensure!(matches!(mode, "embedded" | "system"), "Invalid login mode");
        Ok(Self {
            url: login_url(url)?.to_string(),
            mode: if mode == "embedded" && !embedded_available() {
                "system"
            } else {
                mode
            }
            .into(),
        })
    }

    fn load(directory: &Path) -> Self {
        Self::read(directory).unwrap_or_default()
    }

    fn read(directory: &Path) -> Result<Self> {
        let path = directory.join("preferences.json");
        ensure!(
            std::fs::symlink_metadata(&path)?.file_type().is_file(),
            "Invalid login preferences file"
        );
        let mut bytes = Vec::new();
        std::fs::File::open(path)?
            .take(4097)
            .read_to_end(&mut bytes)?;
        ensure!(bytes.len() <= 4096, "Login preferences are too large");
        let value: Self = serde_json::from_slice(&bytes)?;
        Self::new(&value.url, &value.mode)
    }
}

fn embedded_available() -> bool {
    cfg!(any(windows, target_os = "linux"))
}

fn default_login_mode() -> &'static str {
    if embedded_available() {
        "embedded"
    } else {
        "system"
    }
}

fn validate_login_mode(mode: &str) -> Result<()> {
    ensure!(matches!(mode, "embedded" | "system"), "Invalid login mode");
    ensure!(
        mode != "embedded" || embedded_available(),
        "Built-in sign-in is unavailable on this platform; use Chrome, Edge or Chromium"
    );
    Ok(())
}

#[derive(Default)]
struct SyncState {
    saved: Option<Value>,
    error: Option<String>,
    name: Option<String>,
}

#[derive(Clone, Debug, PartialEq)]
struct BrowserProxy(Option<tauri::Url>);

impl BrowserProxy {
    fn parse(text: &str) -> Result<Self> {
        let text = text.trim();
        if text.is_empty() {
            return Ok(Self(None));
        }
        let mut url = tauri::Url::parse(text).context("Invalid login browser proxy")?;
        ensure!(
            matches!(url.scheme(), "http" | "https" | "socks5" | "socks5h")
                && url.host_str().is_some()
                && url.port() != Some(0)
                && matches!(url.path(), "" | "/")
                && url.query().is_none()
                && url.fragment().is_none(),
            "Invalid login browser proxy"
        );
        ensure!(
            url.username().is_empty() && url.password().is_none(),
            "Login browsers require a proxy without embedded credentials; use a local proxy endpoint"
        );
        if url.scheme() == "socks5h" {
            url.set_scheme("socks5")
                .map_err(|_| anyhow::anyhow!("Invalid login browser proxy"))?;
        }
        Ok(Self(Some(url)))
    }

    fn arguments(&self) -> Vec<String> {
        let mut arguments = Vec::new();
        if let Some(url) = &self.0 {
            arguments.push(format!(
                "--proxy-server={}://{}:{}",
                url.scheme(),
                url.host().unwrap(),
                url.port_or_known_default().unwrap_or(1080)
            ));
            arguments.extend(
                [
                    "--proxy-bypass-list=<-loopback>",
                    "--disable-quic",
                    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                ]
                .map(str::to_owned),
            );
        } else {
            arguments.push("--no-proxy-server".into());
        }
        #[cfg(all(test, windows, feature = "login-smoke"))]
        arguments.extend(["--host-resolver-rules=MAP *.huifa.test 127.0.0.1, MAP direct-only.invalid 127.0.0.1", "--proxy-bypass-list=*.huifa.test"].map(str::to_owned));
        arguments
    }

    #[cfg(windows)]
    fn webview_arguments(&self) -> String {
        self.arguments()
            .into_iter()
            .map(|argument| {
                if argument.contains(' ') {
                    format!("\"{argument}\"")
                } else {
                    argument
                }
            })
            .collect::<Vec<_>>()
            .join(" ")
    }
}

enum Session {
    Embedded {
        label: String,
        url: String,
        proxy: BrowserProxy,
    },
    System {
        child: Box<Child>,
        guard: ProcessTree,
        cdp: Cdp,
        url: String,
        target_id: std::sync::Mutex<Option<String>>,
        proxy: BrowserProxy,
    },
}

impl LoginBrowser {
    pub fn new(directory: PathBuf) -> Result<Self> {
        private_directory(&directory)?;
        let preferred = Preferences::load(&directory);
        Ok(Self {
            directory,
            system: find_browser(),
            running: Mutex::new(None),
            synced: Default::default(),
            preferred: std::sync::Mutex::new(preferred),
            preference_error: Default::default(),
        })
    }

    pub async fn command(
        &self,
        app: &AppHandle,
        core: &App,
        command: &str,
        args: &Value,
    ) -> Result<Value> {
        let mut running = self.running.lock().await;
        if running.as_mut().is_some_and(|session| !session.active(app))
            && let Some(session) = running.take()
        {
            session.stop(app).await;
        }
        match command {
            "browser.login.status" => {
                let _ = self.sync_running(app, core, &mut running).await;
                Ok(self.status(&running))
            }
            "browser.login.open" => {
                let url = login_url(args["url"].as_str().context("URL must be text")?)?;
                let mode = args["mode"].as_str().context("Login mode is required")?;
                validate_login_mode(mode)?;
                let saved_proxy;
                let proxy = if let Some(proxy) = args.get("proxy") {
                    proxy.as_str().context("Proxy must be text")?
                } else {
                    saved_proxy = core.download_proxy()?;
                    &saved_proxy
                };
                let proxy = BrowserProxy::parse(proxy)?;
                let name = args
                    .get("name")
                    .and_then(Value::as_str)
                    .map(str::trim)
                    .filter(|name| !name.is_empty());
                ensure!(name.is_none_or(|name| name.len() <= 128 && !name.chars().any(char::is_control)), "Invalid cookie name");
                if let Some(session) = running.as_ref() {
                    ensure!(
                        session.mode() == mode
                            && session.url() == url.as_str()
                            && session.proxy() == &proxy,
                        "Close the active login browser before changing website, mode or proxy"
                    );
                    if let Session::Embedded { label, .. } = session
                        && let Some(window) = app.get_window(label)
                    {
                        window.show()?;
                        window.set_focus()?;
                    }
                    return Ok(self.status(&running));
                }
                let session = if mode == "embedded" {
                    let profile = self.directory.join("embedded");
                    private_directory(&profile)?;
                    let label = format!("login-{}", uuid::Uuid::new_v4());
                    let window = embedded_browser(app, &label, &profile, &proxy)?;
                    if let Err(reason) = initialize_storage(&window, &profile, &proxy)
                        .await
                        .and_then(|()| window.navigate(url.clone()).map_err(Into::into))
                    {
                        close_windows(app, &label);
                        return Err(reason);
                    }
                    Session::Embedded {
                        label,
                        url: url.to_string(),
                        proxy,
                    }
                } else {
                    let (name, executable) = self
                        .system
                        .as_ref()
                        .context("Install Edge, Chrome or Chromium to use compatible sign-in")?;
                    let profile = self.directory.join(name);
                    private_directory(&profile)?;
                    launch_system(executable, &profile, url.as_str(), &proxy).await?
                };
                *running = Some(session);
                let _ = self.remember(url.as_str(), mode);
                *self.synced.lock().unwrap() = SyncState {
                    name: name.map(str::to_owned),
                    ..Default::default()
                };
                Ok(self.status(&running))
            }
            "browser.login.save" => {
                let name = args["name"].as_str().context("Cookie name must be text")?;
                ensure!(
                    !name.trim().is_empty()
                        && name.len() <= 128
                        && !name.chars().any(char::is_control),
                    "Invalid cookie name"
                );
                let session = running
                    .as_ref()
                    .context("Open a login browser before saving cookies")?;
                self.sync_session(app, core, session, Some(name)).await?;
                self.synced
                    .lock()
                    .unwrap()
                    .saved
                    .clone()
                    .context("No cookies to save")
            }
            "browser.login.close" => {
                if let Some(session) = running.take() {
                    let saved = self.sync_session(app, core, &session, None).await;
                    session.stop(app).await;
                    saved?;
                }
                Ok(json!(true))
            }
            _ => bail!("Unknown login browser command"),
        }
    }

    fn status(&self, running: &Option<Session>) -> Value {
        let synced = self.synced.lock().unwrap();
        let preferred = self.preferred.lock().unwrap();
        let preference_error = self.preference_error.lock().unwrap();
        json!({"active":running.is_some(),"mode":running.as_ref().map(Session::mode),"targetUrl":running.as_ref().map(|_| preferred.url.as_str()),"preferredUrl":preferred.url,"preferredMode":preferred.mode,"embeddedAvailable":embedded_available(),"preferenceError":*preference_error,"systemBrowser":self.system.as_ref().map(|(name,_)|name),"cookie":synced.saved.as_ref().map(|value| &value["cookie"]),"saveError":synced.error})
    }

    fn remember(&self, url: &str, mode: &str) -> Result<()> {
        let next = Preferences::new(url, mode)?;
        let mut preferred = self.preferred.lock().unwrap();
        let path = self.directory.join("preferences.json");
        if *preferred == next && Preferences::read(&self.directory).is_ok_and(|saved| saved == next)
        {
            *self.preference_error.lock().unwrap() = None;
            return Ok(());
        }
        let saved = (|| -> Result<()> {
            let mut file = tempfile::NamedTempFile::new_in(&self.directory)?;
            file.write_all(&serde_json::to_vec(&next)?)?;
            file.as_file().sync_all()?;
            file.persist(path).map_err(|reason| reason.error)?;
            Ok(())
        })();
        *preferred = next;
        *self.preference_error.lock().unwrap() = saved
            .as_ref()
            .err()
            .map(|reason| huifa_core::process::redact(&format!("{reason:#}")));
        saved
    }

    async fn remember_session(&self, app: &AppHandle, session: &Session) -> Result<()> {
        let url = match session {
            Session::Embedded { label, .. } => app
                .get_webview(&format!("{label}-page"))
                .context("Login window was closed")?
                .url()?
                .to_string(),
            Session::System {
                cdp,
                url,
                target_id,
                ..
            } => {
                let targets = cdp.request("Target.getTargets", json!({}), None).await?;
                let pages: Vec<_> = targets["targetInfos"]
                    .as_array()
                    .context("Browser returned invalid windows")?
                    .iter()
                    .filter(|target| target["type"] == "page")
                    .collect();
                let mut target_id = target_id.lock().unwrap();
                let target = if let Some(id) = target_id.as_ref() {
                    pages.iter().find(|target| target["targetId"] == *id)
                } else {
                    pages
                        .iter()
                        .find(|target| target["url"] == *url)
                        .or_else(|| pages.iter().find(|target| target["openerId"].is_null()))
                }
                .context("No website is open in the login browser")?;
                *target_id = target["targetId"].as_str().map(str::to_owned);
                target["url"]
                    .as_str()
                    .context("Browser returned an invalid website")?
                    .to_owned()
            }
        };
        self.remember(&url, session.mode())
    }

    async fn sync_session(
        &self,
        app: &AppHandle,
        core: &App,
        session: &Session,
        name: Option<&str>,
    ) -> Result<()> {
        // Preferences must survive even when cookie export fails.
        let _ = self.remember_session(app, session).await;
        let result = async {
            let cookies = match session {
                Session::Embedded { label, .. } => {
                    native_cookies(
                        &app.get_webview(&format!("{label}-page"))
                            .context("Login window was closed")?,
                    )
                    .await?
                }
                Session::System { cdp, .. } => {
                    cdp.request("Storage.getCookies", json!({}), None).await?["cookies"].clone()
                }
            };
            let profile = if session.mode() == "embedded" {
                "embedded"
            } else {
                &self
                    .system
                    .as_ref()
                    .context("System browser is unavailable")?
                    .0
            };
            let text = if cookies.as_array().is_some_and(Vec::is_empty) {
                None
            } else {
                Some(netscape(&cookies)?)
            };
            let name = name
                .map(str::to_owned)
                .or_else(|| self.synced.lock().unwrap().name.clone())
                .unwrap_or_else(|| format!("Website sign-in · {profile}"));
            core.sync_login_cookies(profile, &name, text.as_deref())
        }
        .await;
        let mut synced = self.synced.lock().unwrap();
        match result {
            Ok(saved) => {
                synced.saved = saved;
                synced.error = None;
                Ok(())
            }
            Err(reason) => {
                synced.error = Some(huifa_core::process::redact(&format!("{reason:#}")));
                Err(reason)
            }
        }
    }

    pub async fn sync(&self, app: &AppHandle, core: &App) -> Result<()> {
        let mut running = self.running.lock().await;
        self.sync_running(app, core, &mut running).await
    }

    async fn sync_running(
        &self,
        app: &AppHandle,
        core: &App,
        running: &mut Option<Session>,
    ) -> Result<()> {
        if running.as_mut().is_some_and(|session| !session.active(app)) {
            if let Some(session) = running.take() {
                session.stop(app).await;
            }
        } else if let Some(session) = running.as_ref() {
            self.sync_session(app, core, session, None).await?;
            if let Session::System { cdp, .. } = session {
                let targets = cdp.request("Target.getTargets", json!({}), None).await?;
                let visible = targets["targetInfos"]
                    .as_array()
                    .context("Browser returned invalid windows")?
                    .iter()
                    .any(|target| target["type"] == "page");
                if !visible && let Some(session) = running.take() {
                    session.stop(app).await;
                }
            }
        }
        Ok(())
    }

    pub async fn close_window(&self, app: &AppHandle, core: &App, label: &str) -> Result<()> {
        let mut running = self.running.lock().await;
        if let Some(Session::Embedded { label: root, .. }) = running.as_ref()
            && root == label
        {
            if let Some(session) = running.take() {
                let saved = self.sync_session(app, core, &session, None).await;
                session.stop(app).await;
                saved?;
            }
        } else if let Some(window) = app.get_window(label) {
            window.destroy()?;
        }
        Ok(())
    }

    pub async fn stop(&self, app: &AppHandle, core: &App) {
        let mut running = self.running.lock().await;
        if let Some(session) = running.take() {
            let _ = self.sync_session(app, core, &session, None).await;
            session.stop(app).await;
        }
    }

    pub async fn window_closed(&self, app: &AppHandle, label: &str) {
        let mut running = self.running.lock().await;
        if matches!(running.as_ref(), Some(Session::Embedded { label: root, .. }) if root == label)
            && let Some(session) = running.take()
        {
            session.stop(app).await;
        }
    }
}

impl Session {
    fn proxy(&self) -> &BrowserProxy {
        match self {
            Self::Embedded { proxy, .. } | Self::System { proxy, .. } => proxy,
        }
    }

    fn mode(&self) -> &'static str {
        match self {
            Self::Embedded { .. } => "embedded",
            Self::System { .. } => "system",
        }
    }
    fn url(&self) -> &str {
        match self {
            Self::Embedded { url, .. } | Self::System { url, .. } => url,
        }
    }
    fn active(&mut self, app: &AppHandle) -> bool {
        match self {
            Self::Embedded { label, .. } => app.get_window(label).is_some(),
            Self::System { child, cdp, .. } => {
                child.try_wait().ok().flatten().is_none() && !cdp.is_closed()
            }
        }
    }
    async fn stop(mut self, app: &AppHandle) {
        match &mut self {
            Self::Embedded { label, .. } => close_windows(app, label),
            Self::System {
                child, guard, cdp, ..
            } => {
                let _ = tokio::time::timeout(
                    Duration::from_secs(2),
                    cdp.request("Browser.close", json!({}), None),
                )
                .await;
                // The network process must finish writing its cookie database before supervision ends.
                let drained = tokio::time::timeout(Duration::from_secs(10), async {
                    child.wait().await?;
                    while guard.is_running()? {
                        tokio::time::sleep(Duration::from_millis(50)).await;
                    }
                    Ok::<_, anyhow::Error>(())
                })
                .await;
                if !matches!(drained, Ok(Ok(()))) {
                    guard.terminate();
                    let _ = child.kill().await;
                    let _ = child.wait().await;
                }
            }
        }
    }
}

fn private_directory(path: &Path) -> Result<()> {
    std::fs::create_dir_all(path)?;
    ensure!(
        !std::fs::symlink_metadata(path)?.file_type().is_symlink(),
        "Browser storage must not be a link"
    );
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700))?;
    }
    Ok(())
}

fn login_url(text: &str) -> Result<tauri::Url> {
    ensure!(text.len() <= 2048, "Login URL is too long");
    let url = tauri::Url::parse(text)?;
    ensure!(
        website(&url),
        "Login URL must be an HTTP or HTTPS website without a username or password"
    );
    Ok(url)
}

fn website(url: &tauri::Url) -> bool {
    matches!(url.scheme(), "http" | "https")
        && url.host_str().is_some()
        && url.username().is_empty()
        && url.password().is_none()
}

fn close_windows(app: &AppHandle, group: &str) {
    for (label, window) in app.windows() {
        if label == group || label.starts_with(&format!("{group}-")) {
            let _ = window.destroy();
        }
    }
}

const TOOLBAR_HEIGHT: f64 = 44.0;

fn layout_browser(app: &AppHandle, label: &str) -> Result<()> {
    let window = app.get_window(label).context("Login window was closed")?;
    let size = window
        .inner_size()?
        .to_logical::<f64>(window.scale_factor()?);
    for (suffix, top, height) in [
        ("toolbar", 0.0, TOOLBAR_HEIGHT),
        (
            "page",
            TOOLBAR_HEIGHT,
            (size.height - TOOLBAR_HEIGHT).max(1.0),
        ),
    ] {
        if let Some(webview) = app.get_webview(&format!("{label}-{suffix}")) {
            webview.set_bounds(tauri::Rect {
                position: tauri::LogicalPosition::new(0.0, top).into(),
                size: tauri::LogicalSize::new(size.width, height).into(),
            })?;
        }
    }
    Ok(())
}

fn embedded_browser(
    app: &AppHandle,
    label: &str,
    profile: &Path,
    proxy: &BrowserProxy,
) -> Result<Webview> {
    let window = WindowBuilder::new(app, label)
        .title("Huifa — Website sign-in")
        .inner_size(1100.0, 780.0)
        .min_inner_size(480.0, 320.0)
        .visible(false)
        .build()?;
    let result = (|| {
        let toolbar_profile = profile
            .parent()
            .context("Invalid browser profile")?
            .join("toolbar");
        private_directory(&toolbar_profile)?;
        let toolbar_url = WebviewUrl::App("index.html?login-toolbar".into());
        window.add_child(
            WebviewBuilder::new(format!("{label}-toolbar"), toolbar_url)
                .data_directory(toolbar_profile)
                .on_navigation(|url| {
                    url.path() == "/index.html"
                        && url.query() == Some("login-toolbar")
                        && (url.scheme() == "tauri"
                            || url.host_str() == Some("tauri.localhost")
                            || cfg!(debug_assertions) && url.host_str() == Some("127.0.0.1"))
                })
                .on_new_window(|_, _| tauri::webview::NewWindowResponse::Deny)
                .on_download(|_, _| false),
            tauri::LogicalPosition::new(0.0, 0.0),
            tauri::LogicalSize::new(1100.0, TOOLBAR_HEIGHT),
        )?;
        let handle = app.clone();
        let root = label.to_owned();
        let popup_profile = profile.to_owned();
        let popup_proxy = proxy.clone();
        let page = WebviewBuilder::new(
            format!("{label}-page"),
            WebviewUrl::External("about:blank".parse()?),
        )
        .data_directory(profile.to_owned())
        .on_page_load(|view, page| {
            if let Some(host) = page.url().host_str() {
                let _ = view.window().set_title(&format!("Huifa — {host}"));
            }
        })
        .on_navigation(|url| website(url) || url.as_str() == "about:blank")
        .on_download(|_, _| false)
        .on_new_window(move |url, features| {
            login_popup(&handle, &root, &popup_profile, &popup_proxy, url, features)
        });
        #[cfg(windows)]
        let page = page.additional_browser_args(&proxy.webview_arguments());
        #[cfg(not(windows))]
        let page = if let Some(url) = &proxy.0 {
            ensure!(
                url.scheme() != "https",
                "Built-in sign-in on this system supports HTTP and SOCKS5 proxies; use local browser sign-in for an HTTPS proxy"
            );
            page.proxy_url(url.clone())
        } else {
            page
        };
        let page = window.add_child(
            page,
            tauri::LogicalPosition::new(0.0, TOOLBAR_HEIGHT),
            tauri::LogicalSize::new(1100.0, 780.0 - TOOLBAR_HEIGHT),
        )?;
        layout_browser(app, label)?;
        let handle = app.clone();
        let root = label.to_owned();
        window.on_window_event(move |event| {
            if matches!(
                event,
                tauri::WindowEvent::Resized(_) | tauri::WindowEvent::ScaleFactorChanged { .. }
            ) {
                let _ = layout_browser(&handle, &root);
            }
        });
        window.show()?;
        Ok(page)
    })();
    if result.is_err() {
        let _ = window.destroy();
    }
    result
}

fn login_popup(
    app: &AppHandle,
    group: &str,
    profile: &Path,
    proxy: &BrowserProxy,
    url: tauri::Url,
    features: tauri::webview::NewWindowFeatures,
) -> tauri::webview::NewWindowResponse<tauri::Wry> {
    use tauri::webview::NewWindowResponse;
    if !(website(&url) || url.as_str() == "about:blank")
        || app.get_window(group).is_none()
        || app
            .windows()
            .keys()
            .filter(|label| label.starts_with(group))
            .count()
            >= 10
    {
        return NewWindowResponse::Deny;
    }
    let label = format!("{group}-{}", uuid::Uuid::new_v4());
    match embedded_window(
        app,
        &label,
        profile.to_owned(),
        group,
        proxy,
        Some(features),
    ) {
        Ok(window) => NewWindowResponse::Create { window },
        Err(_) => NewWindowResponse::Deny,
    }
}

fn embedded_window(
    app: &AppHandle,
    label: &str,
    profile: PathBuf,
    group: &str,
    proxy: &BrowserProxy,
    features: Option<tauri::webview::NewWindowFeatures>,
) -> Result<WebviewWindow> {
    let handle = app.clone();
    let root = group.to_owned();
    let popup_profile = profile.clone();
    let popup_proxy = proxy.clone();
    let mut builder =
        WebviewWindowBuilder::new(app, label, WebviewUrl::External("about:blank".parse()?))
            .title("Huifa — Website sign-in")
            .inner_size(1100.0, 780.0)
            .data_directory(profile)
            .on_page_load(|window, page| {
                if let Some(host) = page.url().host_str() {
                    let _ = window.set_title(&format!("Huifa — {host}"));
                }
            })
            .on_navigation(|url| website(url) || url.as_str() == "about:blank")
            .on_download(|_, _| false)
            .on_new_window(move |url, features| {
                login_popup(&handle, &root, &popup_profile, &popup_proxy, url, features)
            });
    if let Some(features) = features {
        builder = builder.window_features(features);
    }
    #[cfg(windows)]
    {
        builder = builder.additional_browser_args(&proxy.webview_arguments());
    }
    #[cfg(not(windows))]
    if let Some(url) = &proxy.0 {
        ensure!(
            url.scheme() != "https",
            "Built-in sign-in on this system supports HTTP and SOCKS5 proxies; use local browser sign-in for an HTTPS proxy"
        );
        builder = builder.proxy_url(url.clone());
    }
    Ok(builder.build()?)
}

fn find_browser() -> Option<(String, PathBuf)> {
    let mut candidates = Vec::new();
    #[cfg(windows)]
    {
        for (name, suffix) in [
            ("edge", "Microsoft/Edge/Application/msedge.exe"),
            ("chrome", "Google/Chrome/Application/chrome.exe"),
            ("chromium", "Chromium/Application/chrome.exe"),
        ] {
            for base in ["PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"] {
                if let Some(base) = std::env::var_os(base) {
                    candidates.push((name.to_owned(), PathBuf::from(base).join(suffix)));
                }
            }
        }
    }
    #[cfg(target_os = "macos")]
    {
        let mut application_dirs = vec![PathBuf::from("/Applications")];
        if let Some(home) = std::env::var_os("HOME") {
            application_dirs.push(PathBuf::from(home).join("Applications"));
        }
        candidates.extend(macos_browser_candidates(&application_dirs));
    }
    if let Some(paths) = std::env::var_os("PATH") {
        for (name, binary) in [
            ("chrome", "google-chrome"),
            ("chrome", "google-chrome-stable"),
            ("chromium", "chromium"),
            ("chromium", "chromium-browser"),
            ("edge", "microsoft-edge"),
        ] {
            for path in std::env::split_paths(&paths) {
                candidates.push((name.into(), path.join(binary)));
            }
        }
        #[cfg(windows)]
        for (name, binary) in [
            ("edge", "msedge.exe"),
            ("chrome", "chrome.exe"),
            ("chromium", "chromium.exe"),
        ] {
            for path in std::env::split_paths(&paths) {
                candidates.push((name.into(), path.join(binary)));
            }
        }
    }
    candidates.into_iter().find(|(_, path)| path.is_file())
}

#[cfg(any(target_os = "macos", test))]
fn macos_browser_candidates(application_dirs: &[PathBuf]) -> Vec<(String, PathBuf)> {
    application_dirs
        .iter()
        .flat_map(|directory| {
            [
                ("chrome", "Google Chrome.app/Contents/MacOS/Google Chrome"),
                ("edge", "Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
                ("chromium", "Chromium.app/Contents/MacOS/Chromium"),
            ]
            .into_iter()
            .map(move |(name, executable)| (name.into(), directory.join(executable)))
        })
        .collect()
}

async fn launch_system(
    executable: &Path,
    profile: &Path,
    url: &str,
    proxy: &BrowserProxy,
) -> Result<Session> {
    let endpoint_file = profile.join("DevToolsActivePort");
    match tokio::fs::remove_file(&endpoint_file).await {
        Ok(()) => {}
        Err(reason) if reason.kind() == std::io::ErrorKind::NotFound => {}
        Err(reason) => return Err(reason.into()),
    }
    let mut command = Command::new(executable);
    command.args(proxy.arguments());
    command
        .args([
            "--no-first-run",
            "--no-default-browser-check",
            // Save final cookies before stopping the browser after its last window closes.
            "--keep-alive-for-test",
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=0",
        ])
        .arg(format!("--user-data-dir={}", profile.display()))
        .arg("--new-window")
        .arg(url)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .kill_on_drop(true);
    #[cfg(windows)]
    command.creation_flags(0x08000000);
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.as_std_mut().process_group(0);
    }
    let mut child = command
        .spawn()
        .context("Cannot start compatible sign-in browser")?;
    let guard = ProcessTree::attach(child.id().context("Browser has no process ID")?)?;
    let startup = async {
        loop {
            ensure!(
                child.try_wait()?.is_none(),
                "Browser exited during startup; close any other application using its login profile"
            );
            if let Ok(text) = tokio::fs::read_to_string(&endpoint_file).await {
                let mut lines = text.lines();
                if let (Some(port), Some(path)) = (lines.next(), lines.next()) {
                    let port: u16 = port.parse()?;
                    ensure!(
                        port != 0
                            && path.starts_with("/devtools/browser/")
                            && !path.contains(['\r', '\n', '?', '#']),
                        "Invalid browser endpoint"
                    );
                    return Cdp::connect(&format!("ws://127.0.0.1:{port}{path}")).await;
                }
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    };
    match tokio::time::timeout(Duration::from_secs(30), startup).await {
        Ok(Ok(cdp)) => Ok(Session::System {
            child: Box::new(child),
            guard,
            cdp,
            url: url.into(),
            target_id: Default::default(),
            proxy: proxy.clone(),
        }),
        result => {
            guard.terminate();
            let _ = child.kill().await;
            let _ = child.wait().await;
            match result {
                Ok(Err(reason)) => Err(reason),
                _ => bail!("Compatible sign-in browser startup timed out"),
            }
        }
    }
}

#[cfg(windows)]
async fn native_cookies(window: &Webview) -> Result<Value> {
    use std::sync::{Arc, Mutex};
    use webview2_com::{
        GetCookiesCompletedHandler, Microsoft::Web::WebView2::Win32::ICoreWebView2_2,
    };
    use windows::core::{Interface, PCWSTR};
    let (sender, receiver) = tokio::sync::oneshot::channel();
    let reply = Arc::new(Mutex::new(Some(sender)));
    window.with_webview(move |webview| {
        let callback_reply = reply.clone();
        let handler = GetCookiesCompletedHandler::create(Box::new(move |code, cookies| {
            let result = code
                .map_err(anyhow::Error::from)
                .and_then(|()| windows_cookie_values(cookies));
            if let Some(sender) = callback_reply.lock().unwrap().take() {
                let _ = sender.send(result);
            }
            Ok(())
        }));
        let result = (|| -> Result<()> {
            unsafe {
                webview
                    .controller()
                    .CoreWebView2()?
                    .cast::<ICoreWebView2_2>()?
                    .CookieManager()?
                    .GetCookies(PCWSTR::null(), &handler)?;
            }
            Ok(())
        })();
        if let Err(reason) = result
            && let Some(sender) = reply.lock().unwrap().take()
        {
            let _ = sender.send(Err(reason));
        }
    })?;
    tokio::time::timeout(Duration::from_secs(10), receiver)
        .await
        .context("Reading browser cookies timed out")?
        .context("Login window closed while reading cookies")?
}

#[cfg(windows)]
fn windows_cookie_values(
    cookies: Option<webview2_com::Microsoft::Web::WebView2::Win32::ICoreWebView2CookieList>,
) -> Result<Value> {
    use windows::core::{BOOL, PWSTR};
    let cookies = cookies.context("Browser returned no cookie store")?;
    let mut count = 0;
    unsafe {
        cookies.Count(&mut count)?;
    }
    ensure!(count <= 10000, "Too many cookies");
    let mut output = Vec::new();
    for index in 0..count {
        unsafe {
            let cookie = cookies.GetValueAtIndex(index)?;
            let mut domain = PWSTR::null();
            cookie.Domain(&mut domain)?;
            let domain = webview2_com::take_pwstr(domain);
            let mut path = PWSTR::null();
            cookie.Path(&mut path)?;
            let path = webview2_com::take_pwstr(path);
            let mut name = PWSTR::null();
            cookie.Name(&mut name)?;
            let name = webview2_com::take_pwstr(name);
            let mut value = PWSTR::null();
            cookie.Value(&mut value)?;
            let value = webview2_com::take_pwstr(value);
            let mut http_only = BOOL(0);
            cookie.IsHttpOnly(&mut http_only)?;
            let mut secure = BOOL(0);
            cookie.IsSecure(&mut secure)?;
            let mut session = BOOL(0);
            cookie.IsSession(&mut session)?;
            let mut expires = 0.0;
            cookie.Expires(&mut expires)?;
            output.push(json!({"domain":domain,"path":path,"name":name,"value":value,"httpOnly":http_only.as_bool(),"secure":secure.as_bool(),"session":session.as_bool(),"expires":expires}));
        }
    }
    Ok(json!(output))
}

#[cfg(target_os = "linux")]
async fn native_cookies(window: &Webview) -> Result<Value> {
    use webkit2gtk::{WebViewExt, WebsiteDataManagerExt};
    let (sender, receiver) = tokio::sync::oneshot::channel();
    window.with_webview(move |webview| {
        let manager = webview
            .inner()
            .website_data_manager()
            .and_then(|manager| manager.cookie_manager());
        if let Some(manager) = manager {
            all_cookie_values(&manager, sender);
        } else {
            let _ = sender.send(Err(anyhow::anyhow!("Browser cookie store is unavailable")));
        }
    })?;
    tokio::time::timeout(Duration::from_secs(10), receiver)
        .await
        .context("Reading browser cookies timed out")?
        .context("Login window closed while reading cookies")?
}

#[cfg(target_os = "linux")]
fn all_cookie_values(
    manager: &webkit2gtk::CookieManager,
    sender: tokio::sync::oneshot::Sender<Result<Value>>,
) {
    use webkit2gtk::{
        gio,
        glib::{
            self,
            translate::{FromGlibPtrContainer, ToGlibPtr},
        },
    };
    type Reply = tokio::sync::oneshot::Sender<Result<Value>>;
    // shortcut: webkit2gtk 2.0.2 omits this binding; replace the FFI when the crate exposes get_all_cookies.
    unsafe extern "C" {
        fn webkit_cookie_manager_get_all_cookies(
            manager: *mut webkit2gtk::ffi::WebKitCookieManager,
            cancellable: *mut gio::ffi::GCancellable,
            callback: gio::ffi::GAsyncReadyCallback,
            data: glib::ffi::gpointer,
        );
        fn webkit_cookie_manager_get_all_cookies_finish(
            manager: *mut webkit2gtk::ffi::WebKitCookieManager,
            result: *mut gio::ffi::GAsyncResult,
            error: *mut *mut glib::ffi::GError,
        ) -> *mut glib::ffi::GList;
    }
    unsafe extern "C" fn received(
        source: *mut glib::gobject_ffi::GObject,
        result: *mut gio::ffi::GAsyncResult,
        data: glib::ffi::gpointer,
    ) {
        let sender = unsafe { Box::from_raw(data as *mut Reply) };
        let mut error = std::ptr::null_mut();
        let list = unsafe {
            webkit_cookie_manager_get_all_cookies_finish(source as *mut _, result, &mut error)
        };
        let result = if error.is_null() {
            let cookies: Vec<soup::Cookie> = unsafe { FromGlibPtrContainer::from_glib_full(list) };
            Ok(json!(cookies.into_iter().map(|mut cookie| {
                json!({"domain":cookie.domain().map(|s|s.to_string()),"path":cookie.path().map(|s|s.to_string()),"name":cookie.name().map(|s|s.to_string()),"value":cookie.value().map(|s|s.to_string()),"httpOnly":cookie.is_http_only(),"secure":cookie.is_secure(),"expires":cookie.expires().map(|time|time.to_unix()).unwrap_or(0)})
            }).collect::<Vec<_>>()))
        } else {
            let error: glib::Error = unsafe { glib::translate::from_glib_full(error) };
            Err(error.into())
        };
        let _ = sender.send(result);
    }
    unsafe {
        webkit_cookie_manager_get_all_cookies(
            manager.to_glib_none().0,
            std::ptr::null_mut(),
            Some(received),
            Box::into_raw(Box::new(sender)) as *mut _,
        );
    }
}

#[cfg(target_os = "linux")]
async fn initialize_storage(window: &Webview, profile: &Path, proxy: &BrowserProxy) -> Result<()> {
    use webkit2gtk::{CookieManagerExt, WebViewExt, WebsiteDataManagerExt};
    let path = profile.join("cookies.sqlite");
    let direct = proxy.0.is_none();
    let (sender, receiver) = tokio::sync::oneshot::channel();
    window.with_webview(move |webview| {
        let result = webview
            .inner()
            .website_data_manager()
            .context("Browser cookie store is unavailable")
            .and_then(|manager| {
                // Linux keeps this profile's context alive after closing its last window.
                if direct {
                    manager.set_network_proxy_settings(webkit2gtk::NetworkProxyMode::NoProxy, None);
                }
                let cookies = manager
                    .cookie_manager()
                    .context("Browser cookie store is unavailable")?;
                cookies.set_persistent_storage(
                    &path.to_string_lossy(),
                    webkit2gtk::CookiePersistentStorage::Sqlite,
                );
                Ok(())
            });
        let _ = sender.send(result);
    })?;
    tokio::time::timeout(Duration::from_secs(10), receiver)
        .await
        .context("Browser storage setup timed out")?
        .context("Login window closed during setup")?
}

#[cfg(not(target_os = "linux"))]
async fn initialize_storage(_: &Webview, _: &Path, _: &BrowserProxy) -> Result<()> {
    Ok(())
}

#[cfg(not(any(windows, target_os = "linux")))]
async fn native_cookies(_: &Webview) -> Result<Value> {
    bail!("Use compatible sign-in on this platform")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn login_modes_match_platform_support_and_normalize_old_preferences() -> Result<()> {
        assert!(validate_login_mode("system").is_ok());
        assert_eq!(
            validate_login_mode("embedded").is_ok(),
            embedded_available()
        );
        assert!(validate_login_mode("safari").is_err());
        let saved = Preferences::new("https://www.youtube.com/", "embedded")?;
        assert_eq!(saved.mode, default_login_mode());
        let directory = tempfile::tempdir()?;
        std::fs::write(
            directory.path().join("preferences.json"),
            br#"{"url":"https://www.youtube.com/","mode":"embedded"}"#,
        )?;
        assert_eq!(Preferences::read(directory.path())?, saved);
        let browser = LoginBrowser::new(directory.path().into())?;
        assert_eq!(
            browser.status(&None)["embeddedAvailable"],
            embedded_available()
        );
        Ok(())
    }

    #[test]
    fn finds_chromium_browsers_in_system_and_user_application_bundles() -> Result<()> {
        let directory = tempfile::tempdir()?;
        let application_dirs = [
            directory.path().join("Applications"),
            directory.path().join("Users/example/Applications"),
        ];
        let browser = application_dirs[1].join("Google Chrome.app/Contents/MacOS/Google Chrome");
        std::fs::create_dir_all(browser.parent().unwrap())?;
        std::fs::write(&browser, b"fixture")?;
        let candidates = macos_browser_candidates(&application_dirs);
        assert!(
            !candidates
                .iter()
                .any(|(_, path)| path.to_string_lossy().contains("Safari"))
        );
        assert_eq!(
            candidates.into_iter().find(|(_, path)| path.is_file()),
            Some(("chrome".into(), browser))
        );
        Ok(())
    }

    #[test]
    fn browser_proxy_uses_explicit_proxy_or_direct_access() -> Result<()> {
        assert!(
            BrowserProxy::parse("")?
                .arguments()
                .contains(&"--no-proxy-server".into())
        );
        for (value, expected) in [
            ("http://127.0.0.1:7890", "http://127.0.0.1:7890"),
            ("http://proxy.example.test", "http://proxy.example.test:80"),
            (
                "https://proxy.example.test",
                "https://proxy.example.test:443",
            ),
            ("socks5h://[::1]:1081", "socks5://[::1]:1081"),
        ] {
            let arguments = BrowserProxy::parse(value)?.arguments();
            assert_eq!(arguments[0], format!("--proxy-server={expected}"));
            assert!(!arguments.contains(&"--no-proxy-server".into()));
        }
        for invalid in [
            "file:///tmp/proxy",
            "http://proxy:0",
            "http://proxy/path",
            "http://proxy/?query",
            "http://user:password@proxy:8080",
        ] {
            assert!(BrowserProxy::parse(invalid).is_err(), "{invalid}");
        }
        Ok(())
    }

    #[test]
    fn preferences_persist_across_reopening_and_leave_profile_data_alone() -> Result<()> {
        let directory = tempfile::tempdir()?;
        let browser = LoginBrowser::new(directory.path().into())?;
        assert_eq!(
            browser.status(&None)["preferredUrl"],
            "https://www.youtube.com/"
        );
        assert_eq!(browser.status(&None)["preferredMode"], default_login_mode());
        let profile = directory.path().join("embedded");
        private_directory(&profile)?;
        std::fs::write(profile.join("login-data"), b"keep")?;
        browser.remember("https://www.douyin.com/video/123?from=web", "system")?;
        browser.remember("https://www.bilibili.com/video/BV123/", "embedded")?;
        drop(browser);
        let reopened = LoginBrowser::new(directory.path().into())?;
        assert_eq!(
            reopened.status(&None)["preferredUrl"],
            "https://www.bilibili.com/video/BV123/"
        );
        assert_eq!(
            reopened.status(&None)["preferredMode"],
            default_login_mode()
        );
        assert_eq!(std::fs::read(profile.join("login-data"))?, b"keep");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(
                std::fs::metadata(directory.path().join("preferences.json"))?
                    .permissions()
                    .mode()
                    & 0o777,
                0o600
            );
        }
        Ok(())
    }

    #[test]
    fn invalid_preferences_fall_back_and_cannot_replace_the_last_website() -> Result<()> {
        let directory = tempfile::tempdir()?;
        let browser = LoginBrowser::new(directory.path().into())?;
        browser.remember("https://www.douyin.com/", "system")?;
        let expected = browser.status(&None);
        for (url, mode) in [
            ("about:blank", "embedded"),
            ("file:///secret", "embedded"),
            ("javascript:alert(1)", "embedded"),
            ("https://user:password@example.com/", "embedded"),
            ("https://example.com/", "invalid"),
        ] {
            assert!(browser.remember(url, mode).is_err());
            assert_eq!(browser.status(&None), expected);
            std::fs::write(
                directory.path().join("preferences.json"),
                serde_json::to_vec(&json!({"url":url,"mode":mode}))?,
            )?;
            assert_eq!(Preferences::load(directory.path()), Preferences::default());
        }
        for text in ["{broken", "[]", "null", &"x".repeat(4097)] {
            std::fs::write(directory.path().join("preferences.json"), text)?;
            assert_eq!(Preferences::load(directory.path()), Preferences::default());
        }
        let reopened = LoginBrowser::new(directory.path().into())?;
        assert_eq!(
            reopened.status(&None)["preferredUrl"],
            "https://www.youtube.com/"
        );
        reopened.remember("https://www.youtube.com/", "embedded")?;
        assert_eq!(Preferences::read(directory.path())?, Preferences::default());
        Ok(())
    }

    #[test]
    fn unavailable_preference_storage_keeps_the_current_website_in_memory() -> Result<()> {
        let directory = tempfile::tempdir()?;
        std::fs::create_dir(directory.path().join("preferences.json"))?;
        let browser = LoginBrowser::new(directory.path().into())?;
        assert!(
            browser
                .remember("https://www.douyin.com/", "system")
                .is_err()
        );
        assert_eq!(
            browser.status(&None)["preferredUrl"],
            "https://www.douyin.com/"
        );
        assert_eq!(browser.status(&None)["preferredMode"], "system");
        assert!(browser.status(&None)["preferenceError"].is_string());
        assert!(browser.status(&None)["saveError"].is_null());
        std::fs::remove_dir(directory.path().join("preferences.json"))?;
        std::fs::write(directory.path().join("preferences.json"), b"corrupt")?;
        browser.remember("https://www.douyin.com/", "system")?;
        assert!(browser.status(&None)["preferenceError"].is_null());
        assert_eq!(
            Preferences::load(directory.path()).url,
            "https://www.douyin.com/"
        );
        Ok(())
    }

    #[test]
    fn validates_login_urls_and_preserves_profile_data() -> Result<()> {
        for url in [
            "file:///secret",
            "javascript:alert(1)",
            "https://user:password@example.com",
            "data:text/html,test",
        ] {
            assert!(login_url(url).is_err());
        }
        assert!(login_url("https://www.youtube.com/").is_ok());
        let directory = tempfile::tempdir()?;
        let profile = directory.path().join("embedded");
        private_directory(&profile)?;
        std::fs::write(profile.join("existing-login"), b"keep")?;
        let _browser = LoginBrowser::new(directory.path().into())?;
        assert_eq!(std::fs::read(profile.join("existing-login"))?, b"keep");
        Ok(())
    }
}

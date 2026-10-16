use anyhow::{Context, Result, bail, ensure};
use huifa_core::{
    App,
    browser::{Cdp, netscape},
    process::ProcessTree,
};
use serde_json::{Value, json};
use std::{
    path::{Path, PathBuf},
    process::Stdio,
    time::Duration,
};
use tauri::{AppHandle, Manager, WebviewUrl, WebviewWindow, WebviewWindowBuilder};
use tokio::{
    process::{Child, Command},
    sync::Mutex,
};

#[cfg(all(test, windows, feature = "login-smoke"))]
mod smoke;

pub struct LoginBrowser {
    directory: PathBuf,
    system: Option<(String, PathBuf)>,
    running: Mutex<Option<Session>>,
    synced: std::sync::Mutex<SyncState>,
}

#[derive(Default)]
struct SyncState {
    saved: Option<Value>,
    error: Option<String>,
    name: Option<String>,
}

enum Session {
    Embedded {
        label: String,
        url: String,
    },
    System {
        child: Box<Child>,
        guard: ProcessTree,
        cdp: Cdp,
        url: String,
    },
}

impl LoginBrowser {
    pub fn new(directory: PathBuf) -> Result<Self> {
        private_directory(&directory)?;
        Ok(Self {
            directory,
            system: find_browser(),
            running: Mutex::new(None),
            synced: Default::default(),
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
                ensure!(matches!(mode, "embedded" | "system"), "Invalid login mode");
                let name = args
                    .get("name")
                    .and_then(Value::as_str)
                    .map(str::trim)
                    .filter(|name| !name.is_empty());
                ensure!(name.is_none_or(|name| name.len() <= 128 && !name.chars().any(char::is_control)), "Invalid cookie name");
                if let Some(session) = running.as_ref() {
                    ensure!(
                        session.mode() == mode && session.url() == url.as_str(),
                        "Close the active login browser before opening another website or mode"
                    );
                    if let Session::Embedded { label, .. } = session
                        && let Some(window) = app.get_webview_window(label)
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
                    let window = embedded_window(app, &label, profile.clone(), &label, None)?;
                    if let Err(reason) = initialize_storage(&window, &profile)
                        .await
                        .and_then(|()| window.navigate(url.clone()).map_err(Into::into))
                    {
                        close_windows(app, &label);
                        return Err(reason);
                    }
                    Session::Embedded {
                        label,
                        url: url.to_string(),
                    }
                } else {
                    let (name, executable) = self
                        .system
                        .as_ref()
                        .context("Install Edge, Chrome or Chromium to use compatible sign-in")?;
                    let profile = self.directory.join(name);
                    private_directory(&profile)?;
                    launch_system(executable, &profile, url.as_str()).await?
                };
                *running = Some(session);
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
                if let Some(session) = running.as_ref() {
                    self.sync_session(app, core, session, None).await?;
                }
                if let Some(session) = running.take() {
                    session.stop(app).await;
                }
                Ok(json!(true))
            }
            _ => bail!("Unknown login browser command"),
        }
    }

    fn status(&self, running: &Option<Session>) -> Value {
        let synced = self.synced.lock().unwrap();
        json!({"active":running.is_some(),"mode":running.as_ref().map(Session::mode),"targetUrl":running.as_ref().map(Session::url),"systemBrowser":self.system.as_ref().map(|(name,_)|name),"cookie":synced.saved.as_ref().map(|value| &value["cookie"]),"saveError":synced.error})
    }

    async fn sync_session(
        &self,
        app: &AppHandle,
        core: &App,
        session: &Session,
        name: Option<&str>,
    ) -> Result<()> {
        let result = async {
            let cookies = match session {
                Session::Embedded { label, .. } => {
                    native_cookies(
                        &app.get_webview_window(label)
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
            self.sync_session(app, core, running.as_ref().unwrap(), None)
                .await?;
            if let Some(session) = running.take() {
                session.stop(app).await;
            }
        } else if let Some(window) = app.get_webview_window(label) {
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
            Self::Embedded { label, .. } => app.get_webview_window(label).is_some(),
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
    for (label, window) in app.webview_windows() {
        if label == group || label.starts_with(&format!("{group}-")) {
            let _ = window.destroy();
        }
    }
}

fn embedded_window(
    app: &AppHandle,
    label: &str,
    profile: PathBuf,
    group: &str,
    features: Option<tauri::webview::NewWindowFeatures>,
) -> Result<WebviewWindow> {
    let handle = app.clone();
    let root = group.to_owned();
    let popup_profile = profile.clone();
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
                use tauri::webview::NewWindowResponse;
                if !(website(&url) || url.as_str() == "about:blank")
                    || handle.get_webview_window(&root).is_none()
                    || handle
                        .webview_windows()
                        .keys()
                        .filter(|label| label.starts_with(&root))
                        .count()
                        >= 10
                {
                    return NewWindowResponse::Deny;
                }
                let label = format!("{root}-{}", uuid::Uuid::new_v4());
                match embedded_window(
                    &handle,
                    &label,
                    popup_profile.clone(),
                    &root,
                    Some(features),
                ) {
                    Ok(window) => NewWindowResponse::Create { window },
                    Err(_) => NewWindowResponse::Deny,
                }
            });
    if let Some(features) = features {
        builder = builder.window_features(features);
    }
    #[cfg(all(test, windows, feature = "login-smoke"))]
    {
        builder = builder.additional_browser_args("--host-resolver-rules=\"MAP *.huifa.test 127.0.0.1\" --proxy-bypass-list=\"*.huifa.test\"");
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

async fn launch_system(executable: &Path, profile: &Path, url: &str) -> Result<Session> {
    let endpoint_file = profile.join("DevToolsActivePort");
    match tokio::fs::remove_file(&endpoint_file).await {
        Ok(()) => {}
        Err(reason) if reason.kind() == std::io::ErrorKind::NotFound => {}
        Err(reason) => return Err(reason.into()),
    }
    let mut command = Command::new(executable);
    #[cfg(all(test, windows, feature = "login-smoke"))]
    command.args([
        "--host-resolver-rules=MAP *.huifa.test 127.0.0.1",
        "--proxy-bypass-list=*.huifa.test",
    ]);
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
async fn native_cookies(window: &WebviewWindow) -> Result<Value> {
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
async fn native_cookies(window: &WebviewWindow) -> Result<Value> {
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
async fn initialize_storage(window: &WebviewWindow, profile: &Path) -> Result<()> {
    use webkit2gtk::{CookieManagerExt, WebViewExt, WebsiteDataManagerExt};
    let path = profile.join("cookies.sqlite");
    let (sender, receiver) = tokio::sync::oneshot::channel();
    window.with_webview(move |webview| {
        let result = webview
            .inner()
            .website_data_manager()
            .and_then(|manager| manager.cookie_manager())
            .context("Browser cookie store is unavailable")
            .map(|manager| {
                manager.set_persistent_storage(
                    &path.to_string_lossy(),
                    webkit2gtk::CookiePersistentStorage::Sqlite,
                );
            });
        let _ = sender.send(result);
    })?;
    tokio::time::timeout(Duration::from_secs(10), receiver)
        .await
        .context("Browser storage setup timed out")?
        .context("Login window closed during setup")?
}

#[cfg(not(target_os = "linux"))]
async fn initialize_storage(_: &WebviewWindow, _: &Path) -> Result<()> {
    Ok(())
}

#[cfg(not(any(windows, target_os = "linux")))]
async fn native_cookies(_: &WebviewWindow) -> Result<Value> {
    bail!("Use compatible sign-in on this platform")
}

#[cfg(test)]
mod tests {
    use super::*;
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

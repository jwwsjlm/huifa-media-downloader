use super::*;
use std::{
    io::{Read, Write},
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicUsize, Ordering},
    },
};

static INVOCATIONS: AtomicUsize = AtomicUsize::new(0);

#[tauri::command]
fn upload_asset(probe: bool) -> bool {
    INVOCATIONS.fetch_add(1, Ordering::SeqCst);
    probe
}

struct Fixture {
    port: u16,
    stopped: Arc<AtomicBool>,
    denied: Arc<AtomicBool>,
    main_ready: Arc<AtomicBool>,
    toolbar_ready: Arc<AtomicBool>,
    proxy_embedded: Arc<AtomicBool>,
    proxy_system: Arc<AtomicBool>,
    thread: Option<std::thread::JoinHandle<()>>,
}

impl Fixture {
    fn start() -> Result<Self> {
        let listener = std::net::TcpListener::bind("127.0.0.1:0")?;
        let port = listener.local_addr()?.port();
        listener.set_nonblocking(true)?;
        let stopped = Arc::new(AtomicBool::new(false));
        let denied = Arc::new(AtomicBool::new(false));
        let main_ready = Arc::new(AtomicBool::new(false));
        let toolbar_ready = Arc::new(AtomicBool::new(false));
        let proxy_embedded = Arc::new(AtomicBool::new(false));
        let proxy_system = Arc::new(AtomicBool::new(false));
        let flags = (
            stopped.clone(),
            denied.clone(),
            main_ready.clone(),
            toolbar_ready.clone(),
        );
        let proxy_flags = (proxy_embedded.clone(), proxy_system.clone());
        let thread = std::thread::spawn(move || {
            while !flags.0.load(Ordering::SeqCst) {
                let Ok((mut stream, _)) = listener.accept() else {
                    std::thread::sleep(Duration::from_millis(10));
                    continue;
                };
                let _ = stream.set_read_timeout(Some(Duration::from_secs(1)));
                let mut buffer = [0; 16384];
                let count = stream.read(&mut buffer).unwrap_or(0);
                let request = String::from_utf8_lossy(&buffer[..count]);
                let target = request
                    .lines()
                    .next()
                    .and_then(|line| line.split_whitespace().nth(1))
                    .unwrap_or("/");
                let proxy_url = tauri::Url::parse(target).ok().filter(|url| {
                    url.scheme() == "http" && url.host_str() == Some("proxy-only.invalid")
                });
                let path = proxy_url.as_ref().map_or(target, tauri::Url::path);
                if proxy_url.is_some() {
                    match path {
                        "/proxy-embedded" => proxy_flags.0.store(true, Ordering::SeqCst),
                        "/proxy-system" => proxy_flags.1.store(true, Ordering::SeqCst),
                        _ => {}
                    }
                }
                let asset_path = path.split('?').next().unwrap_or("");
                if (asset_path == "/index.html" || asset_path.starts_with("/assets/"))
                    && !asset_path.contains("..")
                {
                    let file = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
                        .join("../frontend/dist")
                        .join(asset_path.trim_start_matches('/'));
                    let body = std::fs::read(file).unwrap_or_default();
                    let mime = if asset_path.ends_with(".js") {
                        "text/javascript"
                    } else if asset_path.ends_with(".css") {
                        "text/css"
                    } else {
                        "text/html"
                    };
                    let header = format!(
                        "HTTP/1.1 200 OK\r\nContent-Type: {mime}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                        body.len()
                    );
                    let _ = stream.write_all(header.as_bytes());
                    let _ = stream.write_all(&body);
                    continue;
                }
                if path == "/denied" {
                    flags.1.store(true, Ordering::SeqCst);
                }
                if path == "/main-ready" {
                    flags.2.store(true, Ordering::SeqCst);
                }
                if path == "/toolbar-ready" {
                    flags.3.store(true, Ordering::SeqCst);
                }
                let cookies = match path {
                    "/login" => {
                        "Set-Cookie: host_sid=host-secret; HttpOnly; Path=/; Max-Age=3600\r\nSet-Cookie: domain_sid=domain-secret; Domain=huifa.test; Path=/; Max-Age=3600\r\n"
                    }
                    "/popup" => {
                        "Set-Cookie: popup_sid=popup-secret; HttpOnly; Path=/; Max-Age=3600\r\n"
                    }
                    "/proxy-embedded" => {
                        "Set-Cookie: proxy_embedded=proxy-secret; HttpOnly; Path=/; Max-Age=3600\r\n"
                    }
                    "/proxy-system" => {
                        "Set-Cookie: proxy_system=proxy-secret; HttpOnly; Path=/; Max-Age=3600\r\n"
                    }
                    "/direct-embedded" => {
                        "Set-Cookie: direct_embedded=direct-secret; HttpOnly; Path=/; Max-Age=3600\r\n"
                    }
                    "/direct-system" => {
                        "Set-Cookie: direct_system=direct-secret; HttpOnly; Path=/; Max-Age=3600\r\n"
                    }
                    _ => "",
                };
                let body = if path == "/" || path == "/login" {
                    "<html><body>Huifa isolated login test<script>function probe(){if(!window.__TAURI_INTERNALS__){setTimeout(probe,50);return}window.__TAURI_INTERNALS__.invoke('upload_asset',{probe:true}).then(()=>fetch('/main-ready'),()=>fetch('/denied'))}probe()</script></body></html>".to_owned()
                } else {
                    "<html><body>Huifa isolated login test</body></html>".into()
                };
                let response = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: {}\r\nConnection: close\r\n{cookies}\r\n{body}",
                    body.len()
                );
                let _ = stream.write_all(response.as_bytes());
            }
        });
        Ok(Self {
            port,
            stopped,
            denied,
            main_ready,
            toolbar_ready,
            proxy_embedded,
            proxy_system,
            thread: Some(thread),
        })
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        self.stopped.store(true, Ordering::SeqCst);
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

async fn wait_cookie(browser: &LoginBrowser, app: &AppHandle, name: &str) -> Result<Value> {
    tokio::time::timeout(Duration::from_secs(20), async {
        loop {
            let running = browser.running.lock().await;
            let cookies = match running.as_ref().context("Missing login session")? {
                Session::Embedded { label, .. } => {
                    native_cookies(
                        &app.get_webview(&format!("{label}-page"))
                            .context("Missing login window")?,
                    )
                    .await?
                }
                Session::System { cdp, .. } => {
                    cdp.request("Storage.getCookies", json!({}), None).await?["cookies"].clone()
                }
            };
            if cookies
                .as_array()
                .is_some_and(|cookies| cookies.iter().any(|cookie| cookie["name"] == name))
            {
                return Ok(cookies);
            }
            drop(running);
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    })
    .await
    .with_context(|| format!("Test cookie {name} was not received"))?
}

async fn wait_navigation(toolbar: &Webview, path: &str) -> Result<()> {
    tokio::time::timeout(Duration::from_secs(10), async {
        loop {
            let state = navigation::command(toolbar.clone(), "state", None).await?;
            if state["url"].as_str().is_some_and(|url| url.ends_with(path)) {
                return Ok::<_, anyhow::Error>(());
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    })
    .await
    .context("Browser navigation did not reach the requested page")?
}

#[test]
#[ignore = "opens isolated WebView2 and Edge/Chrome windows; run with --ignored --test-threads=1"]
fn real_login_cookie_export_persistence_popups_and_ipc_isolation() -> Result<()> {
    INVOCATIONS.store(0, Ordering::SeqCst);
    let fixture = Fixture::start()?;
    let directory = tempfile::tempdir()?;
    let browser = Arc::new(LoginBrowser::new(directory.path().join("login"))?);
    ensure!(
        browser.system.is_some(),
        "Install Edge or Chrome before running the native test"
    );
    let core = tauri::async_runtime::block_on(App::open(huifa_core::AppConfig {
        data_dir: directory.path().join("data"),
        download_dir: directory.path().join("downloads"),
        tool_dir: None,
    }))?;
    let outcome = Arc::new(std::sync::Mutex::new(None));
    let report = outcome.clone();
    let observer = browser.clone();
    let observed_core = core.clone();
    let mut context = tauri::generate_context!();
    context.config_mut().app.windows.clear();
    context.config_mut().build.dev_url =
        Some(format!("http://127.0.0.1:{}/", fixture.port).parse()?);
    let main_profile = directory.path().join("main");
    let test_directory = directory.path().to_owned();
    let application = tauri::Builder::default()
        .any_thread()
        .invoke_handler(tauri::generate_handler![upload_asset, crate::login_navigation])
        .on_window_event(move |window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event
                && window.label().starts_with("login-")
            {
                api.prevent_close();
                let handle = window.app_handle().clone();
                let label = window.label().to_owned();
                let browser = observer.clone();
                let core = observed_core.clone();
                tauri::async_runtime::spawn(async move {
                    let _ = browser.close_window(&handle, &core, &label).await;
                });
            }
            if matches!(event, tauri::WindowEvent::Destroyed) {
                let handle = window.app_handle().clone();
                let label = window.label().to_owned();
                let browser = observer.clone();
                tauri::async_runtime::spawn(async move {
                    browser.window_closed(&handle, &label).await;
                });
            }
        })
        .setup(move |app| {
            WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                .data_directory(main_profile.clone())
                .visible(false)
                .build()?;
            let handle = app.handle().clone();
            let browser = browser.clone();
            let core = core.clone();
            tauri::async_runtime::spawn(async move {
                let result = tokio::time::timeout(Duration::from_secs(150), async {
                    let url = format!("http://login.huifa.test:{}/login", fixture.port);
                    tokio::time::timeout(Duration::from_secs(15), async {
                        while !fixture.main_ready.load(Ordering::SeqCst) {
                            tokio::time::sleep(Duration::from_millis(50)).await;
                        }
                    })
                    .await
                    .context("Main-window IPC did not work")?;
                    browser
                        .command(
                            &handle,
                            &core,
                            "browser.login.open",
                            &json!({"url":url,"mode":"embedded"}),
                        )
                        .await?;
                    let cookies = wait_cookie(&browser, &handle, "domain_sid").await?;
                    let text = netscape(&cookies)?;
                    ensure!(
                        text.contains("#HttpOnly_login.huifa.test\tFALSE\t/\tFALSE"),
                        "Host-only cookie scope changed"
                    );
                    ensure!(
                        text.contains(".huifa.test\tTRUE\t/\tFALSE"),
                        "Domain cookie scope changed"
                    );
                    tokio::time::timeout(Duration::from_secs(10), async {
                        while !fixture.denied.load(Ordering::SeqCst) {
                            tokio::time::sleep(Duration::from_millis(50)).await;
                        }
                    })
                    .await
                    .context("Login-window IPC was not rejected")?;
                    ensure!(
                        INVOCATIONS.load(Ordering::SeqCst) == 1,
                        "An untrusted page reached an application command"
                    );
                    ensure!(
                        browser
                            .command(&handle, &core, "browser.login.save", &json!({"name":""}))
                            .await
                            .is_err()
                    );
                    let label = match browser.running.lock().await.as_ref().unwrap() {
                        Session::Embedded { label, .. } => label.clone(),
                        _ => unreachable!(),
                    };
                    let toolbar = handle.get_webview(&format!("{label}-toolbar")).context("Missing navigation toolbar")?;
                    let page = handle.get_webview(&format!("{label}-page")).unwrap();
                    toolbar.eval("function checkToolbar(){const nav=document.querySelector('.browser-toolbar');if(!nav||nav.querySelectorAll('button').length!==3||Math.abs(nav.getBoundingClientRect().height-44)>1){setTimeout(checkToolbar,50);return}window.__TAURI_INTERNALS__.invoke('login_navigation',{action:'state'}).then(()=>fetch('/toolbar-ready'))}checkToolbar()")?;
                    tokio::time::timeout(Duration::from_secs(10), async {
                        while !fixture.toolbar_ready.load(Ordering::SeqCst) { tokio::time::sleep(Duration::from_millis(20)).await; }
                    }).await.context("The toolbar could not use its dedicated navigation command")?;
                    ensure!(navigation::command(page.clone(), "state", None).await.is_err(), "The website received navigation access");
                    let second = format!("http://login.huifa.test:{}/second", fixture.port);
                    navigation::command(toolbar.clone(), "navigate", Some(&second)).await?;
                    wait_navigation(&toolbar, "/second").await?;
                    ensure!(navigation::command(toolbar.clone(), "state", None).await?["canGoBack"] == true, "Back history unavailable after navigation");
                    navigation::command(toolbar.clone(), "back", None).await?;
                    wait_navigation(&toolbar, "/login").await?;
                    ensure!(navigation::command(toolbar.clone(), "state", None).await?["canGoForward"] == true, "Forward history unavailable after going back");
                    navigation::command(toolbar.clone(), "forward", None).await?;
                    wait_navigation(&toolbar, "/second").await?;
                    navigation::command(toolbar.clone(), "reload", None).await?;
                    wait_navigation(&toolbar, "/second").await?;
                    let status = browser.command(&handle, &core, "browser.login.status", &json!({})).await?;
                    ensure!(status["preferredUrl"] == second && status["preferredMode"] == "embedded", "Navigation did not update saved browser preferences");
                    let reopened = LoginBrowser::new(browser.directory.clone())?;
                    ensure!(reopened.status(&None)["preferredUrl"] == second && reopened.status(&None)["preferenceError"].is_null(), "Browser preferences did not survive reopening");
                    let parent = handle.get_window(&label).unwrap();
                    parent.set_size(tauri::LogicalSize::new(760.0, 600.0))?;
                    tokio::time::sleep(Duration::from_millis(100)).await;
                    let scale = parent.scale_factor()?;
                    let toolbar_bounds = toolbar.bounds()?;
                    let page_bounds = page.bounds()?;
                    ensure!((toolbar_bounds.size.to_logical::<f64>(scale).height - TOOLBAR_HEIGHT).abs() < 1.0, "Resizing changed the toolbar height");
                    ensure!((page_bounds.position.to_logical::<f64>(scale).y - TOOLBAR_HEIGHT).abs() < 1.0, "The toolbar overlapped the website after resizing");
                    handle
                        .get_webview(&format!("{label}-page"))
                        .unwrap()
                        .eval("window.open('/popup')")?;
                    wait_cookie(&browser, &handle, "popup_sid")
                        .await
                        .context("Login popup did not share the cookie store")?;
                    handle.get_window(&label).unwrap().close()?;
                    tokio::time::timeout(Duration::from_secs(10), async {
                        loop {
                            let status = browser.command(&handle, &core, "browser.login.status", &json!({})).await?;
                            if status["active"] == false { return Ok::<_, anyhow::Error>(status); }
                            tokio::time::sleep(Duration::from_millis(50)).await;
                        }
                    }).await??;
                    let stored = core.command("cookies.list", json!({})).await?;
                    ensure!(stored.as_array().is_some_and(|cookies| cookies.len() == 1), "Closing the login window did not automatically save cookies");
                    let saved = json!({"cookie":stored[0]});
                    let saved_text = core.read_cookie(saved["cookie"]["id"].as_str().unwrap())?;
                    ensure!(saved_text.contains("popup-secret") && saved_text.contains("host-secret"), "Final cookies were lost when the login window closed");
                    ensure!(core.command("snapshot", json!({})).await?["settings"]["downloadOptions"]["cookieId"] == "auto");
                    ensure!(!saved.to_string().contains("host-secret"));
                    tokio::time::timeout(Duration::from_secs(5), async {
                        while handle
                            .windows()
                            .keys()
                            .any(|label| label.starts_with("login-"))
                        {
                            tokio::time::sleep(Duration::from_millis(20)).await;
                        }
                    })
                    .await
                    .context("A login popup survived saving")?;
                    ensure!(
                        handle.get_webview_window("main").is_some(),
                        "Closing login exited the application"
                    );
                    let resume = format!("http://login.huifa.test:{}/resume", fixture.port);
                    browser
                        .command(
                            &handle,
                            &core,
                            "browser.login.open",
                            &json!({"url":resume,"mode":"embedded"}),
                        )
                        .await?;
                    wait_cookie(&browser, &handle, "popup_sid")
                        .await
                        .context("Reopened built-in profile lost its cookies")?;
                    let label = match browser.running.lock().await.as_ref().unwrap() {
                        Session::Embedded { label, .. } => label.clone(),
                        _ => unreachable!(),
                    };
                    let credentials = test_directory.join("data/credentials");
                    let retained = test_directory.join("retained-credentials");
                    std::fs::rename(&credentials, &retained)?;
                    std::fs::write(&credentials, b"simulate unavailable cookie storage")?;
                    handle.get_window(&label).unwrap().close()?;
                    let closed = tokio::time::timeout(Duration::from_secs(3), async {
                        while handle.get_window(&label).is_some() {
                            tokio::time::sleep(Duration::from_millis(20)).await;
                        }
                    }).await;
                    std::fs::remove_file(&credentials)?;
                    std::fs::rename(&retained, &credentials)?;
                    closed.context("Cookie sync failure prevented closing the login window")?;
                    let status = browser.command(&handle, &core, "browser.login.status", &json!({})).await?;
                    ensure!(status["active"] == false && status["saveError"].is_string(), "Closing after a sync failure lost the error or kept the session active");
                    ensure!(core.read_cookie(saved["cookie"]["id"].as_str().unwrap())? == saved_text, "Failed final sync replaced previously saved cookies");
                    tokio::time::timeout(Duration::from_secs(10), async {
                        loop {
                            if browser
                                .command(&handle, &core, "browser.login.status", &json!({}))
                                .await
                                .unwrap()["active"]
                                == false
                            {
                                break;
                            }
                            tokio::time::sleep(Duration::from_millis(50)).await;
                        }
                    })
                    .await
                    .context("Closed login session remained active")?;
                    browser.command(&handle, &core, "browser.login.open", &json!({"url":resume,"mode":"embedded"})).await?;
                    wait_cookie(&browser, &handle, "host_sid").await?;
                    let label = match browser.running.lock().await.as_ref().unwrap() {
                        Session::Embedded { label, .. } => label.clone(),
                        _ => unreachable!(),
                    };
                    std::fs::rename(&credentials, &retained)?;
                    std::fs::write(&credentials, b"simulate unavailable cookie storage")?;
                    let result = browser.command(&handle, &core, "browser.login.close", &json!({})).await;
                    std::fs::remove_file(&credentials)?;
                    std::fs::rename(&retained, &credentials)?;
                    ensure!(result.is_err(), "Closing hid the final cookie-sync failure");
                    tokio::time::timeout(Duration::from_secs(3), async {
                        while handle.get_window(&label).is_some() {
                            tokio::time::sleep(Duration::from_millis(20)).await;
                        }
                    }).await.context("Cookie sync failure prevented the close command from closing the browser")?;
                    ensure!(browser.running.lock().await.is_none(), "Close command kept a failed login session active");
                    ensure!(core.read_cookie(saved["cookie"]["id"].as_str().unwrap())? == saved_text, "Close command changed previously saved cookies after a sync failure");
                    browser
                        .command(
                            &handle,
                            &core,
                            "browser.login.open",
                            &json!({"url":url,"mode":"system"}),
                        )
                        .await?;
                    wait_cookie(&browser, &handle, "domain_sid")
                        .await
                        .context("Compatible browser did not receive cookies")?;
                    {
                        let running = browser.running.lock().await;
                        let Session::System { cdp, .. } = running.as_ref().unwrap() else { unreachable!() };
                        let targets = cdp.request("Target.getTargets", json!({}), None).await?;
                        for target in targets["targetInfos"].as_array().unwrap() {
                            if target["type"] == "page" {
                                cdp.request("Target.closeTarget", json!({"targetId":target["targetId"]}), None).await?;
                            }
                        }
                    }
                    tokio::time::sleep(Duration::from_millis(100)).await;
                    let status = browser.command(&handle, &core, "browser.login.status", &json!({})).await?;
                    ensure!(status["cookie"]["count"].as_u64().is_some_and(|count| count >= 2), "Closing the compatible browser lost its cookies before automatic saving");
                    ensure!(status["active"] == false, "Compatible browser process survived its last visible window");
                    browser
                        .command(
                            &handle,
                            &core,
                            "browser.login.open",
                            &json!({"url":resume,"mode":"system"}),
                        )
                        .await?;
                    wait_cookie(&browser, &handle, "host_sid")
                        .await
                        .context("Reopened compatible profile lost its cookies")?;
                    browser.command(&handle, &core, "browser.login.close", &json!({})).await?;
                    core.command("settings.save", json!({"settings":{"downloadOptions":{"proxy":format!("http://127.0.0.1:{}", fixture.port)}}})).await?;
                    for (mode, marker, cookie) in [
                        ("embedded", &fixture.proxy_embedded, "proxy_embedded"),
                        ("system", &fixture.proxy_system, "proxy_system"),
                    ] {
                        let proxy_url = format!("http://proxy-only.invalid/proxy-{mode}");
                        browser.command(&handle, &core, "browser.login.open", &json!({"url":proxy_url,"mode":mode})).await?;
                        wait_cookie(&browser, &handle, cookie).await
                            .with_context(|| format!("{mode} login did not use the saved HTTP proxy"))?;
                        ensure!(marker.load(Ordering::SeqCst), "{mode} login did not send an absolute HTTP URL to the proxy");
                        browser.command(&handle, &core, "browser.login.close", &json!({})).await?;
                    }
                    core.command("settings.save", json!({"settings":{"downloadOptions":{"proxy":""}}})).await?;
                    for mode in ["embedded", "system"] {
                        let direct_url = format!("http://direct-only.invalid:{}/direct-{mode}", fixture.port);
                        browser.command(&handle, &core, "browser.login.open", &json!({"url":direct_url,"mode":mode})).await?;
                        wait_cookie(&browser, &handle, &format!("direct_{mode}")).await
                            .with_context(|| format!("{mode} login did not return to direct connections after clearing the proxy"))?;
                        wait_cookie(&browser, &handle, "host_sid").await
                            .with_context(|| format!("{mode} login lost its existing cookies after clearing the proxy"))?;
                        browser.command(&handle, &core, "browser.login.close", &json!({})).await?;
                    }
                    browser.stop(&handle, &core).await;
                    core.shutdown().await?;
                    Ok::<_, anyhow::Error>(())
                })
                .await
                .context("Native login smoke test timed out")
                .and_then(|result| result);
                browser.stop(&handle, &core).await;
                *report.lock().unwrap() = Some(result);
                handle.exit(0);
            });
            Ok(())
        })
        .build(context)?;
    application.run_return(|_, _| {});
    let result = outcome
        .lock()
        .unwrap()
        .take()
        .context("Native login test did not finish")?;
    if result.is_err() {
        eprintln!(
            "Isolated browser test data retained at {}",
            directory.keep().display()
        );
    }
    result
}

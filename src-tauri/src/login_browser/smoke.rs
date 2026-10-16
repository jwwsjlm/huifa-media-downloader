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
        let flags = (stopped.clone(), denied.clone(), main_ready.clone());
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
                let path = request
                    .lines()
                    .next()
                    .and_then(|line| line.split_whitespace().nth(1))
                    .unwrap_or("/");
                if path == "/denied" {
                    flags.1.store(true, Ordering::SeqCst);
                }
                if path == "/main-ready" {
                    flags.2.store(true, Ordering::SeqCst);
                }
                let cookies = match path {
                    "/login" => {
                        "Set-Cookie: host_sid=host-secret; HttpOnly; Path=/; Max-Age=3600\r\nSet-Cookie: domain_sid=domain-secret; Domain=huifa.test; Path=/; Max-Age=3600\r\n"
                    }
                    "/popup" => {
                        "Set-Cookie: popup_sid=popup-secret; HttpOnly; Path=/; Max-Age=3600\r\n"
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
                        &app.get_webview_window(label)
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
    let application = tauri::Builder::default()
        .any_thread()
        .invoke_handler(tauri::generate_handler![upload_asset])
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
                    browser.close_window(&handle, &core, &label).await.unwrap();
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
                    handle
                        .get_webview_window(&label)
                        .unwrap()
                        .eval("window.open('/popup')")?;
                    wait_cookie(&browser, &handle, "popup_sid")
                        .await
                        .context("Login popup did not share the cookie store")?;
                    handle.get_webview_window(&label).unwrap().close()?;
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
                            .webview_windows()
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
                    handle.get_webview_window(&label).unwrap().close()?;
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

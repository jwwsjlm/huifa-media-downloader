use anyhow::{Context, Result, ensure};
use base64::{Engine, engine::general_purpose::STANDARD};
use huifa_core::{App, AppConfig};
use serde_json::{Value, json};
use std::{
    path::PathBuf,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
};
use tauri::{Emitter, Manager, State};
use tauri_plugin_clipboard_manager::ClipboardExt;
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_notification::NotificationExt;
use tauri_plugin_opener::OpenerExt;

mod login_browser;
#[cfg(windows)]
mod portable_update;
mod startup;
mod updater;

#[tauri::command]
async fn login_navigation(
    webview: tauri::Webview,
    action: String,
    url: Option<String>,
) -> Result<Value, String> {
    login_browser::navigation::command(webview, &action, url.as_deref())
        .await
        .map_err(error)
}

struct Host {
    core: Arc<App>,
    mcp: huifa_mcp::DesktopMcp,
    data_dir: PathBuf,
    portable: bool,
    closing: AtomicBool,
    shutdown_done: AtomicBool,
    updater: updater::Updater,
    login_browser: login_browser::LoginBrowser,
}

fn error(error: impl std::fmt::Display) -> String {
    huifa_core::process::redact(&format!("{error:#}"))
}

#[tauri::command]
async fn command(
    app: tauri::AppHandle,
    command: String,
    args: Value,
    host: State<'_, Host>,
) -> Result<Value, String> {
    if host.closing.load(Ordering::Acquire) || app.state::<startup::Initialization>().is_closing() {
        return Err("Application is shutting down".into());
    }
    if command == "host.clipboard_read" {
        return tauri::async_runtime::spawn_blocking(move || {
            clipboard_text(app.clipboard().read_text()).map(Value::String)
        })
        .await
        .map_err(|_| "Clipboard is temporarily unavailable".to_string())?;
    }
    if command.starts_with("browser.login.") {
        return host
            .login_browser
            .command(&app, &host.core, &command, &args)
            .await
            .map_err(error);
    }
    match command.as_str() {
        "mcp.status" => return host.mcp.status().map_err(error),
        "mcp.configure" => {
            let saved = host.mcp.status().map_err(error)?;
            return host
                .mcp
                .configure(
                    args["enabled"]
                        .as_bool()
                        .unwrap_or(saved["enabled"] == true),
                    true,
                    args.get("port")
                        .unwrap_or(&saved["port"])
                        .as_u64()
                        .filter(|n| *n > 0 && *n <= 65535)
                        .ok_or("Invalid MCP port")? as u16,
                    args["autoStart"].as_bool(),
                )
                .await
                .map_err(error);
        }
        "mcp.connection" => {
            let bridge = std::env::current_exe()
                .map_err(error)?
                .parent()
                .ok_or("Application directory unavailable")?
                .join(if cfg!(windows) {
                    "huifa-mcp-bridge.exe"
                } else {
                    "huifa-mcp-bridge"
                });
            return host
                .mcp
                .connection(&bridge.to_string_lossy())
                .map_err(error);
        }
        _ => {}
    }
    if command == "media.file" {
        return Err("File paths are internal to the desktop host".into());
    }
    if matches!(
        command.as_str(),
        "tasks.add" | "probe.video" | "probe.collection"
    ) && let Err(reason) = host.login_browser.sync(&app, &host.core).await
    {
        let _ = app.emit("huifa-host-error", json!({"message":format!("Could not refresh browser cookies: {}. Continuing with previously saved matching cookies, or anonymously if none are saved.", error(reason))}));
    }
    if command == "host.paths" {
        return Ok(
            json!({"downloadDir":clipboard_path(host.core.download_directory().to_string_lossy().into_owned()),"dataDir":host.data_dir}),
        );
    }
    if command == "host.download_directory" {
        let path = PathBuf::from(args["path"].as_str().ok_or("Choose a download folder")?);
        let core = host.core.clone();
        let path = tauri::async_runtime::spawn_blocking(move || core.set_download_directory(&path))
            .await
            .map_err(error)?
            .map_err(error)?;
        return Ok(json!({"downloadDir":clipboard_path(path.to_string_lossy().into_owned())}));
    }
    if serde_json::to_vec(&args).map_err(error)?.len() > 3 * 1024 * 1024 {
        return Err("Command arguments are too large".into());
    }
    if command == "cookies.read" {
        return host
            .core
            .read_cookie(args["id"].as_str().ok_or("A Cookie ID is required")?)
            .map(Value::String)
            .map_err(error);
    }
    if command == "access.change_password" {
        let value = host.core.command(&command, args).await.map_err(error)?;
        host.mcp.shutdown().await;
        let config = host.mcp.status().map_err(error)?;
        if config["enabled"] == true {
            host.mcp
                .configure(
                    true,
                    true,
                    config["port"].as_u64().unwrap_or(19492) as u16,
                    None,
                )
                .await
                .map_err(error)?;
        }
        return Ok(value);
    }
    host.core.command(&command, args).await.map_err(error)
}

fn clipboard_text(
    result: tauri_plugin_clipboard_manager::Result<String>,
) -> Result<String, String> {
    match result {
        Ok(text) if text.len() > 3 * 1024 * 1024 => Err("Clipboard text is too large".into()),
        Ok(text) => Ok(text),
        Err(tauri_plugin_clipboard_manager::Error::Clipboard(message))
            if message
                == "The clipboard contents were not available in the requested format or the clipboard is empty." =>
        {
            Ok(String::new())
        }
        Err(_) => Err("Clipboard is temporarily unavailable".into()),
    }
}

#[tauri::command]
async fn task_copy_value(
    host: State<'_, Host>,
    id: String,
    kind: String,
) -> Result<String, String> {
    let value = host.core.task_copy_value(&id, &kind).map_err(error)?;
    Ok(if kind == "path" {
        clipboard_path(value)
    } else {
        value
    })
}

fn clipboard_path(value: String) -> String {
    if let Some(path) = value.strip_prefix(r"\\?\UNC\") {
        format!(r"\\{path}")
    } else {
        value.strip_prefix(r"\\?\").unwrap_or(&value).to_string()
    }
}

#[tauri::command]
async fn release_status(host: State<'_, Host>) -> Result<Value, String> {
    host.core
        .command("app.release_status", json!({}))
        .await
        .map_err(error)
}

#[tauri::command]
fn open_project(app: tauri::AppHandle) -> Result<(), String> {
    app.opener()
        .open_url(
            "https://github.com/jwwsjlm/huifa-media-downloader",
            None::<&str>,
        )
        .map_err(error)
}

#[tauri::command]
fn open_release(app: tauri::AppHandle, url: String) -> Result<(), String> {
    if !huifa_core::releases::valid_url(&url) {
        return Err("Unsupported release URL".into());
    }
    app.opener().open_url(url, None::<&str>).map_err(error)
}

#[tauri::command]
async fn capabilities(app: tauri::AppHandle) -> Result<Value, String> {
    let initialization = app.state::<startup::Initialization>();
    initialization.wait().await?;
    if initialization.is_closing() {
        return Err("Application is shutting down".into());
    }
    let host = app.state::<Host>();
    Ok(
        json!({"desktop":true,"browserCookies":true,"remoteBrowser":false,"updates":host.updater.supported(),"directoryPicker":true}),
    )
}

fn portable_mode(executable: &std::path::Path) -> bool {
    executable
        .parent()
        .is_some_and(|directory| directory.join("portable.flag").is_file())
}

#[tauri::command]
fn open_download_directory(app: tauri::AppHandle, host: State<'_, Host>) -> Result<(), String> {
    app.opener()
        .open_path(
            clipboard_path(
                host.core
                    .download_directory()
                    .to_string_lossy()
                    .into_owned(),
            ),
            None::<&str>,
        )
        .map_err(error)
}

#[tauri::command]
async fn choose_directory(
    app: tauri::AppHandle,
    default_path: String,
) -> Result<Option<String>, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let mut dialog = app.dialog().file().set_title("Choose folder");
        if !default_path.is_empty() {
            dialog = dialog.set_directory(default_path);
        }
        dialog
            .blocking_pick_folder()
            .map(|path| {
                path.into_path()
                    .map(|path| path.to_string_lossy().into_owned())
                    .map_err(error)
            })
            .transpose()
    })
    .await
    .map_err(error)?
}

fn file_kind(kind: &str) -> Result<&str> {
    match kind {
        "video" | "media" => Ok("media"),
        "cover" | "originalCover" | "thumbnail" | "taskThumbnail" | "description" | "info"
        | "subtitle" | "chapter" => Ok(kind),
        _ => anyhow::bail!("Unsupported media file kind"),
    }
}

async fn owned_file(core: &App, id: &str, kind: &str) -> Result<(PathBuf, String)> {
    ensure!(
        uuid::Uuid::parse_str(id)?.to_string() == id,
        "Invalid media ID"
    );
    let value = core
        .command("media.file", json!({"id":id,"kind":file_kind(kind)?}))
        .await?;
    let path = PathBuf::from(value["path"].as_str().context("Missing owned media path")?);
    Ok((
        path,
        value["mimeType"]
            .as_str()
            .unwrap_or("application/octet-stream")
            .to_owned(),
    ))
}

#[tauri::command]
async fn result_action(
    app: tauri::AppHandle,
    host: State<'_, Host>,
    id: String,
    kind: String,
    action: String,
) -> Result<(), String> {
    let (path, _) = owned_file(&host.core, &id, &kind).await.map_err(error)?;
    match action.as_str() {
        "open" => app
            .opener()
            .open_path(path.to_string_lossy().as_ref(), None::<&str>)
            .map_err(error),
        "reveal" => app.opener().reveal_item_in_dir(path).map_err(error),
        "download" => {
            let filename = path
                .file_name()
                .context("Invalid media filename")
                .map_err(error)?
                .to_string_lossy()
                .into_owned();
            let selected = tauri::async_runtime::spawn_blocking(move || {
                app.dialog()
                    .file()
                    .set_title("Save media file")
                    .set_file_name(filename)
                    .blocking_save_file()
            })
            .await
            .map_err(error)?;
            if let Some(destination) = selected {
                let destination = destination.into_path().map_err(error)?;
                tauri::async_runtime::spawn_blocking(move || copy_export(&path, &destination))
                    .await
                    .map_err(error)?
                    .map_err(error)?;
            }
            Ok(())
        }
        _ => Err("Unsupported result action".into()),
    }
}

fn copy_export(source: &std::path::Path, target: &std::path::Path) -> Result<()> {
    if target.exists() && source.canonicalize()? == target.canonicalize()? {
        return Ok(());
    }
    let parent = target.parent().context("Invalid output folder")?;
    let mut temporary = tempfile::NamedTempFile::new_in(parent)?;
    std::io::copy(&mut std::fs::File::open(source)?, temporary.as_file_mut())?;
    temporary.as_file_mut().sync_all()?;
    temporary.persist(target).map_err(|reason| reason.error)?;
    #[cfg(unix)]
    std::fs::File::open(parent)?.sync_all()?;
    Ok(())
}

#[tauri::command]
async fn upload_asset(host: State<'_, Host>, name: String, data: String) -> Result<Value, String> {
    if data.len() > 35 * 1024 * 1024 {
        return Err("Image upload is too large".into());
    }
    let bytes = STANDARD.decode(data).map_err(error)?;
    let core = host.core.clone();
    tauri::async_runtime::spawn_blocking(move || store_image(&core, &name, bytes))
        .await
        .map_err(error)?
        .map_err(error)
}

fn store_image(core: &App, name: &str, bytes: Vec<u8>) -> Result<Value> {
    ensure!(
        !bytes.is_empty() && bytes.len() <= 25 * 1024 * 1024,
        "Image must be at most 25 MiB"
    );
    let reader = image::ImageReader::new(std::io::Cursor::new(&bytes)).with_guessed_format()?;
    let (width, height) = reader.into_dimensions()?;
    ensure!(
        width > 0
            && height > 0
            && width <= 16384
            && height <= 16384
            && u64::from(width) * u64::from(height) <= 50_000_000,
        "Image dimensions are too large"
    );
    let mut reader = image::ImageReader::new(std::io::Cursor::new(&bytes)).with_guessed_format()?;
    let mut limits = image::Limits::default();
    limits.max_image_width = Some(16384);
    limits.max_image_height = Some(16384);
    limits.max_alloc = Some(400_000_000);
    reader.limits(limits);
    let image = reader.decode()?;
    core.store_cover_upload(name, &image)
}

#[tauri::command]
fn notify(app: tauri::AppHandle, title: String, body: String) -> Result<(), String> {
    if title.len() > 200 || body.len() > 2000 {
        return Err("Notification is too large".into());
    }
    app.notification()
        .builder()
        .title(title)
        .body(body)
        .show()
        .map_err(error)
}

#[tauri::command]
async fn check_update(
    app: tauri::AppHandle,
    host: State<'_, Host>,
) -> Result<updater::Status, String> {
    host.updater
        .check(&app, &host.core.download_proxy().map_err(error)?)
        .await
        .map_err(error)
}

#[tauri::command]
fn update_status(host: State<'_, Host>) -> updater::Status {
    host.updater.status()
}

#[tauri::command]
async fn download_update(
    app: tauri::AppHandle,
    host: State<'_, Host>,
) -> Result<updater::Status, String> {
    host.updater
        .download(&app, &host.core.download_proxy().map_err(error)?)
        .await
        .map_err(error)
}

#[tauri::command]
async fn install_update(app: tauri::AppHandle, host: State<'_, Host>) -> Result<(), String> {
    host.updater.install(&app, &host).await.map_err(error)
}

async fn initialize_host(
    handle: &tauri::AppHandle,
    mut config: AppConfig,
    portable: bool,
) -> Result<Host> {
    config.tool_dir = config.tool_dir.filter(|directory| directory.is_dir());
    let data_dir = config.data_dir.clone();
    let core = App::open(config).await?;
    let mcp = huifa_mcp::DesktopMcp::new(core.clone());
    let prepared = (|| {
        let login_browser = login_browser::LoginBrowser::new(data_dir.join("login-browser"))
            .context("Could not initialize the login browser profile")?;
        Ok::<_, anyhow::Error>((
            login_browser,
            mcp.status()
                .context("Could not read MCP startup settings")?,
        ))
    })();
    let (login_browser, config) = match prepared {
        Ok(prepared) => prepared,
        Err(reason) => {
            core.shutdown()
                .await
                .context("Could not save tasks after startup failed")?;
            return Err(reason);
        }
    };
    if !handle.state::<startup::Initialization>().is_closing()
        && let Err(reason) = mcp
            .configure(
                config["autoStart"] == true,
                true,
                config["port"].as_u64().unwrap_or(19492) as u16,
                None,
            )
            .await
    {
        eprintln!("MCP startup failed: {}", error(reason));
    }
    let updater =
        updater::Updater::new(&data_dir, updater::public_key()?, updater::target(portable));
    Ok(Host {
        login_browser,
        mcp,
        core,
        data_dir,
        portable,
        closing: AtomicBool::new(false),
        shutdown_done: AtomicBool::new(false),
        updater,
    })
}

pub fn run() {
    #[cfg(windows)]
    let recovery = match portable_update::startup() {
        Ok(message) => message,
        Err(reason) => {
            startup::report(&format!("Portable update: {reason:#}"));
            std::process::exit(1);
        }
    };
    #[cfg(not(windows))]
    let recovery: Option<String> = None;
    startup::install();
    let builder = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_clipboard_manager::init())
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::Destroyed = event {
                let handle = window.app_handle().clone();
                let label = window.label().to_owned();
                if label.starts_with("login-") {
                    tauri::async_runtime::spawn(async move {
                        if let Some(host) = handle.try_state::<Host>() {
                            host.login_browser.window_closed(&handle, &label).await;
                        }
                    });
                }
            }
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                if window.label().starts_with("login-") {
                    api.prevent_close();
                    let handle = window.app_handle().clone();
                    let label = window.label().to_owned();
                    tauri::async_runtime::spawn(async move {
                        let host = handle.state::<Host>();
                        if let Err(reason) = host.login_browser.close_window(&handle, &host.core, &label).await {
                            let _ = handle.emit("huifa-host-error", json!({"message":format!("The login browser was closed, but the last Cookie sync failed: {}. Previously saved cookies were kept. Reopen the login browser to retry.", error(reason))}));
                        }
                    });
                    return;
                }
                if window.label() != "main" {
                    return;
                }
                if window.try_state::<Host>().is_some_and(|host| host.shutdown_done.load(Ordering::Acquire)) {
                    return;
                }
                api.prevent_close();
                begin_shutdown(window.app_handle());
            }
        })
        .setup(|app| {
            let portable = portable_mode(&std::env::current_exe()?);
            let base = std::env::var_os("HUIFA_DATA_DIR")
                .map(PathBuf::from)
                .unwrap_or(app.path().local_data_dir()?.join("HuifaDownloaderRust"));
            let download_dir = std::env::var_os("HUIFA_DOWNLOAD_DIR")
                .map(PathBuf::from)
                .unwrap_or(
                    app.path()
                        .download_dir()
                        .unwrap_or(base.join("downloads"))
                        .join("HuifaDownloaderRust"),
                );
            let tools = std::env::var_os("HUIFA_TOOL_DIR")
                .map(PathBuf::from)
                .unwrap_or(app.path().resource_dir()?.join("tools"));
            let config = AppConfig {
                data_dir: base.clone(),
                download_dir,
                tool_dir: Some(tools),
            };
            let (initialization, ready) = startup::Initialization::new();
            app.manage(initialization);
            let handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                let worker = handle.clone();
                let result = tauri::async_runtime::spawn_blocking(move || {
                    tauri::async_runtime::block_on(initialize_host(&worker, config, portable))
                })
                .await;
                let panic_reported = matches!(&result, Err(tauri::Error::JoinError(reason)) if reason.is_panic());
                let result = result.map_err(error).and_then(|result| result.map_err(error));
                match result {
                    Ok(host) => {
                        let mut events = host.core.subscribe();
                        handle.manage(host);
                        if let Some(message) = recovery.as_deref() {
                            handle.state::<Host>().updater.report_recovery(&handle, message);
                        }
                        let events_handle = handle.clone();
                        tauri::async_runtime::spawn(async move {
                            loop {
                                match events.recv().await {
                                    Ok(event) => { let _ = events_handle.emit("huifa-event", event); }
                                    Err(tokio::sync::broadcast::error::RecvError::Lagged(_)) => { let _ = events_handle.emit("huifa-resync", ()); }
                                    Err(_) => break,
                                }
                            }
                        });
                        startup::ready();
                        let _ = ready.send(Some(Ok(())));
                        let mut interval = tokio::time::interval(std::time::Duration::from_secs(1));
                        loop {
                            interval.tick().await;
                            if handle.state::<startup::Initialization>().is_closing() { break; }
                            let host = handle.state::<Host>();
                            if host.closing.load(Ordering::Acquire) { break; }
                            let _ = host.login_browser.sync(&handle, &host.core).await;
                        }
                    }
                    Err(reason) => {
                        let closing = handle.state::<startup::Initialization>().is_closing();
                        let _ = ready.send(Some(Err(reason.clone())));
                        if !closing {
                            // Worker panics have already shown the startup panic report.
                            if !panic_reported {
                                let _ = tauri::async_runtime::spawn_blocking(move || startup::report(&reason)).await;
                            }
                            handle.exit(1);
                        }
                    }
                }
            });
            Ok(())
        })
        .register_asynchronous_uri_scheme_protocol("huifa-media", |context, request, responder| {
            let handle = context.app_handle().clone();
            tauri::async_runtime::spawn(async move {
                let result = async {
                    let id = request.uri().path().trim_start_matches('/');
                    let kind = request
                        .uri()
                        .query()
                        .and_then(|query| query.strip_prefix("kind="))
                        .unwrap_or("thumbnail");
                    let host = handle.try_state::<Host>().context("Application is starting")?;
                    let (path, mime) = owned_file(&host.core, id, kind).await?;
                    ensure!(
                        mime.starts_with("image/"),
                        "Only owned images are served to the webview"
                    );
                    let bytes = tokio::fs::read(path).await?;
                    ensure!(bytes.len() <= 40 * 1024 * 1024, "Image is too large");
                    Ok::<_, anyhow::Error>((bytes, mime))
                }
                .await;
                let response = match result {
                    Ok((bytes, mime)) => tauri::http::Response::builder()
                        .header("Content-Type", mime)
                        .header("Cache-Control", "no-store")
                        .header("Access-Control-Allow-Origin", "*")
                        .body(bytes),
                    Err(_) => tauri::http::Response::builder()
                        .status(404)
                        .header("Cache-Control", "no-store")
                        .body(Vec::new()),
                };
                responder.respond(response.unwrap());
            });
        })
        .invoke_handler(tauri::generate_handler![
            command,
            capabilities,
            open_download_directory,
            choose_directory,
            result_action,
            upload_asset,
            notify,
            check_update,
            download_update,
            update_status,
            install_update,
            task_copy_value,
            release_status,
            open_release,
            open_project,
            login_navigation
        ]);
    let application = match builder.build(tauri::generate_context!()) {
        Ok(application) => application,
        Err(reason) => {
            startup::report(&format!("{reason:#}"));
            std::process::exit(1);
        }
    };
    let startup_failed = Arc::new(AtomicBool::new(false));
    let failed = startup_failed.clone();
    let code = application.run_return(move |app, event| {
        if let tauri::RunEvent::Exit = event {
            let initialization = app.state::<startup::Initialization>();
            failed.store(
                initialization.failed() && !initialization.is_closing(),
                Ordering::Release,
            );
        }
        if let tauri::RunEvent::ExitRequested { api, .. } = event {
            if app
                .try_state::<Host>()
                .is_some_and(|host| host.shutdown_done.load(Ordering::Acquire))
                || app.state::<startup::Initialization>().failed()
            {
                return;
            }
            api.prevent_exit();
            begin_shutdown(app);
        }
    });
    std::process::exit(if startup_failed.load(Ordering::Acquire) {
        1
    } else {
        code
    });
}

fn begin_shutdown(app: &tauri::AppHandle) {
    if !app.state::<startup::Initialization>().request_close() {
        return;
    }
    if let Some(host) = app.try_state::<Host>() {
        host.closing.store(true, Ordering::Release);
        host.updater.cancel();
    }
    let handle = app.clone();
    tauri::async_runtime::spawn(async move {
        if handle
            .state::<startup::Initialization>()
            .wait()
            .await
            .is_err()
        {
            handle.exit(0);
            return;
        }
        let host = handle.state::<Host>();
        host.closing.store(true, Ordering::Release);
        host.mcp.shutdown().await;
        host.login_browser.stop(&handle, &host.core).await;
        match host.core.shutdown().await {
            Ok(()) => {
                handle
                    .state::<Host>()
                    .shutdown_done
                    .store(true, Ordering::Release);
                handle.exit(0);
            }
            Err(reason) => {
                handle
                    .state::<startup::Initialization>()
                    .closing
                    .store(false, Ordering::Release);
                handle
                    .state::<Host>()
                    .closing
                    .store(false, Ordering::Release);
                let _ = handle.emit("huifa-host-error", json!({"message":format!("Could not finish saving tasks: {reason}. Close again to retry shutdown.")}));
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clipboard_reads_preserve_text_skip_other_formats_and_hide_errors() {
        assert_eq!(
            clipboard_text(Ok("https://example.test/video".into())).unwrap(),
            "https://example.test/video"
        );
        assert_eq!(clipboard_text(Ok(String::new())).unwrap(), "");
        assert_eq!(clipboard_text(Err(tauri_plugin_clipboard_manager::Error::Clipboard("The clipboard contents were not available in the requested format or the clipboard is empty.".into()))).unwrap(), "");
        assert_eq!(
            clipboard_text(Err(tauri_plugin_clipboard_manager::Error::Clipboard(
                "temporary busy: private clipboard diagnostic".into()
            )))
            .unwrap_err(),
            "Clipboard is temporarily unavailable"
        );
    }

    #[test]
    fn clipboard_reads_reject_text_over_command_payload_limit() {
        let limit = 3 * 1024 * 1024;
        assert_eq!(clipboard_text(Ok("a".repeat(limit))).unwrap().len(), limit);
        assert_eq!(
            clipboard_text(Ok("a".repeat(limit + 1))).unwrap_err(),
            "Clipboard text is too large"
        );
        assert_eq!(
            clipboard_text(Ok("中".repeat(limit / 3 + 1))).unwrap_err(),
            "Clipboard text is too large"
        );
    }

    #[test]
    fn command_errors_keep_the_cause_without_signed_urls() {
        let reason = anyhow::anyhow!("HTTP 403 https://example.test/file?token=secret")
            .context("Cannot download yt-dlp runtime");
        let message = error(reason);
        assert!(message.contains("Cannot download yt-dlp runtime: HTTP 403"));
        assert!(!message.contains("secret"));
    }
    #[test]
    fn portable_marker_only_applies_beside_the_executable() -> Result<()> {
        let directory = tempfile::tempdir()?;
        let executable = directory.path().join("app/HuifaDownloader.exe");
        std::fs::create_dir_all(executable.parent().unwrap())?;
        std::fs::write(directory.path().join("portable.flag"), b"")?;
        assert!(!portable_mode(&executable));
        let marker = executable.parent().unwrap().join("portable.flag");
        std::fs::create_dir(&marker)?;
        assert!(!portable_mode(&executable));
        std::fs::remove_dir(&marker)?;
        std::fs::write(&marker, b"")?;
        assert!(portable_mode(&executable));
        Ok(())
    }
    #[test]
    fn only_owned_file_kinds_cross_host_boundary() {
        assert_eq!(file_kind("video").unwrap(), "media");
        assert_eq!(file_kind("cover").unwrap(), "cover");
        assert_eq!(file_kind("taskThumbnail").unwrap(), "taskThumbnail");
        assert!(file_kind("../../secret").is_err());
        assert!(file_kind("file:///etc/passwd").is_err());
    }
    #[test]
    fn copied_paths_use_normal_windows_notation() {
        assert_eq!(
            clipboard_path(r"\\?\D:\下载\视频.mkv".into()),
            r"D:\下载\视频.mkv"
        );
        assert_eq!(
            clipboard_path(r"\\?\UNC\server\share\video.mkv".into()),
            r"\\server\share\video.mkv"
        );
        assert_eq!(
            clipboard_path("/downloads/video.mkv".into()),
            "/downloads/video.mkv"
        );
    }
    #[test]
    fn export_preserves_existing_target_on_failure_and_replaces_on_success() -> Result<()> {
        let directory = tempfile::tempdir()?;
        let source = directory.path().join("source.mp4");
        let target = directory.path().join("result.mp4");
        std::fs::write(&source, b"new media")?;
        std::fs::write(&target, b"previous media")?;
        assert!(copy_export(&directory.path().join("missing.mp4"), &target).is_err());
        assert_eq!(std::fs::read(&target)?, b"previous media");
        copy_export(&source, &target)?;
        assert_eq!(std::fs::read(&target)?, b"new media");
        assert_eq!(std::fs::read(&source)?, b"new media");
        copy_export(&source, &source)?;
        assert_eq!(std::fs::read(&source)?, b"new media");
        Ok(())
    }
    #[tokio::test]
    async fn image_upload_checks_content_and_owns_filename() -> Result<()> {
        let directory = tempfile::tempdir()?;
        let core = App::open(AppConfig {
            data_dir: directory.path().join("data"),
            download_dir: directory.path().join("downloads"),
            tool_dir: None,
        })
        .await?;
        assert!(store_image(&core, "../../untrusted.png", b"not an image".to_vec()).is_err());
        let mut data = std::io::Cursor::new(Vec::new());
        image::DynamicImage::new_rgb8(2, 3).write_to(&mut data, image::ImageFormat::Png)?;
        let value = store_image(&core, "../../untrusted.png", data.into_inner())?;
        let id = value["id"].as_str().unwrap();
        assert!(uuid::Uuid::parse_str(id).is_ok());
        assert!(
            directory
                .path()
                .join("data/assets")
                .join(format!("{id}.png"))
                .is_file()
        );
        assert_eq!(value["name"], "untrusted.png");
        core.command("cover.release_upload", json!({"assetId":id}))
            .await?;
        assert!(
            !directory
                .path()
                .join("data/assets")
                .join(format!("{id}.png"))
                .exists()
        );
        core.shutdown().await?;
        Ok(())
    }
}

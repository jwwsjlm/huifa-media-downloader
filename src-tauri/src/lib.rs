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
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_notification::NotificationExt;
use tauri_plugin_opener::OpenerExt;
use tauri_plugin_updater::UpdaterExt;
use tokio::sync::Mutex;

struct Host {
    core: Arc<App>,
    data_dir: PathBuf,
    download_dir: PathBuf,
    portable: bool,
    closing: AtomicBool,
    shutdown_done: AtomicBool,
    update: Mutex<Option<tauri_plugin_updater::Update>>,
}

fn error(error: impl std::fmt::Display) -> String {
    error.to_string()
}

#[tauri::command]
async fn command(command: String, args: Value, host: State<'_, Host>) -> Result<Value, String> {
    if command == "media.file" {
        return Err("File paths are internal to the desktop host".into());
    }
    if command == "host.paths" {
        return Ok(json!({"downloadDir":host.download_dir,"dataDir":host.data_dir}));
    }
    if serde_json::to_vec(&args).map_err(error)?.len() > 3 * 1024 * 1024 {
        return Err("Command arguments are too large".into());
    }
    host.core.command(&command, args).await.map_err(error)
}

#[tauri::command]
fn capabilities(host: State<'_, Host>) -> Value {
    json!({"desktop":true,"browserCookies":true,"remoteBrowser":false,"updates":!host.portable,"directoryPicker":true})
}

fn portable_mode(executable: &std::path::Path) -> bool {
    executable
        .parent()
        .is_some_and(|directory| directory.join("portable.flag").is_file())
}

#[tauri::command]
fn open_download_directory(app: tauri::AppHandle, host: State<'_, Host>) -> Result<(), String> {
    app.opener()
        .open_path(host.download_dir.to_string_lossy().as_ref(), None::<&str>)
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
        "cover" | "originalCover" | "thumbnail" | "description" | "info" | "subtitle"
        | "chapter" => Ok(kind),
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
    let directory = host.data_dir.join("assets");
    tauri::async_runtime::spawn_blocking(move || store_image(&directory, &name, bytes))
        .await
        .map_err(error)?
        .map_err(error)
}

fn store_image(directory: &std::path::Path, name: &str, bytes: Vec<u8>) -> Result<Value> {
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
    let id = uuid::Uuid::new_v4().to_string();
    std::fs::create_dir_all(directory)?;
    let path = directory.join(format!("{id}.png"));
    image.save(&path)?;
    Ok(
        json!({"id":id,"name":std::path::Path::new(name).file_name().unwrap_or_default().to_string_lossy(),"kind":"image"}),
    )
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
async fn check_update(app: tauri::AppHandle, host: State<'_, Host>) -> Result<Value, String> {
    if host.portable {
        return Err("Download and extract the latest portable ZIP to update this edition".into());
    }
    let update = app.updater().map_err(error)?.check().await.map_err(error)?;
    let value = update
        .as_ref()
        .map(|update| json!({"version":update.version,"body":update.body}))
        .unwrap_or(json!({}));
    *host.update.lock().await = update;
    Ok(value)
}

#[tauri::command]
async fn install_update(app: tauri::AppHandle, host: State<'_, Host>) -> Result<(), String> {
    if host.portable {
        return Err("Portable ZIP updates do not run the installer".into());
    }
    let mut guard = host.update.lock().await;
    let update = guard.as_mut().ok_or("Check for an update first")?;
    let bytes = update.download(|_, _| {}, || {}).await.map_err(error)?;
    host.closing.store(true, Ordering::Release);
    if let Err(reason) = host.core.shutdown().await {
        host.closing.store(false, Ordering::Release);
        return Err(error(reason));
    }
    host.shutdown_done.store(true, Ordering::Release);
    if let Err(reason) = update.install(bytes) {
        let message = format!(
            "Update installation failed after tasks were saved: {reason}. Restart the application to resume work."
        );
        let _ = app.emit("huifa-host-error", json!({"message":message}));
        return Err(message);
    }
    app.restart();
}

pub fn run() {
    let builder = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                let Some(host) = window.try_state::<Host>() else {
                    return;
                };
                if host.shutdown_done.load(Ordering::Acquire) {
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
            let tool_dir = tools.is_dir().then_some(tools);
            let core = tauri::async_runtime::block_on(App::open(AppConfig {
                data_dir: base.clone(),
                download_dir: download_dir.clone(),
                tool_dir,
            }))?;
            let mut events = core.subscribe();
            let handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                loop {
                    match events.recv().await {
                        Ok(event) => {
                            let _ = handle.emit("huifa-event", event);
                        }
                        Err(tokio::sync::broadcast::error::RecvError::Lagged(_)) => {
                            let _ = handle.emit("huifa-resync", ());
                        }
                        Err(_) => break,
                    }
                }
            });
            app.manage(Host {
                core,
                data_dir: base,
                download_dir,
                portable,
                closing: AtomicBool::new(false),
                shutdown_done: AtomicBool::new(false),
                update: Mutex::new(None),
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
                    let host = handle.state::<Host>();
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
            install_update
        ]);
    let application = builder
        .build(tauri::generate_context!())
        .expect("Could not start Huifa Media Downloader");
    application.run(|app, event| {
        if let tauri::RunEvent::ExitRequested { api, .. } = event {
            let host = app.state::<Host>();
            if host.shutdown_done.load(Ordering::Acquire) {
                return;
            }
            api.prevent_exit();
            begin_shutdown(app);
        }
    });
}

fn begin_shutdown(app: &tauri::AppHandle) {
    let host = app.state::<Host>();
    if host.closing.swap(true, Ordering::AcqRel) {
        return;
    }
    let core = Arc::clone(&host.core);
    let handle = app.clone();
    tauri::async_runtime::spawn(async move {
        match core.shutdown().await {
            Ok(()) => {
                handle
                    .state::<Host>()
                    .shutdown_done
                    .store(true, Ordering::Release);
                handle.exit(0);
            }
            Err(reason) => {
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
        assert!(file_kind("../../secret").is_err());
        assert!(file_kind("file:///etc/passwd").is_err());
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
    #[test]
    fn image_upload_checks_content_and_owns_filename() -> Result<()> {
        let directory = tempfile::tempdir()?;
        assert!(
            store_image(
                directory.path(),
                "../../untrusted.png",
                b"not an image".to_vec()
            )
            .is_err()
        );
        let mut data = std::io::Cursor::new(Vec::new());
        image::DynamicImage::new_rgb8(2, 3).write_to(&mut data, image::ImageFormat::Png)?;
        let value = store_image(directory.path(), "../../untrusted.png", data.into_inner())?;
        let id = value["id"].as_str().unwrap();
        assert!(uuid::Uuid::parse_str(id).is_ok());
        assert!(directory.path().join(format!("{id}.png")).is_file());
        assert_eq!(value["name"], "untrusted.png");
        Ok(())
    }
}

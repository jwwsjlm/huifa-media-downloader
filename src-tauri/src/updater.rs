use anyhow::{Context, Result, bail, ensure};
use base64::{Engine, engine::general_purpose::STANDARD};
use minisign_verify::{PublicKey, Signature};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::{Read, Write},
    path::{Path, PathBuf},
    sync::Mutex,
    time::{Duration, Instant},
};
use tauri::{Emitter, Manager};
use tauri_plugin_updater::{Update, UpdaterExt};
use tokio_util::sync::CancellationToken;

const MAX_PACKAGE_SIZE: u64 = 1024 * 1024 * 1024;

pub(crate) fn public_key() -> Result<String> {
    let config: serde_json::Value = serde_json::from_str(include_str!("../tauri.conf.json"))?;
    Ok(config["plugins"]["updater"]["pubkey"]
        .as_str()
        .context("Updater public key is missing")?
        .into())
}

#[derive(Clone, Copy, Debug, Default, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum Phase {
    #[default]
    Idle,
    Checking,
    Available,
    Downloading,
    Verifying,
    Ready,
    Installing,
    Error,
}

#[derive(Clone, Debug, Default, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Status {
    pub revision: u64,
    pub phase: Phase,
    pub version: Option<String>,
    pub body: Option<String>,
    pub downloaded_bytes: u64,
    pub total_bytes: Option<u64>,
    pub error: Option<String>,
    pub can_download: bool,
    pub can_install: bool,
    pub restart_required: bool,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub(crate) struct Package {
    pub version: String,
    pub target: String,
    pub url: String,
    pub signature: String,
}

impl Package {
    fn from_update(update: &Update, target: &str) -> Self {
        Self {
            version: update.version.clone(),
            target: target.into(),
            url: update.download_url.to_string(),
            signature: update.signature.clone(),
        }
    }

    fn directory(&self, root: &Path) -> Result<PathBuf> {
        ensure!(valid_target(&self.target), "Unsupported update platform");
        Ok(root
            .join(&self.target)
            .join(format!("{:x}", Sha256::digest(self.signature.as_bytes()))))
    }
}

#[derive(Default)]
struct State {
    status: Status,
    update: Option<Update>,
}

pub struct Updater {
    state: Mutex<State>,
    operation: tokio::sync::Mutex<()>,
    cancel: CancellationToken,
    root: PathBuf,
    pubkey: String,
    target: Option<String>,
}

impl Updater {
    pub fn new(data_dir: &Path, pubkey: String, target: Option<String>) -> Self {
        Self {
            state: Mutex::new(State::default()),
            operation: tokio::sync::Mutex::new(()),
            cancel: CancellationToken::new(),
            root: data_dir.join("updates"),
            pubkey,
            target,
        }
    }

    pub fn supported(&self) -> bool {
        self.target.is_some()
    }

    pub fn status(&self) -> Status {
        self.state.lock().unwrap().status.clone()
    }

    fn change<R: tauri::Runtime>(
        &self,
        app: &tauri::AppHandle<R>,
        change: impl FnOnce(&mut State),
    ) -> Status {
        let status = {
            let mut state = self.state.lock().unwrap();
            change(&mut state);
            state.status.revision += 1;
            state.status.clone()
        };
        let _ = app.emit("huifa-update", &status);
        status
    }

    fn failed<R: tauri::Runtime>(
        &self,
        app: &tauri::AppHandle<R>,
        reason: impl std::fmt::Display,
    ) -> Status {
        self.change(app, |state| {
            state.status.phase = Phase::Error;
            state.status.error = Some(super::error(reason));
        })
    }

    pub fn cancel(&self) {
        self.cancel.cancel();
    }

    pub fn report_recovery<R: tauri::Runtime>(&self, app: &tauri::AppHandle<R>, message: &str) {
        self.failed(app, message);
    }

    pub async fn check<R: tauri::Runtime>(
        &self,
        app: &tauri::AppHandle<R>,
        proxy: &str,
    ) -> Result<Status> {
        let Ok(_operation) = self.operation.try_lock() else {
            return Ok(self.status());
        };
        ensure!(!self.cancel.is_cancelled(), "Application is shutting down");
        let target = self
            .target
            .as_deref()
            .context("This package requires a manual update")?;
        self.change(app, |state| {
            state.status.phase = Phase::Checking;
            state.status.error = None;
            state.status.can_download = false;
        });
        let result = async {
            let mut builder = app
                .updater_builder()
                .target(target)
                .timeout(Duration::from_secs(20));
            builder = if proxy.is_empty() {
                builder.no_proxy()
            } else {
                builder.proxy(proxy.parse()?)
            };
            let updater = builder.build()?;
            let update = tokio::select! {
                _ = self.cancel.cancelled() => bail!("Update check cancelled"),
                result = updater.check() => result?,
            };
            let cached = if let Some(update) = &update {
                let package = Package::from_update(update, target);
                let root = self.root.clone();
                let pubkey = self.pubkey.clone();
                tauri::async_runtime::spawn_blocking(move || load_cache(&root, &package, &pubkey))
                    .await??
            } else {
                let directory = self.root.clone().join(target);
                tauri::async_runtime::spawn_blocking(move || clear_cache(&directory, None))
                    .await??;
                None
            };
            Ok::<_, anyhow::Error>(self.change(app, |state| {
                state.status = Status {
                    revision: state.status.revision,
                    phase: if cached.is_some() {
                        Phase::Ready
                    } else if update.is_some() {
                        Phase::Available
                    } else {
                        Phase::Idle
                    },
                    version: update.as_ref().map(|update| update.version.clone()),
                    body: update.as_ref().and_then(|update| update.body.clone()),
                    downloaded_bytes: cached.unwrap_or(0),
                    total_bytes: cached,
                    can_download: update.is_some() && cached.is_none(),
                    can_install: cached.is_some(),
                    ..Default::default()
                };
                state.update = update;
            }))
        }
        .await;
        Ok(match result {
            Ok(status) => status,
            Err(reason) => self.failed(app, reason),
        })
    }

    pub async fn download<R: tauri::Runtime>(
        &self,
        app: &tauri::AppHandle<R>,
        proxy: &str,
    ) -> Result<Status> {
        let Ok(_operation) = self.operation.try_lock() else {
            return Ok(self.status());
        };
        ensure!(!self.cancel.is_cancelled(), "Application is shutting down");
        if self.status().can_install {
            return Ok(self.status());
        }
        let mut update = self
            .state
            .lock()
            .unwrap()
            .update
            .clone()
            .context("Check for an update first")?;
        update.proxy = if proxy.is_empty() {
            None
        } else {
            Some(proxy.parse()?)
        };
        update.no_proxy = proxy.is_empty();
        update.timeout = Some(Duration::from_secs(30 * 60));
        self.change(app, |state| {
            state.status.phase = Phase::Downloading;
            state.status.error = None;
            state.status.downloaded_bytes = 0;
            state.status.total_bytes = None;
            state.status.can_download = false;
        });
        let result = async {
            let cancel = self.cancel.child_token();
            let mut oversized = false;
            let mut downloaded = 0u64;
            let mut last_event = Instant::now();
            let downloading = update.download(|bytes, total| {
                downloaded = downloaded.saturating_add(bytes as u64);
                if downloaded > MAX_PACKAGE_SIZE || total.is_some_and(|total| total > MAX_PACKAGE_SIZE) {
                    oversized = true;
                    cancel.cancel();
                    return;
                }
                {
                    let mut state = self.state.lock().unwrap();
                    state.status.downloaded_bytes = downloaded;
                    state.status.total_bytes = total.filter(|total| *total > 0);
                }
                if last_event.elapsed() >= Duration::from_millis(100) {
                    self.change(app, |_| {});
                    last_event = Instant::now();
                }
            }, || {
                self.change(app, |state| state.status.phase = Phase::Verifying);
            });
            let bytes = tokio::select! {
                biased;
                _ = cancel.cancelled() => bail!("Update download cancelled or package exceeded 1 GiB"),
                result = downloading => result?,
            };
            ensure!(!oversized && bytes.len() as u64 <= MAX_PACKAGE_SIZE, "Update package exceeds 1 GiB");
            let package = Package::from_update(&update, self.target.as_deref().unwrap());
            let root = self.root.clone();
            let length = bytes.len() as u64;
            tauri::async_runtime::spawn_blocking(move || save_cache(&root, &package, &bytes)).await??;
            ensure!(!self.cancel.is_cancelled(), "Update download cancelled");
            Ok::<_, anyhow::Error>(self.change(app, |state| {
                state.status.phase = Phase::Ready;
                state.status.downloaded_bytes = length;
                state.status.total_bytes = Some(length);
                state.status.can_install = true;
            }))
        }.await;
        Ok(match result {
            Ok(status) => status,
            Err(reason) => {
                self.change(app, |state| state.status.can_download = true);
                self.failed(app, reason)
            }
        })
    }

    pub async fn install(&self, app: &tauri::AppHandle, host: &super::Host) -> Result<()> {
        let _operation = self
            .operation
            .try_lock()
            .context("An update operation is already running")?;
        ensure!(!self.cancel.is_cancelled(), "Application is shutting down");
        ensure!(
            self.status().can_install && !self.status().restart_required,
            "Download and verify an update first"
        );
        let update = self
            .state
            .lock()
            .unwrap()
            .update
            .clone()
            .context("Check for an update first")?;
        let package = Package::from_update(&update, self.target.as_deref().unwrap());
        let result = async {
            let root = self.root.clone();
            let pubkey = self.pubkey.clone();
            let checked_package = package.clone();
            let bytes = match tauri::async_runtime::spawn_blocking(move || read_cache(&root, &checked_package, &pubkey)).await? {
                Ok(bytes) => bytes,
                Err(reason) => {
                    self.change(app, |state| { state.status.can_install = false; state.status.can_download = true; });
                    remove_cache(&package.directory(&self.root)?)?;
                    return Err(reason);
                }
            };
            #[cfg(target_os = "linux")]
            let executable = app.env().appimage.map(PathBuf::from).context("Run the AppImage to install application updates")?;
            #[cfg(not(target_os = "linux"))]
            let executable = tauri::utils::platform::current_exe()?;
            preflight(&executable, host.portable)?;
            #[cfg(windows)]
            let prepared = if host.portable {
                Some(super::portable_update::prepare(&executable, &bytes, &package, &self.pubkey)?)
            } else { None };
            ensure!(app.state::<super::startup::Initialization>().request_close(), "Application is shutting down");
            host.closing.store(true, std::sync::atomic::Ordering::Release);
            self.cancel.cancel();
            self.change(app, |state| {
                state.status.phase = Phase::Installing;
                state.status.error = None;
                state.status.can_install = false;
                state.status.can_download = false;
                state.status.restart_required = true;
            });
            host.mcp.shutdown().await;
            host.login_browser.stop(app, &host.core).await;
            if let Err(reason) = host.core.shutdown().await {
                app.state::<super::startup::Initialization>().closing.store(false, std::sync::atomic::Ordering::Release);
                return Err(reason.context("Could not save tasks. Close and reopen the application before retrying"));
            }
            host.shutdown_done.store(true, std::sync::atomic::Ordering::Release);
            huifa_core::backup::create(&host.data_dir, "before-update")
                .context("Update was not installed because the configuration backup failed. Tasks were saved; close and reopen the application before retrying")?;
            #[cfg(windows)]
            if let Some(prepared) = prepared {
                prepared.launch(&executable)?;
                app.exit(0);
                return Ok(());
            }
            update.install(bytes).context("Update installation failed. Tasks were saved; close and reopen the application before retrying")?;
            app.restart();
        }.await;
        if let Err(reason) = &result {
            self.failed(app, reason);
        }
        result
    }
}

pub fn target(portable: bool) -> Option<String> {
    let base = tauri_plugin_updater::target()?;
    if portable {
        return cfg!(windows).then(|| format!("{base}-portable"));
    }
    if cfg!(target_os = "macos") {
        let executable = tauri::utils::platform::current_exe().ok()?;
        if executable.parent()?.parent()?.parent()?.extension() != Some(std::ffi::OsStr::new("app"))
        {
            return None;
        }
    }
    if cfg!(target_os = "linux")
        && tauri::utils::platform::bundle_type() != Some(tauri::utils::config::BundleType::AppImage)
    {
        return None;
    }
    Some(base)
}

fn valid_target(target: &str) -> bool {
    matches!(
        target,
        "windows-x86_64"
            | "windows-aarch64"
            | "windows-x86_64-portable"
            | "windows-aarch64-portable"
            | "darwin-x86_64"
            | "darwin-aarch64"
            | "linux-x86_64"
            | "linux-aarch64"
    )
}

pub(crate) fn verify(bytes: &[u8], package: &Package, pubkey: &str) -> Result<()> {
    ensure!(
        !bytes.is_empty() && bytes.len() as u64 <= MAX_PACKAGE_SIZE,
        "Invalid update package size"
    );
    ensure!(
        package.signature.len() <= 16 * 1024,
        "Invalid update signature"
    );
    let key = String::from_utf8(STANDARD.decode(pubkey)?)?;
    let signature = String::from_utf8(STANDARD.decode(&package.signature)?)?;
    let key = PublicKey::decode(&key)?;
    let signature = Signature::decode(&signature)?;
    key.verify(bytes, &signature, true)
        .context("Update signature verification failed")?;
    if let Some(version) = signature
        .trusted_comment()
        .split('\t')
        .find_map(|field| field.strip_prefix("version:"))
    {
        ensure!(
            version.trim_start_matches('v') == package.version,
            "Signed update version does not match the release"
        );
    }
    Ok(())
}

fn checked_file(path: &Path, limit: u64) -> Result<Vec<u8>> {
    huifa_core::reject_links(path)?;
    let file = fs::metadata(path)?;
    ensure!(
        file.is_file() && file.len() <= limit,
        "Invalid update cache file"
    );
    let mut bytes = Vec::new();
    fs::File::open(path)?
        .take(limit + 1)
        .read_to_end(&mut bytes)?;
    ensure!(
        bytes.len() as u64 <= limit,
        "Update cache file exceeds its size limit"
    );
    Ok(bytes)
}

pub(crate) fn read_cache(root: &Path, package: &Package, pubkey: &str) -> Result<Vec<u8>> {
    let directory = package.directory(root)?;
    let cached: Package =
        serde_json::from_slice(&checked_file(&directory.join("package.json"), 32 * 1024)?)?;
    ensure!(
        &cached == package,
        "Update cache does not match the release"
    );
    let bytes = checked_file(&directory.join("package.bin"), MAX_PACKAGE_SIZE)?;
    verify(&bytes, package, pubkey)?;
    Ok(bytes)
}

fn load_cache(root: &Path, package: &Package, pubkey: &str) -> Result<Option<u64>> {
    let directory = package.directory(root)?;
    huifa_core::reject_links(&directory)?;
    if !directory.try_exists()? {
        return Ok(None);
    }
    match read_cache(root, package, pubkey) {
        Ok(bytes) => Ok(Some(bytes.len() as u64)),
        Err(_) => {
            remove_cache(&directory)?;
            Ok(None)
        }
    }
}

fn remove_cache(directory: &Path) -> Result<()> {
    huifa_core::reject_links(directory)?;
    if directory.try_exists()? {
        for entry in fs::read_dir(directory)? {
            let path = entry?.path();
            huifa_core::reject_links(&path)?;
            ensure!(
                fs::metadata(&path)?.is_file(),
                "Unexpected update cache directory"
            );
            fs::remove_file(path)?;
        }
        fs::remove_dir(directory)?;
    }
    Ok(())
}

fn save_cache(root: &Path, package: &Package, bytes: &[u8]) -> Result<()> {
    let destination = package.directory(root)?;
    let parent = destination.parent().unwrap();
    huifa_core::reject_links(parent)?;
    fs::create_dir_all(parent)?;
    let staging = tempfile::tempdir_in(parent)?;
    let metadata = serde_json::to_vec(package)?;
    for (name, bytes) in [
        ("package.json", metadata.as_slice()),
        ("package.bin", bytes),
    ] {
        let mut file = fs::File::create(staging.path().join(name))?;
        file.write_all(bytes)?;
        file.sync_all()?;
    }
    remove_cache(&destination)?;
    fs::rename(staging.path(), &destination)?;
    #[cfg(unix)]
    fs::File::open(parent)?.sync_all()?;
    clear_cache(parent, Some(&destination))?;
    Ok(())
}

fn clear_cache(parent: &Path, keep: Option<&Path>) -> Result<()> {
    huifa_core::reject_links(parent)?;
    if !parent.try_exists()? {
        return Ok(());
    }
    for entry in fs::read_dir(parent)? {
        let entry = entry?;
        let name = entry.file_name();
        if Some(entry.path().as_path()) != keep
            && name.to_str().is_some_and(|name| {
                name.len() == 64 && name.bytes().all(|byte| byte.is_ascii_hexdigit())
            })
        {
            remove_cache(&entry.path())?;
        }
    }
    Ok(())
}

fn preflight(executable: &Path, portable: bool) -> Result<()> {
    huifa_core::reject_links(executable)?;
    ensure!(
        fs::metadata(executable)?.is_file(),
        "Application executable is missing"
    );
    let destination = if cfg!(target_os = "macos") {
        tauri_plugin_updater::extract_path_from_executable(executable)?
    } else {
        executable.to_path_buf()
    };
    let directory = destination
        .parent()
        .context("Application directory is unavailable")?;
    if portable || cfg!(any(target_os = "linux", target_os = "macos")) {
        tempfile::NamedTempFile::new_in(directory).context(
            "The application folder is not writable. Move it to a writable folder before updating",
        )?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    };
    use tauri::Listener;
    use tokio::{
        io::{AsyncReadExt, AsyncWriteExt},
        net::TcpListener,
        sync::Semaphore,
    };

    const PAYLOAD: &[u8] = b"Huifa signed update fixture\n";
    const KEY: &str = include_str!("../tests/fixtures/update.pub");
    const SIGNATURE: &str = include_str!("../tests/fixtures/update.txt.sig");

    fn package() -> Package {
        Package {
            version: "0.6.0".into(),
            target: "windows-x86_64-portable".into(),
            url: "https://example.test/update.zip".into(),
            signature: SIGNATURE.trim().into(),
        }
    }

    struct Server {
        url: String,
        downloads: Arc<AtomicUsize>,
        permit: Arc<Semaphore>,
        task: tokio::task::JoinHandle<()>,
    }

    impl Drop for Server {
        fn drop(&mut self) {
            self.task.abort();
        }
    }

    impl Server {
        async fn start(payload: &[u8], blocked: bool) -> Result<Self> {
            let listener = TcpListener::bind("127.0.0.1:0").await?;
            let url = format!("http://{}", listener.local_addr()?);
            let downloads = Arc::new(AtomicUsize::new(0));
            let permit = Arc::new(Semaphore::new(if blocked { 0 } else { 20 }));
            let requests = downloads.clone();
            let permits = permit.clone();
            let data = payload.to_vec();
            let base = url.clone();
            let task = tokio::spawn(async move {
                while let Ok((mut stream, _)) = listener.accept().await {
                    let mut request = vec![0; 8192];
                    let Ok(length) = stream.read(&mut request).await else {
                        continue;
                    };
                    let download =
                        String::from_utf8_lossy(&request[..length]).starts_with("GET /package ");
                    let body = if download {
                        requests.fetch_add(1, Ordering::SeqCst);
                        let Ok(permit) = permits.acquire().await else {
                            break;
                        };
                        permit.forget();
                        data.clone()
                    } else {
                        serde_json::to_vec(&serde_json::json!({"version":"0.6.0","notes":"Test update","platforms":{"windows-x86_64-portable":{"url":format!("{base}/package"),"signature":SIGNATURE.trim()}}})).unwrap()
                    };
                    // No Content-Length exercises unknown-size update progress.
                    let _ = stream.write_all(b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Type: application/json\r\n\r\n").await;
                    let _ = stream.write_all(&body).await;
                    let _ = stream.shutdown().await;
                }
            });
            Ok(Self {
                url,
                downloads,
                permit,
                task,
            })
        }

        fn app(&self) -> tauri::App<tauri::test::MockRuntime> {
            let mut context = tauri::test::mock_context(tauri::test::noop_assets());
            context.config_mut().plugins.0.insert("updater".into(), serde_json::json!({"pubkey":KEY.trim(),"endpoints":[format!("{}/manifest",self.url)],"dangerousInsecureTransportProtocol":true}));
            tauri::test::mock_builder()
                .plugin(tauri_plugin_updater::Builder::new().build())
                .build(context)
                .unwrap()
        }
    }

    #[test]
    fn cached_packages_require_the_live_metadata_platform_and_original_signature() -> Result<()> {
        let root = tempfile::tempdir()?;
        let package = package();
        verify(PAYLOAD, &package, KEY.trim())?;
        assert!(verify(b"tampered update", &package, KEY.trim()).is_err());
        assert!(verify(PAYLOAD, &package, &public_key()?).is_err());
        save_cache(root.path(), &package, PAYLOAD)?;
        assert_eq!(read_cache(root.path(), &package, KEY.trim())?, PAYLOAD);
        let mut other = package.clone();
        other.target = "windows-aarch64-portable".into();
        assert_eq!(load_cache(root.path(), &other, KEY.trim())?, None);
        other = package.clone();
        other.url = "https://example.test/changed.zip".into();
        assert!(read_cache(root.path(), &other, KEY.trim()).is_err());
        fs::write(
            package.directory(root.path())?.join("package.bin"),
            b"tampered",
        )?;
        assert_eq!(load_cache(root.path(), &package, KEY.trim())?, None);
        assert!(!package.directory(root.path())?.exists());
        Ok(())
    }

    #[test]
    fn partial_cache_and_unsafe_platforms_cannot_be_installed_or_remove_unowned_files() -> Result<()>
    {
        let root = tempfile::tempdir()?;
        let package = package();
        fs::create_dir_all(root.path().join(&package.target).join("unrelated"))?;
        fs::write(
            root.path().join(&package.target).join("unrelated/keep.txt"),
            b"keep",
        )?;
        let directory = package.directory(root.path())?;
        fs::create_dir_all(&directory)?;
        fs::write(directory.join("package.bin"), PAYLOAD)?;
        assert!(read_cache(root.path(), &package, KEY.trim()).is_err());
        assert_eq!(load_cache(root.path(), &package, KEY.trim())?, None);
        let mut unsafe_package = package;
        unsafe_package.target = "../credentials".into();
        assert!(unsafe_package.directory(root.path()).is_err());
        clear_cache(&root.path().join("windows-x86_64-portable"), None)?;
        assert_eq!(
            fs::read(
                root.path()
                    .join("windows-x86_64-portable/unrelated/keep.txt")
            )?,
            b"keep"
        );
        Ok(())
    }

    #[tokio::test]
    async fn checks_downloads_verifies_and_restores_a_cache_without_redownloading() -> Result<()> {
        let server = Server::start(PAYLOAD, false).await?;
        let app = server.app();
        let directory = tempfile::tempdir()?;
        let updater = Updater::new(
            directory.path(),
            KEY.trim().into(),
            Some("windows-x86_64-portable".into()),
        );
        let phases = Arc::new(Mutex::new(Vec::<String>::new()));
        let events = phases.clone();
        app.listen("huifa-update", move |event| {
            let value: serde_json::Value = serde_json::from_str(event.payload()).unwrap();
            events
                .lock()
                .unwrap()
                .push(value["phase"].as_str().unwrap().into());
        });
        assert_eq!(
            updater.check(app.handle(), "").await?.phase,
            Phase::Available
        );
        assert_eq!(server.downloads.load(Ordering::SeqCst), 0);
        let downloaded = updater.download(app.handle(), "").await?;
        assert_eq!(downloaded.phase, Phase::Ready, "{:?}", downloaded.error);
        assert!(downloaded.can_install && !downloaded.can_download);
        assert!(
            phases
                .lock()
                .unwrap()
                .iter()
                .any(|phase| phase == "verifying")
        );
        let reopened = Updater::new(
            directory.path(),
            KEY.trim().into(),
            Some("windows-x86_64-portable".into()),
        );
        assert!(!reopened.status().can_install);
        assert_eq!(reopened.check(app.handle(), "").await?.phase, Phase::Ready);
        assert_eq!(server.downloads.load(Ordering::SeqCst), 1);
        Ok(())
    }

    #[tokio::test]
    async fn concurrent_downloads_share_one_transfer_and_closing_cancels_it() -> Result<()> {
        for cancel in [false, true] {
            let server = Server::start(PAYLOAD, true).await?;
            let app = server.app();
            let directory = tempfile::tempdir()?;
            let updater = Arc::new(Updater::new(
                directory.path(),
                KEY.trim().into(),
                Some("windows-x86_64-portable".into()),
            ));
            assert_eq!(
                updater.check(app.handle(), "").await?.phase,
                Phase::Available
            );
            let active = updater.clone();
            let handle = app.handle().clone();
            let transfer = tokio::spawn(async move { active.download(&handle, "").await });
            tokio::time::timeout(Duration::from_secs(5), async {
                while server.downloads.load(Ordering::SeqCst) == 0 {
                    tokio::task::yield_now().await;
                }
            })
            .await?;
            assert_eq!(
                updater.download(app.handle(), "").await?.phase,
                Phase::Downloading
            );
            assert_eq!(
                updater.check(app.handle(), "").await?.phase,
                Phase::Downloading
            );
            if cancel {
                updater.cancel();
            } else {
                server.permit.add_permits(1);
            }
            let completed = transfer.await??;
            assert_eq!(
                completed.phase,
                if cancel { Phase::Error } else { Phase::Ready }
            );
            assert_eq!(completed.can_install, !cancel);
            assert_eq!(server.downloads.load(Ordering::SeqCst), 1);
        }
        Ok(())
    }

    #[tokio::test]
    async fn failed_signature_never_creates_a_ready_cache_and_can_be_retried() -> Result<()> {
        let server = Server::start(b"changed after signing", false).await?;
        let app = server.app();
        let directory = tempfile::tempdir()?;
        let updater = Updater::new(
            directory.path(),
            KEY.trim().into(),
            Some("windows-x86_64-portable".into()),
        );
        updater.check(app.handle(), "").await?;
        for _ in 0..2 {
            let status = updater.download(app.handle(), "").await?;
            assert_eq!(status.phase, Phase::Error);
            assert!(!status.can_install && status.can_download);
            assert!(!updater.root.exists());
        }
        assert_eq!(server.downloads.load(Ordering::SeqCst), 2);
        Ok(())
    }
}

#[cfg(test)]
mod acceptance;
mod browser;
mod cover;
mod credentials;
mod db;
mod download;
pub(crate) mod model;
pub mod process;
mod processing;
mod tools;

use anyhow::{Context, Result, bail, ensure};
use db::Database;
pub use model::{AppConfig, DownloadOptions, Event};
use model::{Task, text, validate_url};
use serde_json::{Value, json};
use std::{
    collections::HashMap,
    path::PathBuf,
    sync::{
        Arc, Mutex, OnceLock, Weak,
        atomic::{AtomicBool, AtomicUsize, Ordering},
    },
};
use tokio::{
    sync::{Notify, Semaphore, broadcast},
    task::JoinHandle,
};
use tokio_util::sync::CancellationToken;

struct Runtime {
    cancel: CancellationToken,
    handle: JoinHandle<()>,
}

pub struct App {
    pub(crate) config: AppConfig,
    pub(crate) db: Database,
    pub(crate) credentials: credentials::CredentialStore,
    pub(crate) shutdown_token: CancellationToken,
    events: broadcast::Sender<Event>,
    myself: OnceLock<Weak<Self>>,
    runners: Mutex<HashMap<String, Runtime>>,
    closing: AtomicBool,
    pub(crate) network: Arc<Semaphore>,
    pub(crate) capture: Arc<Semaphore>,
    pub(crate) processor: Arc<Semaphore>,
    pub(crate) cover_state: crate::cover::CoverState,
    pub(crate) tool_update_lock: tokio::sync::Mutex<()>,
    active_commands: AtomicUsize,
    commands_done: Notify,
}

impl App {
    pub async fn open(mut config: AppConfig) -> Result<Arc<Self>> {
        std::fs::create_dir_all(&config.data_dir)?;
        std::fs::create_dir_all(&config.download_dir)?;
        config.data_dir = config.data_dir.canonicalize()?;
        config.download_dir = config.download_dir.canonicalize()?;
        if let Some(tool_dir) = &config.tool_dir {
            config.tool_dir = Some(tool_dir.canonicalize()?);
        }
        std::fs::create_dir_all(config.data_dir.join("tasks"))?;
        std::fs::create_dir_all(config.data_dir.join("assets"))?;
        let db = Database::open(&config.data_dir.join("rust-core.sqlite3"))?;
        let credentials = credentials::CredentialStore::open(&config.data_dir)?;
        let (events, _) = broadcast::channel(1024);
        let app = Arc::new(Self {
            config,
            db,
            credentials,
            shutdown_token: CancellationToken::new(),
            events,
            myself: OnceLock::new(),
            runners: Mutex::new(HashMap::new()),
            closing: AtomicBool::new(false),
            network: Arc::new(Semaphore::new(3)),
            capture: Arc::new(Semaphore::new(1)),
            processor: Arc::new(Semaphore::new(1)),
            cover_state: Default::default(),
            tool_update_lock: Default::default(),
            active_commands: AtomicUsize::new(0),
            commands_done: Notify::new(),
        });
        app.myself.set(Arc::downgrade(&app)).unwrap();
        for mut task in app.db.tasks()? {
            if matches!(
                task.status.as_str(),
                "queued" | "probing" | "downloading" | "waiting_processing" | "processing"
            ) {
                task.stage(
                    if task.manifest.is_some() {
                        "waiting_processing"
                    } else {
                        "queued"
                    },
                    if task.manifest.is_some() {
                        "waiting_processing"
                    } else {
                        "queued"
                    },
                    None,
                );
                app.save_task(&task)?;
                app.schedule(&task.id)?;
            }
        }
        Ok(app)
    }
    pub fn subscribe(&self) -> broadcast::Receiver<Event> {
        self.events.subscribe()
    }
    pub(crate) fn emit(&self, kind: &str, data: Value) {
        let _ = self.events.send(Event {
            kind: kind.into(),
            data,
        });
    }
    pub(crate) fn tool(&self, name: &str) -> Result<PathBuf> {
        tools::resolve(self, name)
    }
    pub(crate) fn task_tool(&self, task: &Task, name: &str) -> Result<PathBuf> {
        let path = task
            .tools
            .get(name)
            .with_context(|| format!("Task has no frozen {name} runtime"))?;
        ensure!(
            path.is_file(),
            "Frozen {name} runtime is missing; restore its saved tool version before retrying"
        );
        Ok(path.clone())
    }
    fn freeze_tools(&self) -> Result<HashMap<String, PathBuf>> {
        let mut tools = HashMap::new();
        for name in ["yt-dlp", "ffmpeg", "ffprobe"] {
            tools.insert(name.into(), self.tool(name)?.canonicalize()?);
        }
        if let Ok(path) = self.tool("deno") {
            tools.insert("deno".into(), path.canonicalize()?);
        }
        Ok(tools)
    }
    pub(crate) fn save_task(&self, task: &Task) -> Result<()> {
        self.db.save_task(task)?;
        self.emit("task.upsert", task.public());
        Ok(())
    }
    pub(crate) fn set_stage(
        &self,
        id: &str,
        status: &str,
        stage: &str,
        percent: Option<f64>,
    ) -> Result<()> {
        let mut task = self.db.task(id)?;
        task.stage(status, stage, percent);
        self.save_task(&task)
    }
    pub(crate) fn credential_snapshot(&self, options: &DownloadOptions) -> Result<Option<String>> {
        if options.cookie_id.is_empty() {
            Ok(None)
        } else {
            Ok(Some(self.credentials.get(&options.cookie_id)?))
        }
    }
    pub(crate) fn task_cookies(&self, task: &Task) -> Result<Option<String>> {
        task.cookie_snapshot
            .as_deref()
            .map(|key| {
                self.credentials
                    .secret(key)?
                    .context("Frozen cookie snapshot is missing")
            })
            .transpose()
    }
    pub(crate) fn runtime_options(&self, task: &Task) -> Result<DownloadOptions> {
        let mut options = task.options.clone();
        if let Some(key) = &task.proxy_snapshot {
            options.proxy = self
                .credentials
                .secret(key)?
                .context("Frozen proxy snapshot is missing")?;
        }
        Ok(options)
    }
    fn schedule(&self, id: &str) -> Result<()> {
        ensure!(
            !self.closing.load(Ordering::Acquire),
            "Application is shutting down"
        );
        let mut runners = self.runners.lock().unwrap();
        if runners.get(id).is_some_and(|r| !r.handle.is_finished()) {
            return Ok(());
        }
        let mut task = self.db.task(id)?;
        if task.tools.is_empty() {
            task.tools = self.freeze_tools()?;
            self.save_task(&task)?;
        }
        let app = self
            .myself
            .get()
            .and_then(Weak::upgrade)
            .context("Application is closed")?;
        let cancel = self.shutdown_token.child_token();
        let running_cancel = cancel.clone();
        let task_id = id.to_string();
        let handle = tokio::spawn(async move {
            let result = app.run_task(&task_id, &running_cancel).await;
            if let Err(error) = result
                && !running_cancel.is_cancelled()
                && let Ok(mut task) = app.db.task(&task_id)
            {
                task.status = if task.manifest.is_some() {
                    "processing_failed"
                } else {
                    "failed"
                }
                .into();
                task.speed = None;
                task.eta = None;
                task.error = Some(process::redact(&format!("{error:#}")));
                let _ = app.save_task(&task);
            }
        });
        runners.insert(id.into(), Runtime { cancel, handle });
        Ok(())
    }
    async fn stop_task(&self, id: &str, status: &str) -> Result<()> {
        let runtime = self.runners.lock().unwrap().remove(id);
        if let Some(runtime) = runtime {
            runtime.cancel.cancel();
            let _ = runtime.handle.await;
        }
        let mut task = self.db.task(id)?;
        if task.status == "completed" {
            return Ok(());
        }
        task.status = status.into();
        task.speed = None;
        task.eta = None;
        self.save_task(&task)
    }
    async fn resume(&self, id: &str, retry: bool) -> Result<Value> {
        let mut task = self.db.task(id)?;
        ensure!(
            if retry {
                matches!(
                    task.status.as_str(),
                    "failed" | "processing_failed" | "paused" | "cancelled"
                )
            } else {
                matches!(task.status.as_str(), "paused" | "cancelled")
            },
            "Task cannot be resumed in its current state"
        );
        task.error = None;
        task.stage(
            if task.manifest.is_some() {
                "waiting_processing"
            } else {
                "queued"
            },
            if task.manifest.is_some() {
                "waiting_processing"
            } else {
                "queued"
            },
            None,
        );
        self.save_task(&task)?;
        self.schedule(id)?;
        Ok(task.public())
    }
    pub async fn command(&self, command: &str, args: Value) -> Result<Value> {
        let _active = CommandGuard::begin(self)?;
        match command {
            "snapshot" => Ok(
                json!({"version":env!("CARGO_PKG_VERSION"),"tasks":self.db.tasks()?.iter().map(Task::public).collect::<Vec<_>>(),"media":self.db.media()?.iter().map(model::Media::public).collect::<Vec<_>>(),"settings":self.public_settings()?,"tools":tools::check(self).await?}),
            ),
            "tasks.add" => {
                let urls = args["urls"].as_array().context("urls must be an array")?;
                ensure!(
                    !urls.is_empty() && urls.len() <= 1000,
                    "Add between 1 and 1000 URLs"
                );
                let raw_options = args.get("options").cloned().unwrap_or(json!({}));
                let mut options = DownloadOptions::parse(raw_options.clone())?;
                if raw_options.get("proxy").is_none() {
                    options.proxy = self
                        .credentials
                        .secret("downloadProxy")?
                        .unwrap_or_default();
                }
                let cookies = self.credential_snapshot(&options)?;
                let frozen_tools = self.freeze_tools()?;
                let mut validated = Vec::new();
                for url in urls {
                    let url = url.as_str().context("URL must be text")?;
                    validate_url(url)?;
                    validated.push(url.to_string());
                }
                if options.collection_mode == "all" {
                    let mut expanded = Vec::new();
                    for url in validated {
                        let collection =
                            download::collection(self, &url, &options, cookies.as_deref()).await?;
                        for entry in collection["entries"]
                            .as_array()
                            .context("Collection has no entries")?
                        {
                            let url = text(entry, "url");
                            validate_url(url)?;
                            expanded.push(url.into());
                        }
                    }
                    ensure!(
                        !expanded.is_empty() && expanded.len() <= 10000,
                        "Collection has no matching entries or exceeds 10000 entries"
                    );
                    validated = expanded;
                }
                let mut ids = Vec::new();
                for url in validated {
                    let id = uuid::Uuid::new_v4().to_string();
                    let cookie_snapshot = if let Some(cookie) = &cookies {
                        let key = format!("task-cookie:{id}");
                        self.credentials.put_secret(&key, cookie)?;
                        Some(key)
                    } else {
                        None
                    };
                    let proxy_snapshot = if !options.proxy.is_empty() {
                        let key = format!("task-proxy:{id}");
                        self.credentials.put_secret(&key, &options.proxy)?;
                        Some(key)
                    } else {
                        None
                    };
                    let mut frozen_options = options.clone();
                    frozen_options.proxy.clear();
                    let task = Task {
                        id,
                        url,
                        title: "Waiting for metadata".into(),
                        status: "queued".into(),
                        stage: "queued".into(),
                        progress: 0.0,
                        stage_progress: None,
                        stage_indeterminate: true,
                        speed: None,
                        eta: None,
                        error: None,
                        created_at: chrono::Utc::now().to_rfc3339(),
                        options: frozen_options,
                        output_path: None,
                        cookie_snapshot,
                        proxy_snapshot,
                        manifest: None,
                        publication: None,
                        conversion: false,
                        tools: frozen_tools.clone(),
                    };
                    self.db.insert_task(&task)?;
                    self.emit("task.upsert", task.public());
                    ids.push(task.id.clone());
                    self.schedule(&task.id)?;
                }
                Ok(json!(ids))
            }
            "tasks.pause" | "tasks.cancel" => {
                let id = required_id(&args)?;
                self.stop_task(
                    id,
                    if command == "tasks.pause" {
                        "paused"
                    } else {
                        "cancelled"
                    },
                )
                .await?;
                Ok(self.db.task(id)?.public())
            }
            "tasks.resume" | "tasks.retry" => {
                self.resume(required_id(&args)?, command == "tasks.retry")
                    .await
            }
            "tasks.pauseAll" => {
                for task in self.db.tasks()? {
                    if matches!(
                        task.status.as_str(),
                        "queued" | "probing" | "downloading" | "waiting_processing" | "processing"
                    ) {
                        self.stop_task(&task.id, "paused").await?;
                    }
                }
                Ok(json!(true))
            }
            "tasks.resumeAll" => {
                for task in self.db.tasks()? {
                    if task.status == "paused" {
                        self.resume(&task.id, false).await?;
                    }
                }
                Ok(json!(true))
            }
            "tasks.delete" => {
                let id = required_id(&args)?;
                self.stop_task(id, "cancelled").await?;
                let task = self.db.task(id)?;
                let delete_files = args["deleteFiles"].as_bool().unwrap_or(false);
                if delete_files {
                    if let Some(media) = self.db.media_for_task(id)? {
                        self.remove_owned(&media.path)?;
                        for asset in media.assets {
                            self.remove_owned(&asset)?;
                        }
                    }
                    let workspace = self.config.data_dir.join("tasks").join(id);
                    if workspace.exists() {
                        std::fs::remove_dir_all(workspace)?;
                    }
                }
                self.db.remove_task(&task.id, delete_files)?;
                self.emit("task.remove", json!({"id":id}));
                Ok(json!(true))
            }
            "tasks.convert" => {
                processing::queue_conversion(self, required_id(&args)?, text(&args, "encoder"))
                    .await
            }
            "probe.video" | "probe.collection" => {
                let url = text(&args, "url");
                validate_url(url)?;
                let raw_options = args.get("options").cloned().unwrap_or(json!({}));
                let mut options = DownloadOptions::parse(raw_options.clone())?;
                if raw_options.get("proxy").is_none() {
                    options.proxy = self
                        .credentials
                        .secret("downloadProxy")?
                        .unwrap_or_default();
                }
                let cookies = self.credential_snapshot(&options)?;
                if command == "probe.video" {
                    download::public_probe(self, url, &options, cookies.as_deref()).await
                } else {
                    download::collection(self, url, &options, cookies.as_deref()).await
                }
            }
            "settings.save" => self.save_settings(args["settings"].clone()),
            "tools.check" => {
                let value = tools::check(self).await?;
                self.emit("tools.updated", value.clone());
                Ok(value)
            }
            "tools.update" => tools::update(self, text(&args, "name")).await,
            "cookies.import" => {
                let value = self
                    .credentials
                    .put(text(&args, "name"), text(&args, "text"))?;
                Ok(value)
            }
            "cookies.list" => self.credentials.list(),
            "cookies.delete" => {
                self.credentials.delete(required_id(&args)?)?;
                Ok(json!(true))
            }
            "cookies.browser" => browser::import_browser(self, &args).await,
            "cover.preview" | "cover.export" | "cover.generate" | "cover.cancel" => {
                cover::command(self, command, args).await
            }
            "media.list" => Ok(json!(
                self.db
                    .media()?
                    .iter()
                    .map(model::Media::public)
                    .collect::<Vec<_>>()
            )),
            "media.file" => {
                let media = self.db.media_by_id(required_id(&args)?)?;
                let kind = text(&args, "kind");
                let path = if kind.is_empty() || kind == "media" {
                    media.path
                } else {
                    let assets = if kind == "cover" {
                        media.assets.into_iter().rev().collect::<Vec<_>>()
                    } else {
                        media.assets
                    };
                    assets
                        .into_iter()
                        .find(|p| match kind {
                            "cover" | "originalCover" => {
                                matches!(tools::mime(p), "image/png" | "image/jpeg" | "image/webp")
                            }
                            "thumbnail" => {
                                matches!(tools::mime(p), "image/png" | "image/jpeg" | "image/webp")
                            }
                            "description" => p.extension().is_some_and(|e| e == "txt"),
                            "info" => p.extension().is_some_and(|e| e == "json"),
                            "subtitle" => p.extension().is_some_and(|e| {
                                matches!(e.to_str(), Some("vtt" | "srt" | "ass" | "lrc"))
                            }),
                            "chapter" => matches!(
                                tools::mime(p),
                                "video/mp4" | "video/x-matroska" | "video/webm"
                            ),
                            _ => false,
                        })
                        .context("Requested media asset does not exist")?
                };
                let canonical = self.owned_path(&path)?;
                Ok(json!({"path":canonical,"mimeType":tools::mime(&canonical)}))
            }
            _ => bail!("Unknown command: {command}"),
        }
    }
    pub(crate) fn owned_path(&self, path: &std::path::Path) -> Result<PathBuf> {
        reject_links(path)?;
        let canonical = path.canonicalize()?;
        ensure!(
            canonical.starts_with(&self.config.download_dir)
                || canonical.starts_with(self.config.data_dir.join("assets")),
            "Media file is outside application-owned storage"
        );
        ensure!(canonical.is_file(), "Media file does not exist");
        Ok(canonical)
    }
    fn remove_owned(&self, path: &std::path::Path) -> Result<()> {
        if path.exists() {
            std::fs::remove_file(self.owned_path(path)?)?;
        }
        Ok(())
    }
    fn public_settings(&self) -> Result<Value> {
        let mut value = self.db.settings()?;
        if let Some(object) = value.as_object_mut() {
            object.remove("apiKey");
            object.remove("aiApiKey");
            object.remove("proxyPassword");
            object.insert(
                "hasAiApiKey".into(),
                json!(
                    self.credentials
                        .secret("aiApiKey")?
                        .is_some_and(|v| !v.is_empty())
                ),
            );
            object.insert(
                "hasProxy".into(),
                json!(
                    self.credentials
                        .secret("downloadProxy")?
                        .is_some_and(|v| !v.is_empty())
                ),
            );
            if let Some(ai) = object.get_mut("ai").and_then(Value::as_object_mut) {
                ai.remove("apiKey");
            }
            if let Some(options) = object
                .get_mut("downloadOptions")
                .and_then(Value::as_object_mut)
            {
                options.remove("proxy");
                options.remove("processingTempDir");
            }
        }
        Ok(value)
    }
    fn save_settings(&self, mut value: Value) -> Result<Value> {
        let object = value
            .as_object_mut()
            .context("settings must be an object")?;
        if let Some(options) = object.get("downloadOptions") {
            DownloadOptions::parse(options.clone())?;
        }
        if let Some(options) = object
            .get_mut("downloadOptions")
            .and_then(Value::as_object_mut)
            && let Some(proxy) = options.remove("proxy")
        {
            self.credentials.put_secret(
                "downloadProxy",
                proxy.as_str().context("Proxy must be text")?,
            )?;
        }
        for key in ["apiKey", "aiApiKey", "proxyPassword"] {
            if let Some(secret) = object.remove(key) {
                let secret = secret.as_str().context("Secret must be text")?;
                self.credentials.put_secret(key, secret)?;
            }
        }
        if let Some(ai) = object.get_mut("ai").and_then(Value::as_object_mut)
            && let Some(secret) = ai.remove("apiKey")
        {
            self.credentials
                .put_secret("aiApiKey", secret.as_str().context("AI key must be text")?)?;
        }
        self.db.save_settings(&value)?;
        let public = self.public_settings()?;
        self.emit("settings.updated", public.clone());
        Ok(public)
    }
    pub async fn shutdown(&self) -> Result<()> {
        self.closing.store(true, Ordering::Release);
        self.shutdown_token.cancel();
        loop {
            let notified = self.commands_done.notified();
            if self.active_commands.load(Ordering::Acquire) == 0 {
                break;
            }
            notified.await;
        }
        let runners = std::mem::take(&mut *self.runners.lock().unwrap());
        for (_, runtime) in runners {
            runtime.cancel.cancel();
            let _ = runtime.handle.await;
        }
        for mut task in self.db.tasks()? {
            if matches!(
                task.status.as_str(),
                "probing" | "downloading" | "processing"
            ) {
                task.stage(
                    if task.manifest.is_some() {
                        "waiting_processing"
                    } else {
                        "queued"
                    },
                    if task.manifest.is_some() {
                        "waiting_processing"
                    } else {
                        "queued"
                    },
                    None,
                );
                self.save_task(&task)?;
            }
        }
        self.db.checkpoint()?;
        Ok(())
    }
}

fn required_id(args: &Value) -> Result<&str> {
    let id = text(args, "id");
    ensure!(!id.is_empty(), "An ID is required");
    Ok(id)
}

struct CommandGuard<'a>(&'a App);
impl<'a> CommandGuard<'a> {
    fn begin(app: &'a App) -> Result<Self> {
        app.active_commands.fetch_add(1, Ordering::AcqRel);
        let guard = Self(app);
        ensure!(
            !app.closing.load(Ordering::Acquire),
            "Application is shutting down"
        );
        Ok(guard)
    }
}
impl Drop for CommandGuard<'_> {
    fn drop(&mut self) {
        if self.0.active_commands.fetch_sub(1, Ordering::AcqRel) == 1 {
            self.0.commands_done.notify_waiters();
        }
    }
}

pub(crate) fn reject_links(path: &std::path::Path) -> Result<()> {
    let mut current = PathBuf::new();
    for component in path.components() {
        current.push(component);
        if !matches!(component, std::path::Component::Normal(_)) {
            continue;
        }
        let meta = match std::fs::symlink_metadata(&current) {
            Ok(meta) => meta,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
            Err(error) => {
                return Err(error)
                    .with_context(|| format!("Cannot validate owned path {}", current.display()));
            }
        };
        ensure!(
            !meta.file_type().is_symlink(),
            "Symbolic links cannot be used as owned files"
        );
        #[cfg(windows)]
        {
            use std::os::windows::fs::MetadataExt;
            ensure!(
                meta.file_attributes() & 0x400 == 0,
                "Reparse points cannot be used as owned files"
            );
        }
    }
    Ok(())
}

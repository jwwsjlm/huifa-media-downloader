#[cfg(test)]
mod acceptance;
pub mod access;
mod assets;
pub mod browser;
mod cover;
mod credentials;
mod db;
mod download;
pub(crate) mod model;
#[cfg(test)]
mod performance;
pub mod process;
mod processing;
pub mod releases;
pub mod research;
mod thumbnail;
mod tools;
mod workflow;

use anyhow::{Context, Result, bail, ensure};
use db::Database;
pub use model::{AppConfig, DownloadOptions, Event, redact_url};
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
    pub access: access::Access,
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
    pub(crate) research_state: research::ResearchState,
    pub(crate) tool_update_lock: tokio::sync::Mutex<()>,
    pub(crate) publication_lock: tokio::sync::Mutex<()>,
    task_add_lock: tokio::sync::Mutex<()>,
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
        let access = access::Access::open(&config.data_dir)?;
        let (events, _) = broadcast::channel(1024);
        let app = Arc::new(Self {
            access,
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
            research_state: Default::default(),
            tool_update_lock: Default::default(),
            publication_lock: Default::default(),
            task_add_lock: Default::default(),
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
    pub(crate) async fn task_tool(&self, task: &Task, name: &str) -> Result<PathBuf> {
        let path = task
            .tools
            .get(name)
            .with_context(|| format!("Task has no frozen {name} runtime"))?
            .clone();
        let app = self
            .myself
            .get()
            .and_then(Weak::upgrade)
            .context("Application is closed")?;
        let name = name.to_string();
        // Hash every byte before use without occupying a Tokio runtime worker.
        tokio::task::spawn_blocking(move || {
            ensure!(
                path.is_file(),
                "Frozen {name} runtime is missing; restore its saved tool version before retrying"
            );
            tools::validate_frozen(&app, &path, &name)?;
            Ok(path)
        })
        .await
        .context("Runtime verification worker stopped")?
    }
    async fn freeze_tools(&self) -> Result<HashMap<String, PathBuf>> {
        let _guard = tokio::select! {
            guard=self.tool_update_lock.lock()=>guard,
            _=self.shutdown_token.cancelled()=>bail!("Runtime snapshot interrupted"),
        };
        let app = self
            .myself
            .get()
            .and_then(Weak::upgrade)
            .context("Application is closed")?;
        tokio::task::spawn_blocking(move || tools::freeze(&app))
            .await
            .context("Runtime snapshot worker stopped")?
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
    pub(crate) fn credential_snapshot(
        &self,
        options: &DownloadOptions,
        url: &str,
    ) -> Result<Option<String>> {
        match options.cookie_id.as_str() {
            "" => Ok(None),
            "auto" => self.credentials.matching(url),
            id => Ok(Some(self.credentials.get(id)?)),
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
        if matches!(task.status.as_str(), "completed" | "retained") {
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
                let _adding = self.task_add_lock.lock().await;
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
                let mut frozen_tools = None;
                let mut validated = Vec::new();
                for url in urls {
                    let url = url.as_str().context("URL must be text")?;
                    validate_url(url)?;
                    validated.push(url.to_string());
                }
                if options.collection_mode == "all" {
                    let mut expanded = Vec::new();
                    for url in validated {
                        let cookies = self.credential_snapshot(&options, &url)?;
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
                let mut ids: Vec<String> = Vec::new();
                let mut results = Vec::new();
                for url in validated {
                    let key = download_key(&url)?;
                    if ids.iter().any(|id| {
                        self.db
                            .task(id)
                            .is_ok_and(|task| download_key(&task.url).ok().as_ref() == Some(&key))
                    }) {
                        continue;
                    }
                    let matches = self
                        .db
                        .tasks_including_retained()?
                        .into_iter()
                        .filter(|task| {
                            task.kind == "media"
                                && !task.conversion
                                && download_key(&task.url).ok().as_ref() == Some(&key)
                        })
                        .collect::<Vec<_>>();
                    let previous = matches
                        .iter()
                        .find(|task| {
                            matches!(
                                task.status.as_str(),
                                "queued"
                                    | "probing"
                                    | "downloading"
                                    | "waiting_processing"
                                    | "processing"
                                    | "paused"
                            )
                        })
                        .or_else(|| matches.first())
                        .cloned();
                    if let Some(task) = &previous
                        && matches!(
                            task.status.as_str(),
                            "queued"
                                | "probing"
                                | "downloading"
                                | "waiting_processing"
                                | "processing"
                                | "paused"
                        )
                    {
                        ids.push(task.id.clone());
                        results
                            .push(json!({"id":task.id,"status":task.status,"action":"existing"}));
                        continue;
                    }
                    if frozen_tools.is_none() {
                        frozen_tools = Some(self.freeze_tools().await?);
                    }
                    if let Some(task) = &previous {
                        self.stop_task(&task.id, "cancelled").await?;
                        processing::prepare_redownload(self, task).await?;
                        remove_task_directory(&self.config.data_dir.join("tasks").join(&task.id))?;
                        if !task.options.processing_temp_dir.is_empty() {
                            remove_task_directory(&processing::workspace(self, task))?;
                        }
                    }
                    let cookies = self.credential_snapshot(&options, &url)?;
                    let id = previous
                        .as_ref()
                        .map(|task| task.id.clone())
                        .unwrap_or_else(|| uuid::Uuid::new_v4().to_string());
                    self.credentials
                        .delete_secret(&format!("task-cookie:{id}"))?;
                    self.credentials
                        .delete_secret(&format!("task-proxy:{id}"))?;
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
                        kind: "media".into(),
                        files: vec![],
                        id,
                        url,
                        title: "Waiting for metadata".into(),
                        thumbnail: None,
                        status: "queued".into(),
                        stage: "queued".into(),
                        steps: vec![],
                        step_index: None,
                        progress: 0.0,
                        stage_progress: None,
                        stage_indeterminate: true,
                        speed: None,
                        eta: None,
                        error: None,
                        warnings: vec![],
                        created_at: chrono::Utc::now().to_rfc3339(),
                        options: frozen_options,
                        output_path: previous.as_ref().and_then(|task| task.output_path.clone()),
                        previous_outputs: previous
                            .as_ref()
                            .map(|task| task.previous_outputs.clone())
                            .unwrap_or_default(),
                        cookie_snapshot,
                        proxy_snapshot,
                        manifest: None,
                        publication: None,
                        conversion: false,
                        tools: frozen_tools.as_ref().unwrap().clone(),
                    };
                    if previous.is_some() {
                        self.db.save_task(&task)?;
                    } else {
                        self.db.insert_task(&task)?;
                    }
                    self.emit("task.upsert", task.public());
                    ids.push(task.id.clone());
                    results.push(json!({"id":task.id,"status":task.status,"action":if previous.is_some() {"restarted"} else {"added"}}));
                    self.schedule(&task.id)?;
                }
                Ok(if args["report"].as_bool() == Some(true) {
                    json!({"ids":ids,"results":results})
                } else {
                    json!(ids)
                })
            }
            "tasks.pause" | "tasks.cancel" => {
                let _adding = self.task_add_lock.lock().await;
                let id = required_id(&args)?;
                ensure!(
                    self.db.task(id)?.status != "retained",
                    "Task is no longer in the queue"
                );
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
                let _adding = self.task_add_lock.lock().await;
                self.resume(required_id(&args)?, command == "tasks.retry")
                    .await
            }
            "tasks.pauseAll" => {
                let _adding = self.task_add_lock.lock().await;
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
                let _adding = self.task_add_lock.lock().await;
                for task in self.db.tasks()? {
                    if task.status == "paused" {
                        self.resume(&task.id, false).await?;
                    }
                }
                Ok(json!(true))
            }
            "tasks.delete" => {
                let _adding = self.task_add_lock.lock().await;
                let id = required_id(&args)?;
                self.stop_task(id, "cancelled").await?;
                processing::prepare_task_deletion(self, id).await?;
                let task = self.db.task(id)?;
                let delete_files = args["deleteFiles"].as_bool().unwrap_or(false);
                let media = if delete_files {
                    self.db.media_for_task(id)?
                } else {
                    None
                };
                if delete_files {
                    if let Some(media) = &media {
                        self.remove_owned(&media.path)?;
                        for asset in &media.assets {
                            self.remove_owned(asset)?;
                        }
                    }
                    if task.kind == "assets"
                        && let Some(publication) = &task.publication
                    {
                        self.remove_owned(&publication.target)?;
                        for auxiliary in &publication.auxiliary {
                            self.remove_owned(&auxiliary.target)?;
                        }
                    }
                    for path in &task.previous_outputs {
                        self.remove_owned(path)?;
                    }
                    if !task.options.processing_temp_dir.is_empty() {
                        remove_task_directory(&processing::workspace(self, &task))?;
                    }
                    remove_task_directory(&self.config.data_dir.join("tasks").join(id))?;
                }
                self.credentials
                    .delete_secret(&format!("task-cookie:{id}"))?;
                self.credentials
                    .delete_secret(&format!("task-proxy:{id}"))?;
                self.db.remove_task(&task, delete_files)?;
                if let Some(media) = media {
                    self.emit("media.remove", json!({"id":media.id}));
                }
                self.emit("task.remove", json!({"id":id}));
                Ok(json!(true))
            }
            "tasks.convert" => {
                if let Some(options) = args.get("options") {
                    processing::queue_conversion_options_with_strategy(
                        self,
                        required_id(&args)?,
                        options,
                        text(&args, "strategy"),
                    )
                    .await
                } else {
                    processing::queue_conversion(self, required_id(&args)?, text(&args, "encoder"))
                        .await
                }
            }
            "tasks.convertPreview" => {
                processing::preview_conversion(
                    self,
                    required_id(&args)?,
                    &args["options"],
                    text(&args, "strategy"),
                )
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
                let cookies = self.credential_snapshot(&options, url)?;
                if command == "probe.video" {
                    download::public_probe(self, url, &options, cookies.as_deref()).await
                } else {
                    download::collection(self, url, &options, cookies.as_deref()).await
                }
            }
            "settings.save" => self.save_settings(args["settings"].clone()),
            "settings.preferences" => {
                let preferences = args["preferences"]
                    .as_object()
                    .context("preferences must be an object")?;
                ensure!(
                    preferences.len() == 1 && preferences.contains_key("deleteMediaOnTaskDelete"),
                    "Unknown preference"
                );
                ensure!(
                    preferences["deleteMediaOnTaskDelete"].is_boolean(),
                    "deleteMediaOnTaskDelete must be a boolean"
                );
                self.db.save_ui_preferences(&args["preferences"])?;
                let settings = self.public_settings()?;
                self.emit("settings.updated", settings.clone());
                Ok(settings)
            }
            "access.status" => Ok(self.access.status()),
            "access.change_password" => {
                let app = self
                    .myself
                    .get()
                    .and_then(Weak::upgrade)
                    .context("Application is closed")?;
                let current = text(&args, "currentPassword").to_string();
                let new = text(&args, "newPassword").to_string();
                tokio::task::spawn_blocking(move || app.access.change_password(&current, &new))
                    .await?
            }
            "tools.check" => {
                let value = tools::check(self).await?;
                self.emit("tools.updated", value.clone());
                Ok(value)
            }
            "tools.update" => tools::update(self, &args).await,
            "tools.latest" => tools::latest(self, &args).await,
            "app.release_status" => releases::status().await,
            "cookies.import" => {
                let value = self
                    .credentials
                    .put(text(&args, "name"), text(&args, "text"))?;
                Ok(value)
            }
            "cookies.list" => self.credentials.list(),
            "cookies.status" => self.cookie_status(args.get("url").and_then(Value::as_str)),
            "ai.models" => cover::models(self, &args).await,
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
                let id = required_id(&args)?;
                let kind = text(&args, "kind");
                if kind == "taskThumbnail" {
                    let task = self.db.task(id)?;
                    let path = task.thumbnail.context("Task thumbnail is not available")?;
                    reject_links(&path)?;
                    let canonical = path.canonicalize()?;
                    let directory = self
                        .config
                        .data_dir
                        .join("tasks")
                        .join(id)
                        .join("preview")
                        .canonicalize()?;
                    ensure!(
                        canonical.starts_with(directory),
                        "Task thumbnail is outside its preview directory"
                    );
                    let mime = tools::mime(&canonical);
                    ensure!(
                        matches!(mime, "image/jpeg" | "image/png" | "image/webp"),
                        "Unsupported task thumbnail type"
                    );
                    let metadata = std::fs::metadata(&canonical)?;
                    ensure!(
                        metadata.is_file() && metadata.len() <= 8 * 1024 * 1024,
                        "Task thumbnail is missing or too large"
                    );
                    return Ok(json!({"path":canonical,"mimeType":mime}));
                }
                let media = self.db.media_by_id(id)?;
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
    pub fn task_copy_value(&self, id: &str, kind: &str) -> Result<String> {
        let task = self.db.task(id)?;
        match kind {
            "title" => Ok(task.title),
            "source" => Ok(task.url),
            "path" => {
                let media = self
                    .db
                    .media_for_task(id)?
                    .context("Result file is not available")?;
                self.owned_path(&media.path)?;
                Ok(media.path.to_string_lossy().into_owned())
            }
            _ => bail!("Unknown copy action"),
        }
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
                options.insert("cookieId".into(), json!("auto"));
                if options
                    .get("fragmentConcurrent")
                    .and_then(Value::as_u64)
                    .is_some_and(|value| value > 8)
                {
                    options.insert("fragmentConcurrent".into(), json!(8));
                }
            }
        }
        Ok(value)
    }
    pub fn import_login_cookies(&self, name: &str, text: &str) -> Result<Value> {
        let mut settings = self.public_settings()?;
        let options = settings
            .as_object_mut()
            .context("Settings must be an object")?
            .entry("downloadOptions")
            .or_insert_with(|| json!({}))
            .as_object_mut()
            .context("Download settings must be an object")?;
        let cookie = self.credentials.put(name, text)?;
        let id = cookie["id"].as_str().context("Missing cookie ID")?;
        options.insert("cookieId".into(), json!("auto"));
        if let Err(reason) = self.db.activate_cookie("auto") {
            let _ = self.credentials.delete(id);
            return Err(reason);
        }
        self.emit("settings.updated", settings.clone());
        Ok(json!({"cookie":cookie,"settings":settings}))
    }

    pub fn read_cookie(&self, id: &str) -> Result<String> {
        self.credentials.get(id)
    }

    pub fn cookie_status(&self, url: Option<&str>) -> Result<Value> {
        let records = self.credentials.list()?;
        let sites: Vec<_> = records.as_array().unwrap().iter().map(|cookie| json!({"domains":cookie["domains"],"count":cookie["count"],"updatedAt":cookie["updatedAt"]})).collect();
        let matching = url
            .map(|url| {
                validate_url(url)?;
                self.credentials
                    .matching(url)
                    .map(|cookie| cookie.is_some())
            })
            .transpose()?;
        Ok(json!({"sites":sites,"hasMatchingCookies":matching}))
    }

    pub fn sync_login_cookies(
        &self,
        profile: &str,
        name: &str,
        text: Option<&str>,
    ) -> Result<Option<Value>> {
        let Some((cookie, created)) = self.credentials.sync_browser(profile, name, text)? else {
            return Ok(None);
        };
        if created {
            if let Err(reason) = self.db.activate_cookie("auto") {
                let _ = self
                    .credentials
                    .delete(cookie["id"].as_str().context("Missing cookie ID")?);
                return Err(reason);
            }
            self.emit("settings.updated", self.public_settings()?);
        }
        Ok(Some(
            json!({"cookie":cookie,"settings":self.public_settings()?}),
        ))
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

fn download_key(value: &str) -> Result<String> {
    let mut url = url::Url::parse(value)?;
    url.set_fragment(None);
    let host = url.host_str().unwrap_or("");
    let video = if matches!(
        host,
        "youtube.com" | "www.youtube.com" | "m.youtube.com" | "music.youtube.com"
    ) {
        url.query_pairs()
            .find(|(key, _)| key == "v")
            .map(|(_, value)| value.into_owned())
            .or_else(|| {
                url.path()
                    .strip_prefix("/shorts/")
                    .or_else(|| url.path().strip_prefix("/live/"))
                    .map(|value| value.trim_end_matches('/').to_string())
            })
    } else if host == "youtu.be" {
        Some(url.path().trim_matches('/').to_string())
    } else {
        None
    };
    if let Some(video) = video.filter(|value| !value.is_empty()) {
        return Ok(format!("youtube:{video}"));
    }
    Ok(url.to_string())
}

fn remove_task_directory(path: &std::path::Path) -> Result<()> {
    match std::fs::symlink_metadata(path) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        metadata => {
            ensure!(metadata?.is_dir(), "Task workspace is not a directory");
        }
    }
    reject_links(path)?;
    let parent = path
        .parent()
        .context("Task workspace has no parent")?
        .canonicalize()?;
    let canonical = path.canonicalize()?;
    ensure!(
        canonical.parent() == Some(parent.as_path()),
        "Task workspace resolved outside its parent"
    );
    let name = path
        .file_name()
        .and_then(|name| name.to_str())
        .context("Invalid task workspace")?;
    let id = name.strip_prefix("HuifaRust-").unwrap_or(name);
    ensure!(
        uuid::Uuid::parse_str(id)?.to_string() == id,
        "Invalid task workspace ID"
    );
    std::fs::remove_dir_all(canonical)?;
    Ok(())
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

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn saved_cookie_view_reads_only_the_requested_identity() -> Result<()> {
        let root = tempfile::tempdir()?;
        let app = App::open(AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("output"),
            tool_dir: None,
        })
        .await?;
        let text = "# Netscape HTTP Cookie File\n#HttpOnly_.example.com\tTRUE\t/\tTRUE\t0\tSID\tprivate-value\n";
        let saved = app.import_login_cookies("Saved login", text)?;
        let id = saved["cookie"]["id"].as_str().unwrap();
        let before = app.public_settings()?;
        assert_eq!(app.read_cookie(id)?, text);
        assert_eq!(app.public_settings()?, before);
        assert!(
            !app.command("cookies.list", json!({}))
                .await?
                .to_string()
                .contains("private-value")
        );
        assert!(app.command("cookies.read", json!({"id":id})).await.is_err());
        assert!(app.read_cookie("../.credentials.key").is_err());
        assert!(app.read_cookie("secret-aiApiKey").is_err());
        app.command("cookies.delete", json!({"id":id})).await?;
        assert!(app.read_cookie(id).is_err());
        app.shutdown().await?;
        Ok(())
    }

    #[test]
    fn workspace_cleanup_rejects_other_names() -> Result<()> {
        let root = tempfile::tempdir()?;
        let keep = root.path().join("keep");
        std::fs::create_dir(&keep)?;
        std::fs::write(keep.join("keep.txt"), b"unrelated")?;
        assert!(remove_task_directory(&keep).is_err());
        assert!(keep.join("keep.txt").is_file());
        #[cfg(unix)]
        {
            let link = root.path().join(uuid::Uuid::new_v4().to_string());
            std::os::unix::fs::symlink(&keep, &link)?;
            assert!(remove_task_directory(&link).is_err());
            assert!(keep.join("keep.txt").is_file());
        }
        Ok(())
    }

    #[tokio::test]
    async fn independent_mcp_and_ui_saves_do_not_replay_stale_settings() -> Result<()> {
        let root = tempfile::tempdir()?;
        let app = App::open(AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("output"),
            tool_dir: None,
        })
        .await?;
        app.mcp_save_settings(json!({"enabled":false,"autoStart":false,"port":19492}))?;
        let mut stale = app.db.settings()?;
        app.mcp_save_settings(json!({"enabled":true,"autoStart":true,"port":20492}))?;
        stale["uiLanguage"] = json!("en-US");
        app.db.save_settings(&stale)?;
        assert_eq!(app.mcp_settings()?["port"], 20492);
        let barrier = Arc::new(std::sync::Barrier::new(2));
        std::thread::scope(|scope| -> Result<()> {
            let app = &app;
            let mcp_start = barrier.clone();
            let mcp = scope.spawn(move || -> Result<()> {
                mcp_start.wait();
                for port in 21000..21032 {
                    app.mcp_save_settings(json!({"enabled":true,"autoStart":true,"port":port}))?;
                }
                Ok(())
            });
            barrier.wait();
            for index in 0..32 {
                app.save_settings(json!({"uiLanguage":"en-US","appearanceTheme":"light",
                    "downloadOptions":{"fragmentConcurrent":6},"saveIndex":index,
                    "mcp":{"enabled":false,"autoStart":false,"port":19492}}))?;
            }
            mcp.join().expect("MCP save thread panicked")?;
            Ok(())
        })?;
        let saved = app.public_settings()?;
        assert_eq!(saved["mcp"]["port"], 21031);
        assert_eq!(saved["mcp"]["autoStart"], true);
        assert_eq!(saved["saveIndex"], 31);
        assert_eq!(saved["downloadOptions"]["fragmentConcurrent"], 6);
        app.shutdown().await?;
        Ok(())
    }

    #[tokio::test]
    async fn ui_preferences_persist_without_overwriting_settings_or_accepting_stale_drafts()
    -> Result<()> {
        let root = tempfile::tempdir()?;
        let config = AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("output"),
            tool_dir: None,
        };
        let app = App::open(config.clone()).await?;
        let draft = json!({"uiLanguage":"en-US","appearanceTheme":"light","downloadOptions":{"fragmentConcurrent":6}});
        app.command("settings.save", json!({"settings":draft}))
            .await?;
        let saved = app
            .command(
                "settings.preferences",
                json!({"preferences":{"deleteMediaOnTaskDelete":true}}),
            )
            .await?;
        assert_eq!(saved["uiPreferences"]["deleteMediaOnTaskDelete"], true);
        assert_eq!(saved["uiLanguage"], "en-US");
        assert_eq!(saved["downloadOptions"]["fragmentConcurrent"], 6);
        let mut stale = draft;
        stale["appearanceTheme"] = json!("dark");
        stale["uiPreferences"] = json!({"deleteMediaOnTaskDelete":false});
        let saved = app
            .command("settings.save", json!({"settings":stale}))
            .await?;
        assert_eq!(saved["appearanceTheme"], "dark");
        assert_eq!(saved["uiPreferences"]["deleteMediaOnTaskDelete"], true);
        app.mcp_save_settings(json!({"enabled":false,"autoStart":true,"port":19492}))?;
        app.shutdown().await?;
        drop(app);
        let app = App::open(config).await?;
        let saved = app.public_settings()?;
        assert_eq!(saved["uiPreferences"]["deleteMediaOnTaskDelete"], true);
        assert_eq!(saved["appearanceTheme"], "dark");
        assert_eq!(saved["downloadOptions"]["fragmentConcurrent"], 6);
        assert_eq!(saved["mcp"]["autoStart"], true);
        app.command(
            "settings.preferences",
            json!({"preferences":{"deleteMediaOnTaskDelete":false}}),
        )
        .await?;
        assert_eq!(
            app.public_settings()?["uiPreferences"]["deleteMediaOnTaskDelete"],
            false
        );
        app.shutdown().await?;
        Ok(())
    }

    #[tokio::test]
    async fn ui_preferences_reject_unknown_fields_and_non_boolean_values() -> Result<()> {
        let root = tempfile::tempdir()?;
        let app = App::open(AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("output"),
            tool_dir: None,
        })
        .await?;
        let previous = app.public_settings()?;
        for preferences in [
            json!(null),
            json!([]),
            json!({}),
            json!({"deleteMediaOnTaskDelete":"true"}),
            json!({"deleteMediaOnTaskDelete":1}),
            json!({"unknown":true}),
            json!({"deleteMediaOnTaskDelete":true,"unknown":false}),
        ] {
            assert!(
                app.command("settings.preferences", json!({"preferences":preferences}))
                    .await
                    .is_err()
            );
            assert_eq!(app.public_settings()?, previous);
        }
        app.shutdown().await?;
        Ok(())
    }

    #[tokio::test]
    async fn saved_configuration_and_secrets_survive_reopening() -> Result<()> {
        let root = tempfile::tempdir()?;
        let config = AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("output"),
            tool_dir: None,
        };
        let app = App::open(config.clone()).await?;
        let options = DownloadOptions {
            quality: "1080p".into(),
            container: "mp4".into(),
            fragment_concurrent: 6,
            subtitle_language: "en".into(),
            organize_task_folder: true,
            proxy: "http://fixture-user:fixture-password@proxy.example.test:8080".into(),
            ..Default::default()
        };
        app.command(
            "settings.save",
            json!({"settings":{
                "downloadOptions":options,"uiLanguage":"en-US","appearanceTheme":"light",
                "desktopNotifications":true,"aiBaseUrl":"https://models.example.test/v1",
                "aiModel":"fixture-image","aiApiKey":"fixture-api-key"
            }}),
        )
        .await?;
        app.mcp_save_settings(json!({"enabled":false,"autoStart":true,"port":20492}))?;
        let cookies =
            "# Netscape HTTP Cookie File\n.example.test\tTRUE\t/\tTRUE\t0\tSID\tfixture-session\n";
        app.sync_login_cookies("embedded", "Browser", Some(cookies))?;
        let saved = app.public_settings()?;
        app.shutdown().await?;
        drop(app);
        let app = App::open(config).await?;
        assert_eq!(app.public_settings()?, saved);
        assert_eq!(saved["hasAiApiKey"], true);
        assert_eq!(saved["hasProxy"], true);
        assert!(!saved.to_string().contains("fixture-api-key"));
        assert!(!saved.to_string().contains("fixture-password"));
        assert_eq!(
            app.credentials.secret("aiApiKey")?.as_deref(),
            Some("fixture-api-key")
        );
        assert_eq!(
            app.credentials.secret("downloadProxy")?.as_deref(),
            Some(options.proxy.as_str())
        );
        assert_eq!(
            app.credentials
                .matching("https://www.example.test/media")?
                .as_deref(),
            Some(cookies)
        );
        let mut stored = DownloadOptions::parse(saved["downloadOptions"].clone())?;
        stored.proxy = options.proxy.clone();
        assert_eq!(
            serde_json::to_value(stored)?,
            serde_json::to_value(options)?
        );
        app.shutdown().await?;
        Ok(())
    }

    #[tokio::test]
    async fn audit_processing_folder_survives_settings_round_trip() -> Result<()> {
        let root = tempfile::tempdir()?;
        let app = App::open(AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("output"),
            tool_dir: None,
        })
        .await?;
        let folder = root.path().join("processing");
        let saved = app
            .command(
                "settings.save",
                json!({"settings":{"downloadOptions":{"processingTempDir":folder}}}),
            )
            .await?;
        assert_eq!(saved["downloadOptions"]["processingTempDir"], json!(folder));
        app.shutdown().await?;
        drop(app);
        let app = App::open(AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("output"),
            tool_dir: None,
        })
        .await?;
        let options = DownloadOptions::parse(app.public_settings()?["downloadOptions"].clone())?;
        assert_eq!(PathBuf::from(options.processing_temp_dir), folder);
        app.shutdown().await?;
        Ok(())
    }

    #[tokio::test]
    async fn audit_task_deletion_cleans_only_its_files_and_secrets_and_emits_media_removal()
    -> Result<()> {
        let root = tempfile::tempdir()?;
        let app = App::open(AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("output"),
            tool_dir: None,
        })
        .await?;
        let processing = root.path().join("custom");
        std::fs::create_dir_all(&processing)?;
        let sentinel = processing.join("keep.txt");
        std::fs::write(&sentinel, b"unrelated")?;
        for delete_files in [false, true] {
            let mut task = paused_task(HashMap::new());
            task.options.processing_temp_dir = processing.to_string_lossy().into();
            task.proxy_snapshot = Some(format!("task-proxy:{}", task.id));
            // A saved source manifest has already cleared the cookie reference.
            app.credentials
                .put_secret(&format!("task-cookie:{}", task.id), "cookie snapshot")?;
            app.credentials
                .put_secret(task.proxy_snapshot.as_ref().unwrap(), "proxy snapshot")?;
            app.credentials.put_secret("aiApiKey", "keep this key")?;
            app.db.insert_task(&task)?;
            let source = app.config.data_dir.join("tasks").join(&task.id);
            let workspace = processing.join(format!("HuifaRust-{}", task.id));
            std::fs::create_dir_all(&source)?;
            std::fs::create_dir_all(&workspace)?;
            std::fs::write(source.join("source.bin"), b"source")?;
            std::fs::write(workspace.join("prepared.bin"), b"working media")?;
            let path = app.config.download_dir.join(format!("{}.mp4", task.id));
            std::fs::write(&path, b"output")?;
            let media = model::Media {
                id: uuid::Uuid::new_v4().to_string(),
                task_id: task.id.clone(),
                title: "Audit".into(),
                filename: "output.mp4".into(),
                size: 6,
                duration: 1.0,
                path: path.clone(),
                assets: vec![],
            };
            app.db.complete(&task, &media)?;
            let mut events = app.subscribe();
            app.command(
                "tasks.delete",
                json!({"id":task.id,"deleteFiles":delete_files}),
            )
            .await?;
            assert!(
                app.credentials
                    .secret(&format!("task-cookie:{}", task.id))?
                    .is_none()
            );
            assert!(
                app.credentials
                    .secret(task.proxy_snapshot.as_ref().unwrap())?
                    .is_none()
            );
            assert_eq!(
                app.credentials.secret("aiApiKey")?.as_deref(),
                Some("keep this key")
            );
            assert_eq!(path.exists(), !delete_files);
            assert_eq!(source.exists(), !delete_files);
            assert_eq!(workspace.exists(), !delete_files);
            assert_eq!(app.db.media_for_task(&task.id)?.is_none(), delete_files);
            if delete_files {
                assert!(app.db.task(&task.id).is_err());
            } else {
                assert_eq!(app.db.task(&task.id)?.status, "retained");
                assert!(app.db.tasks()?.iter().all(|item| item.id != task.id));
                assert!(
                    app.research_tasks()?
                        .iter()
                        .all(|item| item["id"] != task.id)
                );
            }
            let mut removed = false;
            while let Ok(event) = events.try_recv() {
                if event.kind == "media.remove" {
                    assert_eq!(event.data["id"], media.id);
                    removed = true;
                }
            }
            assert_eq!(removed, delete_files);
            assert!(sentinel.is_file());
        }
        app.shutdown().await?;
        Ok(())
    }

    pub(super) fn paused_task(tools: HashMap<String, PathBuf>) -> Task {
        Task {
            kind: "media".into(),
            files: vec![],
            id: uuid::Uuid::new_v4().to_string(),
            url: "https://example.test/frozen.mp4".into(),
            title: "Frozen runtime".into(),
            thumbnail: None,
            status: "paused".into(),
            stage: "queued".into(),
            steps: vec![],
            step_index: None,
            progress: 0.0,
            stage_progress: None,
            stage_indeterminate: true,
            speed: None,
            eta: None,
            error: None,
            warnings: vec![],
            created_at: chrono::Utc::now().to_rfc3339(),
            options: DownloadOptions::default(),
            output_path: None,
            previous_outputs: vec![],
            cookie_snapshot: None,
            proxy_snapshot: None,
            manifest: None,
            publication: None,
            conversion: false,
            tools,
        }
    }

    #[tokio::test]
    async fn removed_queue_item_keeps_library_conversion_and_redownload_working() -> Result<()> {
        let root = tempfile::tempdir()?;
        let bundled = root.path().join("tools");
        std::fs::create_dir_all(&bundled)?;
        for name in ["yt-dlp", "ffmpeg", "ffprobe", "qjs"] {
            std::fs::write(
                bundled.join(format!("{name}{}", std::env::consts::EXE_SUFFIX)),
                format!("fixture {name}"),
            )?;
        }
        let app = App::open(AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("downloads"),
            tool_dir: Some(bundled),
        })
        .await?;
        let network = app.network.acquire_many(3).await?;
        let processor = app.processor.acquire().await?;
        let mut task = paused_task(app.freeze_tools().await?);
        task.stage("completed", "completed", Some(100.0));
        let path = app.config.download_dir.join("original.mp4");
        std::fs::write(&path, b"original media")?;
        let obsolete = app.config.download_dir.join("obsolete.mkv");
        std::fs::write(&obsolete, b"pending cleanup")?;
        task.previous_outputs = vec![obsolete.clone()];
        task.output_path = Some(path.clone());
        app.db.insert_task(&task)?;
        app.db.complete(
            &task,
            &model::Media {
                id: "retained-media".into(),
                task_id: task.id.clone(),
                title: task.title.clone(),
                filename: "original.mp4".into(),
                size: 14,
                duration: 1.0,
                path: path.clone(),
                assets: vec![],
            },
        )?;
        app.command("tasks.delete", json!({"id":task.id,"deleteFiles":false}))
            .await?;
        assert_eq!(app.db.task(&task.id)?.status, "retained");
        assert!(app.db.tasks()?.is_empty());
        let conversion = app
            .research_convert_media_options("retained-media", &json!({"container":"mkv"}))
            .await?;
        let conversion_id = conversion["id"].as_str().unwrap();
        assert_ne!(conversion_id, task.id);
        assert_eq!(
            app.db.task(conversion_id)?.manifest.unwrap().files[0].path,
            path
        );
        app.command(
            "tasks.delete",
            json!({"id":conversion_id,"deleteFiles":true}),
        )
        .await?;
        let added = app
            .command("tasks.add", json!({"urls":[task.url],"report":true}))
            .await?;
        assert_eq!(added["ids"][0], task.id);
        assert_eq!(added["results"][0]["action"], "restarted");
        assert_eq!(
            app.db.task(&task.id)?.previous_outputs,
            vec![obsolete.clone()]
        );
        assert_eq!(std::fs::read(&path)?, b"original media");
        app.command("tasks.delete", json!({"id":task.id,"deleteFiles":false}))
            .await?;
        assert_eq!(app.db.task(&task.id)?.status, "retained");
        app.command("tasks.delete", json!({"id":task.id,"deleteFiles":true}))
            .await?;
        assert!(app.db.task(&task.id).is_err());
        assert!(app.db.media_for_task(&task.id)?.is_none());
        assert!(!path.exists());
        assert!(!obsolete.exists());
        drop(processor);
        drop(network);
        app.shutdown().await?;
        Ok(())
    }

    #[tokio::test]
    async fn duplicate_adds_reuse_active_and_paused_tasks_and_restart_completed_record()
    -> Result<()> {
        let root = tempfile::tempdir()?;
        let bundled = root.path().join("tools");
        std::fs::create_dir_all(&bundled)?;
        for name in ["yt-dlp", "ffmpeg", "ffprobe", "qjs"] {
            std::fs::write(
                bundled.join(format!("{name}{}", std::env::consts::EXE_SUFFIX)),
                format!("fixture {name}"),
            )?;
        }
        let app = App::open(AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("downloads"),
            tool_dir: Some(bundled),
        })
        .await?;
        let permit = app.network.acquire_many(3).await?;
        let request = json!({"urls":["https://www.youtube.com/watch?v=abcdef12345&list=one","https://youtu.be/abcdef12345"],"options":{"quality":"best"},"report":true});
        let (one, two) = tokio::join!(
            app.command("tasks.add", request.clone()),
            app.command("tasks.add", request.clone())
        );
        let one = one?;
        let two = two?;
        let id = one["ids"][0].as_str().unwrap();
        assert_eq!(one["ids"], two["ids"]);
        assert_eq!(one["ids"].as_array().unwrap().len(), 1);
        assert_eq!(two["results"][0]["action"], "existing");
        assert_eq!(app.db.tasks()?.len(), 1);
        app.stop_task(id, "paused").await?;
        let paused=app.command("tasks.add",json!({"urls":["https://youtu.be/abcdef12345"],"options":{"quality":"worst"},"report":true})).await?;
        assert_eq!(paused["results"][0]["status"], "paused");
        assert_eq!(app.db.task(id)?.options.quality, "best");
        let result_path = app.config.download_dir.join("旧标题 [abcdef12345].mkv");
        std::fs::write(&result_path, b"old valid media")?;
        let mut task = app.db.task(id)?;
        task.status = "completed".into();
        task.output_path = Some(result_path.clone());
        app.db.save_task(&task)?;
        let restarted=app.command("tasks.add",json!({"urls":["https://youtu.be/abcdef12345"],"options":{"quality":"worst"},"report":true})).await?;
        assert_eq!(restarted["ids"][0], id);
        assert_eq!(restarted["results"][0]["action"], "restarted");
        assert_eq!(app.db.task(id)?.options.quality, "worst");
        assert_eq!(app.db.tasks()?.len(), 1);
        app.stop_task(id, "cancelled").await?;
        assert_eq!(std::fs::read(result_path)?, b"old valid media");
        assert_ne!(
            download_key("https://example.test/play?id=one&token=a")?,
            download_key("https://example.test/play?id=two&token=a")?
        );
        assert_eq!(
            download_key("https://example.test/play?id=one#fragment")?,
            download_key("https://example.test/play?id=one")?
        );
        drop(permit);
        app.shutdown().await?;
        Ok(())
    }

    #[tokio::test]
    async fn task_copy_preserves_original_source_and_complete_unicode_result_path() -> Result<()> {
        let root = tempfile::tempdir()?;
        let app = App::open(AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("downloads"),
            tool_dir: None,
        })
        .await?;
        let mut task = paused_task(Default::default());
        task.title = "【视频】中文标题".into();
        task.url = "https://example.test/play?id=42&signature=private".into();
        app.db.insert_task(&task)?;
        assert_eq!(app.task_copy_value(&task.id, "title")?, task.title);
        assert_eq!(app.task_copy_value(&task.id, "source")?, task.url);
        assert!(app.task_copy_value(&task.id, "path").is_err());
        let path = app
            .config
            .download_dir
            .join("中文目录/【视频】中文标题 [42].mkv");
        std::fs::create_dir_all(path.parent().unwrap())?;
        std::fs::write(&path, b"saved result")?;
        let media = model::Media {
            id: uuid::Uuid::new_v4().to_string(),
            task_id: task.id.clone(),
            title: task.title.clone(),
            filename: path.file_name().unwrap().to_string_lossy().into(),
            size: 12,
            duration: 1.0,
            path: path.clone(),
            assets: vec![],
        };
        app.db.complete(&task, &media)?;
        assert_eq!(
            app.task_copy_value(&task.id, "path")?,
            path.to_string_lossy()
        );
        assert!(!task.public().to_string().contains("signature"));
        std::fs::remove_file(path)?;
        assert!(app.task_copy_value(&task.id, "path").is_err());
        app.shutdown().await?;
        Ok(())
    }

    #[tokio::test]
    async fn task_preview_is_available_before_media_and_confined_to_its_task() -> Result<()> {
        let root = tempfile::tempdir()?;
        let app = App::open(AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("downloads"),
            tool_dir: None,
        })
        .await?;
        let mut task = paused_task(HashMap::new());
        let mut legacy = serde_json::to_value(&task)?;
        legacy.as_object_mut().unwrap().remove("thumbnail");
        assert!(serde_json::from_value::<Task>(legacy)?.thumbnail.is_none());
        let path = app
            .config
            .data_dir
            .join("tasks")
            .join(&task.id)
            .join("preview/thumbnail.jpg");
        std::fs::create_dir_all(path.parent().unwrap())?;
        image::RgbImage::from_pixel(2, 2, image::Rgb([40, 60, 80])).save(&path)?;
        task.thumbnail = Some(path.clone());
        app.db.insert_task(&task)?;
        assert!(app.db.media_for_task(&task.id)?.is_none());
        assert_eq!(task.public()["hasThumbnail"], true);
        assert!(task.public().get("thumbnail").is_none());
        let value = app
            .command("media.file", json!({"id":task.id,"kind":"taskThumbnail"}))
            .await?;
        assert_eq!(
            PathBuf::from(value["path"].as_str().unwrap()),
            path.canonicalize()?
        );
        assert_eq!(value["mimeType"], "image/jpeg");
        for wrong in [
            app.config.download_dir.join("other.jpg"),
            app.config
                .data_dir
                .join("tasks/other/preview/thumbnail.jpg"),
            path.with_file_name("secret.txt"),
        ] {
            std::fs::create_dir_all(wrong.parent().unwrap())?;
            std::fs::write(&wrong, b"private")?;
            task.thumbnail = Some(wrong);
            app.db.save_task(&task)?;
            assert!(
                app.command("media.file", json!({"id":task.id,"kind":"taskThumbnail"}))
                    .await
                    .is_err()
            );
        }
        app.shutdown().await?;
        Ok(())
    }

    #[tokio::test]
    async fn frozen_tools_survive_source_upgrade_and_restart_and_reject_tampering() -> Result<()> {
        let root = tempfile::tempdir()?;
        let bundled = root.path().join("bundled");
        std::fs::create_dir_all(&bundled)?;
        let names = ["yt-dlp", "ffmpeg", "ffprobe", "deno"];
        let source = |name: &str| bundled.join(format!("{name}{}", std::env::consts::EXE_SUFFIX));
        for name in names {
            std::fs::write(source(name), format!("{name}:version-one"))?;
        }
        let config = AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("downloads"),
            tool_dir: Some(bundled.clone()),
        };
        let app = App::open(config.clone()).await?;
        let old = paused_task(app.freeze_tools().await?);
        ensure!(
            old.tools["ffmpeg"].parent() == old.tools["ffprobe"].parent(),
            "FFmpeg and FFprobe were frozen into different directories"
        );
        app.db.insert_task(&old)?;
        ensure!(
            old.public().get("tools").is_none(),
            "Runtime paths reached the public task snapshot"
        );
        app.shutdown().await?;
        drop(app);

        for name in names {
            std::fs::write(source(name), format!("{name}:version-two"))?;
        }
        let app = App::open(config.clone()).await?;
        let saved = app.db.task(&old.id)?;
        let new = paused_task(app.freeze_tools().await?);
        app.db.insert_task(&new)?;
        for name in names {
            let old_path = app.task_tool(&saved, name).await?;
            let new_path = app.task_tool(&new, name).await?;
            ensure!(
                old_path == old.tools[name] && old_path != new_path,
                "Upgrade changed a frozen runtime or failed to adopt a new runtime"
            );
            ensure!(
                std::fs::read(&old_path)? == format!("{name}:version-one").as_bytes(),
                "An old task switched runtime bytes"
            );
            ensure!(
                std::fs::read(&new_path)? == format!("{name}:version-two").as_bytes(),
                "A new task did not adopt the upgraded runtime"
            );
        }
        app.shutdown().await?;
        drop(app);

        let app = App::open(config).await?;
        let saved = app.db.task(&old.id)?;
        let new = app.db.task(&new.id)?;
        for name in names {
            ensure!(
                app.task_tool(&saved, name).await? == old.tools[name]
                    && app.task_tool(&new, name).await? == new.tools[name],
                "Restart lost a runtime snapshot"
            );
        }
        let mut unsafe_task = saved.clone();
        unsafe_task.tools.insert("ffmpeg".into(), source("ffmpeg"));
        ensure!(
            app.task_tool(&unsafe_task, "ffmpeg").await.is_err(),
            "A mutable runtime path was accepted"
        );
        let cached = &saved.tools["ffmpeg"];
        #[cfg(windows)]
        #[allow(clippy::permissions_set_readonly_false)]
        {
            let mut permissions = std::fs::metadata(cached)?.permissions();
            permissions.set_readonly(false);
            std::fs::set_permissions(cached, permissions)?;
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(cached, std::fs::Permissions::from_mode(0o700))?;
        }
        let original = std::fs::metadata(cached)?;
        std::fs::write(cached, vec![b'X'; original.len() as usize])?;
        std::fs::OpenOptions::new()
            .write(true)
            .open(cached)?
            .set_times(std::fs::FileTimes::new().set_modified(original.modified()?))?;
        ensure!(
            app.task_tool(&saved, "ffmpeg").await.is_err(),
            "Changed frozen runtime bytes with the same size and modification time were accepted"
        );

        let missing = paused_task(HashMap::new());
        app.db.insert_task(&missing)?;
        app.command("tasks.resume", json!({"id":missing.id}))
            .await?;
        tokio::time::timeout(std::time::Duration::from_secs(3), async {
            loop {
                let task = app.db.task(&missing.id)?;
                if task.status == "failed" {
                    ensure!(
                        task.tools.is_empty()
                            && task
                                .error
                                .as_deref()
                                .is_some_and(|e| e.contains("frozen yt-dlp")),
                        "A missing snapshot was silently replaced"
                    );
                    return Ok::<_, anyhow::Error>(());
                }
                tokio::time::sleep(std::time::Duration::from_millis(10)).await;
            }
        })
        .await??;
        app.shutdown().await?;
        Ok(())
    }
}

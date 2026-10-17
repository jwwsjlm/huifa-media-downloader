use anyhow::{Context, Result, ensure};
use axum::{
    Router,
    extract::{ConnectInfo, Request, State},
    http::StatusCode,
    middleware::{self, Next},
    response::{IntoResponse, Response},
};
use huifa_core::{App, DownloadOptions};
use rmcp::{
    ErrorData, RoleServer, ServerHandler,
    model::*,
    service::RequestContext,
    transport::streamable_http_server::{
        StreamableHttpServerConfig, StreamableHttpService, session::local::LocalSessionManager,
    },
};
use serde_json::{Value, json};
use std::{
    net::{IpAddr, Ipv4Addr, SocketAddr},
    sync::Arc,
};
use tokio_util::sync::CancellationToken;

pub struct DesktopMcp {
    app: Arc<App>,
    running: tokio::sync::Mutex<Option<(CancellationToken, tokio::task::JoinHandle<()>)>>,
}
impl DesktopMcp {
    pub fn new(app: Arc<App>) -> Self {
        Self {
            app,
            running: Default::default(),
        }
    }
    pub async fn configure(
        &self,
        enabled: bool,
        downloads: bool,
        port: u16,
        auto_start: Option<bool>,
    ) -> Result<Value> {
        ensure!(port > 0, "MCP port must be between 1 and 65535");
        let saved = self.app.mcp_settings()?;
        let preference_only = auto_start.is_some();
        let auto_start = auto_start.unwrap_or(
            saved["autoStart"]
                .as_bool()
                .unwrap_or(saved["enabled"] == true),
        );
        let mut running = self.running.lock().await;
        if preference_only
            && running.is_some() == enabled
            && saved["enabled"] == enabled
            && saved["port"] == port
        {
            self.app.mcp_save_settings(
                json!({"enabled":enabled,"downloads":downloads,"port":port,"autoStart":auto_start}),
            )?;
            return self.status();
        }
        if let Some((cancel, handle)) = running.take() {
            cancel.cancel();
            handle.abort();
            let _ = handle.await;
        }
        self.app.mcp_save_settings(
            json!({"enabled":false,"downloads":downloads,"port":port,"autoStart":auto_start}),
        )?;
        if enabled {
            let listener = tokio::net::TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, port))
                .await
                .context("MCP port is unavailable; choose another port")?;
            let cancel = CancellationToken::new();
            let mut config = StreamableHttpServerConfig::default().enforce_origin_validation();
            config.cancellation_token = cancel.clone();
            config.max_request_body_bytes = 64 * 1024;
            config.allowed_hosts = vec![format!("127.0.0.1:{port}"), format!("localhost:{port}")];
            let app = self.app.clone();
            let router = http_router(app, config);
            let stop = cancel.clone();
            let handle = tokio::spawn(async move {
                let _ = axum::serve(
                    listener,
                    router.into_make_service_with_connect_info::<SocketAddr>(),
                )
                .with_graceful_shutdown(stop.cancelled_owned())
                .await;
            });
            *running = Some((cancel, handle));
        }
        self.app.mcp_save_settings(
            json!({"enabled":enabled,"downloads":downloads,"port":port,"autoStart":auto_start}),
        )?;
        self.status()
    }
    pub fn status(&self) -> Result<Value> {
        self.app.mcp_settings()
    }
    pub fn connection(&self, bridge: &str) -> Result<Value> {
        let config = self.status()?;
        let port = config["port"].as_u64().unwrap_or(19492);
        let url = format!("http://127.0.0.1:{port}/mcp");
        Ok(
            json!({"url":url,"http":{"mcpServers":{"huifa":{"url":url,"headers":{"Authorization":"Bearer YOUR_ACCESS_PASSWORD"}}}},"stdio":{"mcpServers":{"huifa":{"command":bridge,"env":{"HUIFA_MCP_URL":url,"HUIFA_MCP_PASSWORD":"YOUR_ACCESS_PASSWORD"}}}},"codex":format!("[mcp_servers.huifa]\nurl = \"{url}\"\nhttp_headers = {{ Authorization = \"Bearer YOUR_ACCESS_PASSWORD\" }}\n"),"tools":definitions()}),
        )
    }
    pub async fn shutdown(&self) {
        if let Some((cancel, handle)) = self.running.lock().await.take() {
            cancel.cancel();
            handle.abort();
            let _ = handle.await;
        }
    }
}
/// Mount on the host's existing HTTP listener; it shares the same core and queue.
pub fn http_router(app: Arc<App>, config: StreamableHttpServerConfig) -> Router {
    router_at(app, config, "/mcp")
}
fn router_at(app: Arc<App>, config: StreamableHttpServerConfig, path: &str) -> Router {
    // Tauri enables reqwest's provider-free TLS; use the core's existing ring backend.
    let _ = rustls::crypto::ring::default_provider().install_default();
    let stop = config.cancellation_token.clone();
    let handler_app = app.clone();
    let service = StreamableHttpService::new(
        move || {
            Ok(Handler {
                generation: handler_app.access.generation(),
                app: handler_app.clone(),
                stop: stop.clone(),
            })
        },
        Arc::new(LocalSessionManager::default()),
        config,
    );
    Router::new()
        .nest_service(path, service)
        .layer(middleware::from_fn_with_state(app, require_enabled))
}
pub fn web_router(app: Arc<App>, stop: CancellationToken) -> Router {
    // The web host validates its public authority before dispatching to this service.
    let mut config = StreamableHttpServerConfig::default().enforce_origin_validation();
    config.allowed_hosts.clear();
    config.cancellation_token = stop;
    config.max_request_body_bytes = 64 * 1024;
    router_at(app, config, "/mcp-server")
}
async fn require_enabled(State(app): State<Arc<App>>, request: Request, next: Next) -> Response {
    let valid = app
        .mcp_settings()
        .ok()
        .is_some_and(|s| s["enabled"] == true);
    if !valid {
        return StatusCode::SERVICE_UNAVAILABLE.into_response();
    }
    let password = request
        .headers()
        .get("authorization")
        .and_then(|value| std::str::from_utf8(value.as_bytes()).ok())
        .and_then(|value| value.strip_prefix("Bearer "))
        .filter(|value| value.len() <= 4096)
        .map(str::to_owned);
    let Some(password) = password else {
        return unauthorized();
    };
    static SLOTS: std::sync::OnceLock<Arc<tokio::sync::Semaphore>> = std::sync::OnceLock::new();
    let Ok(permit) = SLOTS
        .get_or_init(|| Arc::new(tokio::sync::Semaphore::new(2)))
        .clone()
        .try_acquire_owned()
    else {
        return StatusCode::TOO_MANY_REQUESTS.into_response();
    };
    let ip = request
        .extensions()
        .get::<ConnectInfo<SocketAddr>>()
        .map(|peer| peer.0.ip())
        .unwrap_or(IpAddr::V4(Ipv4Addr::LOCALHOST));
    if !app.access.check_attempt(ip) {
        return StatusCode::TOO_MANY_REQUESTS.into_response();
    }
    let generation = app.access.generation();
    let verifier = app.clone();
    let valid = tokio::task::spawn_blocking(move || {
        let _permit = permit;
        let valid = verifier.access.verify_bearer(&password);
        if verifier.access.generation() == generation {
            verifier.access.record_attempt(ip, valid);
        }
        valid
    })
    .await
    .unwrap_or(false);
    if app.access.generation() != generation {
        return unauthorized();
    }
    if !valid {
        return unauthorized();
    }
    let response = next.run(request).await;
    if app.access.generation() != generation {
        return unauthorized();
    }
    response
}
fn unauthorized() -> Response {
    (
        StatusCode::UNAUTHORIZED,
        [("www-authenticate", "Bearer realm=\"Huifa\"")],
        axum::Json(json!({"error":"MCP access password is missing or incorrect"})),
    )
        .into_response()
}

#[derive(Clone)]
struct Handler {
    app: Arc<App>,
    stop: CancellationToken,
    generation: u64,
}
impl ServerHandler for Handler {
    fn get_info(&self) -> ServerConfig {
        ServerConfig::new(ServerCapabilities::builder().enable_tools().build())
    }
    async fn list_tools(
        &self,
        _: Option<PaginatedRequestParams>,
        _: RequestContext<RoleServer>,
    ) -> Result<ListToolsResult, ErrorData> {
        if self.generation != self.app.access.generation() {
            return Err(ErrorData::invalid_request(
                "Access password changed; reconnect MCP",
                None,
            ));
        }
        let downloads = self
            .app
            .mcp_settings()
            .ok()
            .is_some_and(|s| s["downloads"] == true);
        Ok(serde_json::from_value(json!({"tools":definitions().into_iter().filter(|d|downloads||!writes(d["name"].as_str().unwrap_or(""))).collect::<Vec<_>>()})).unwrap())
    }
    async fn call_tool(
        &self,
        request: CallToolRequestParams,
        context: RequestContext<RoleServer>,
    ) -> Result<CallToolResponse, ErrorData> {
        let args = Value::Object(request.arguments.unwrap_or_default());
        let cancel = context.ct.child_token();
        let future = self.execute(&request.name, &args, &cancel);
        let result = tokio::select! {
            result=future=>result,
            _=self.stop.cancelled()=>{cancel.cancel();Err(anyhow::anyhow!("MCP is disabled or stopping"))},
            _=tokio::time::sleep(std::time::Duration::from_secs(120))=>{cancel.cancel();Err(anyhow::anyhow!("Query timed out after 120 seconds; try a smaller request"))}
        };
        let result = if self.generation != self.app.access.generation() {
            Err(anyhow::anyhow!("Access password changed; reconnect MCP"))
        } else {
            result
        };
        let value = match result {
            Ok(value) => {
                json!({"content":[{"type":"text","text":serde_json::to_string(&value).unwrap()}],"structuredContent":value,"isError":false})
            }
            Err(error) => {
                let message = self.app.research_error(&format!("{error:#}"));
                let (code, hint) = classify_error(&message);
                json!({"content":[{"type":"text","text":format!("{message}\n{hint}")}],"structuredContent":{"error":message,"code":code,"hint":hint},"isError":true})
            }
        };
        Ok(serde_json::from_value::<CallToolResult>(value)
            .unwrap()
            .into())
    }
}
fn writes(name: &str) -> bool {
    matches!(
        name,
        "start_download"
            | "start_batch_download"
            | "control_download"
            | "resolve_download_filename"
            | "convert_media"
            | "retry_processing"
            | "start_collection_download"
            | "download_assets"
    )
}
fn classify_error(message: &str) -> (&'static str, &'static str) {
    let lower = message.to_lowercase();
    if lower.contains("requirements unmet:") {
        (
            "requirements_unmet",
            "The selected quality, frame rate, audio or subtitles are unavailable. Ask the user whether to adjust the requirements before restarting.",
        )
    } else if lower.contains("destination conflict:") {
        (
            "filename_conflict",
            "Ask the user to choose another filename or skip using resolve_download_filename. Replacing files requires confirmation in the desktop or authenticated web interface.",
        )
    } else if lower.contains("429") || lower.contains("rate limit") {
        (
            "rate_limited",
            "The site is rate limiting requests. Wait before retrying.",
        )
    } else if lower.contains("sign in")
        || lower.contains("login")
        || lower.contains("bot")
        || lower.contains("cookies")
    {
        (
            "authentication_required",
            "Check saved Cookie and proxy settings in the desktop app.",
        )
    } else if lower.contains("subtitle") {
        (
            "subtitles_unavailable",
            "Query subtitle languages first, then choose an available language.",
        )
    } else if lower.contains("source reference") || lower.contains("source ref") {
        (
            "source_expired",
            "Search or list the source again to obtain a current sourceRef.",
        )
    } else if lower.contains("too large")
        || lower.contains("output limit")
        || lower.contains("byte limit")
    {
        (
            "result_too_large",
            "The extractor result exceeded the query size limit. Full comment extraction cannot be reduced by changing the page size.",
        )
    } else if lower.contains("timed out") {
        (
            "timeout",
            "Check network settings. Full comment extraction may exceed the time limit even for a small result page.",
        )
    } else if lower.contains("enable mcp") || lower.contains("disabled") {
        (
            "permission_denied",
            "Check MCP access and download permissions in desktop Settings.",
        )
    } else if lower.contains("unsupported url") {
        (
            "unsupported_site",
            "This URL is not supported by the installed yt-dlp version.",
        )
    } else if lower.contains("unsupported operation")
        || lower.contains("comments are unavailable")
        || lower.contains("comments were not provided")
        || lower.contains("extractor does not provide")
    {
        (
            "unsupported_operation",
            "The installed extractor does not provide this feature for the source. Other queries and downloads may still work.",
        )
    } else if lower.contains("unavailable")
        || lower.contains("private video")
        || lower.contains("removed")
    {
        (
            "unavailable",
            "The video may be unavailable, private, or restricted in this location.",
        )
    } else if lower.contains("yt-dlp")
        && (lower.contains("missing")
            || lower.contains("not found")
            || lower.contains("cannot start"))
    {
        (
            "tool_unavailable",
            "Install or repair yt-dlp in Settings > Tools & updates.",
        )
    } else {
        (
            "operation_failed",
            "Check the diagnostic message and saved network settings before retrying.",
        )
    }
}
impl Handler {
    async fn execute(&self, name: &str, args: &Value, cancel: &CancellationToken) -> Result<Value> {
        ensure!(
            self.generation == self.app.access.generation(),
            "Access password changed; reconnect MCP"
        );
        let config = self.app.mcp_settings()?;
        ensure!(config["enabled"] == true, "MCP is disabled");
        if writes(name) {
            ensure!(
                config["downloads"] == true,
                "Enable MCP download management in Settings first"
            );
        }
        validate(name, args)?;
        match name {
            "start_batch_download" => {
                let options = download_options(self.app.research_options()?, args)?;
                let items = args["items"].as_array().cloned().unwrap_or_else(|| {
                    args["urls"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .map(|url| json!({"url":url}))
                        .collect()
                });
                self.queue_items(&items, &options, cancel).await
            }
            "start_collection_download" => {
                let options = download_options(self.app.research_options()?, args)?;
                let mut query = args.clone();
                query["limit"] = json!(args["limit"].as_u64().unwrap_or(20));
                query["options"] = options.clone();
                let collection = self
                    .app
                    .research("list_collection_items", &query, cancel)
                    .await?;
                let entries = collection["entries"]
                    .as_array()
                    .context("Collection has no entries")?;
                let items = entries
                    .iter()
                    .map(|entry| {
                        if entry["sourceRef"].is_string() {
                            json!({"sourceRef":entry["sourceRef"]})
                        } else {
                            json!({"url":entry["url"]})
                        }
                    })
                    .collect::<Vec<_>>();
                let mut result = self.queue_items(&items, &options, cancel).await?;
                result["nextOffset"] = collection["nextOffset"].clone();
                result["selectionScope"] = json!("range-local");
                Ok(result)
            }
            "get_download_options" => {
                let schema = download_options_schema();
                let current =
                    serde_json::to_value(DownloadOptions::parse(self.app.research_options()?)?)?;
                Ok(
                    json!({"schema":schema,"defaults":public_options(&serde_json::to_value(DownloadOptions::default())?),"current":public_options(&current),"features":feature_groups()}),
                )
            }
            "preview_download" => {
                let mut query = args.clone();
                query["options"] = download_options(self.app.research_options()?, args)?;
                self.app.research(name, &query, cancel).await
            }
            "download_assets" => {
                let mut query = args.clone();
                query["url"] = json!(self.app.resolve_research_url(args)?);
                query.as_object_mut().unwrap().remove("sourceRef");
                query["options"] = download_options(self.app.research_options()?, args)?;
                self.app.research_download_assets(&query).await
            }
            "list_media" => self.app.research_media(
                None,
                args["offset"].as_u64().unwrap_or(0) as usize,
                args["limit"].as_u64().unwrap_or(20) as usize,
                args["query"].as_str().unwrap_or(""),
            ),
            "get_media" => self.app.research_media(args["id"].as_str(), 0, 1, ""),
            "convert_media" => self
                .app
                .research_convert_media_options(
                    args["id"].as_str().unwrap(),
                    &conversion_options(args),
                )
                .await
                .map(|task| json!({"task":task})),
            "retry_processing" => self
                .app
                .research_retry_processing(args["id"].as_str().unwrap())
                .await
                .map(|task| json!({"task":task})),
            "get_cookie_status" => {
                if args.get("url").is_some() || args.get("sourceRef").is_some() {
                    self.app
                        .cookie_status(Some(&self.app.resolve_research_url(args)?))
                } else {
                    self.app.cookie_status(None)
                }
            }
            "start_download" => {
                let options = download_options(self.app.research_options()?, args)?;
                let url = self.app.resolve_research_url(args)?;
                let report = self
                    .app
                    .command(
                        "tasks.add",
                        json!({"urls":[url],"options":options,"report":true}),
                    )
                    .await?;
                Ok(json!({"taskIds":report["ids"],"results":report["results"]}))
            }
            "control_download" => self
                .app
                .command(
                    &format!("tasks.{}", args["action"].as_str().unwrap()),
                    json!({"id":args["id"]}),
                )
                .await
                .map(|_| json!({"id":args["id"],"action":args["action"],"accepted":true})),
            "resolve_download_filename" => self
                .app
                .command(
                    "tasks.destination",
                    json!({"id":args["id"],"choice":args["choice"],"filename":args["filename"]}),
                )
                .await
                .map(|_| json!({"id":args["id"],"choice":args["choice"],"accepted":true})),
            "get_download" => self
                .app
                .research_tasks()?
                .into_iter()
                .find(|t| t["id"] == args["id"])
                .context("Download task not found"),
            "list_downloads" => {
                let tasks = self.app.research_tasks()?;
                let offset = args["offset"].as_u64().unwrap_or(0) as usize;
                let limit = args["limit"].as_u64().unwrap_or(20) as usize;
                Ok(
                    json!({"tasks":tasks.iter().skip(offset).take(limit).collect::<Vec<_>>(),"nextOffset":if offset+limit<tasks.len(){Some(offset+limit)}else{None}}),
                )
            }
            _ => self.app.research(name, args, cancel).await,
        }
    }
    async fn queue_items(
        &self,
        items: &[Value],
        options: &Value,
        cancel: &CancellationToken,
    ) -> Result<Value> {
        let mut results = Vec::new();
        let mut seen = std::collections::HashSet::new();
        for item in items {
            if cancel.is_cancelled() {
                break;
            }
            let source = self.app.resolve_research_url(item);
            let key = source.as_ref().map(String::as_str).unwrap_or_else(|_| {
                item["url"]
                    .as_str()
                    .or(item["sourceRef"].as_str())
                    .unwrap_or("")
            });
            if !seen.insert(key.to_string()) {
                continue;
            }
            let mut result = json!({});
            if let Some(url) = item["url"].as_str() {
                result["url"] = json!(huifa_core::redact_url(url));
            }
            if item["sourceRef"].is_string() {
                result["sourceRef"] = item["sourceRef"].clone();
            }
            match source {
                Ok(url) => match self
                    .app
                    .command(
                        "tasks.add",
                        json!({"urls":[url],"options":options,"report":true}),
                    )
                    .await
                {
                    Ok(report) => {
                        result["taskIds"] = report["ids"].clone();
                        if let Some(item) =
                            report["results"].as_array().and_then(|items| items.first())
                        {
                            for key in ["action", "status", "id"] {
                                result[key] = item[key].clone();
                            }
                        }
                    }
                    Err(error) => {
                        result["error"] = json!(self.app.research_error(&format!("{error:#}")))
                    }
                },
                Err(error) => {
                    result["error"] = json!(self.app.research_error(&format!("{error:#}")))
                }
            }
            results.push(result);
        }
        Ok(json!({"results":results,"cancelled":cancel.is_cancelled()}))
    }
}
fn download_options_schema() -> Value {
    let choice = |values: &str| json!({"type":"string","enum":values.split_whitespace().collect::<Vec<_>>()});
    let text = |max| json!({"type":"string","maxLength":max});
    let boolean = json!({"type":"boolean"});
    let mut properties = json!({
        "quality":choice("best worst 8k 4k 2k 1k 1080p 720p 480p 360p 240p qcif"),
        "contentMode":choice("video audio manual"),"audioFormat":choice("best aac alac flac m4a mp3 opus vorbis wav"),
        "audioTrack":choice("default original all zh-Hans zh-Hant en ja ko es fr de ru pt ar hi id vi th tr"),
        "container":choice("auto mp4 mkv"),"videoFps":choice("best 240 120 60 50 30 25 24"),
        "sourceVideoCodec":choice("auto h264 h265 av1 vp9"),"vrMode":choice("any 2d360 3d180 3d360 none"),
        "compatibilityTarget":choice("auto windows macos linux ios android"),
        "collectionOrder":choice("original reverse random"),"firstN":{"type":"integer","minimum":0,"maximum":1000000},
        "playlistItems":text(200),"dateAfter":text(10),"dateBefore":text(10),
        "durationMin":{"type":"number","minimum":0},"durationMax":{"type":"number","minimum":0}
    });
    let subtitles = json!({
        "liveFilter":choice("videos all live"),"liveFromStart":boolean,"waitForLive":boolean,
        "waitMin":{"type":"integer","minimum":1,"maximum":86400},"waitMax":{"type":"integer","minimum":1,"maximum":86400},
        "sectionStart":text(24),"sectionEnd":text(24),"splitChapters":boolean,
        "subtitleFormat":choice("best vtt srt ass lrc"),"subtitleLanguage":text(40),
        "subtitleLanguages":{"type":"array","maxItems":100,"items":text(40)},"automaticSubtitles":boolean,
        "embedSubtitles":boolean,"writeThumbnail":boolean,"writeDescription":boolean,"writeComments":boolean,"writeInfoJson":boolean,
        "embedMetadata":boolean,"embedChapters":boolean,"embedThumbnail":boolean
    });
    let processing = json!({
        "sponsorblockMode":choice("off mark remove"),"sponsorblockCategories":{"type":"array","maxItems":8,"items":choice("sponsor intro outro selfpromo interaction preview music_offtopic filler")},
        "rateLimit":text(32),"organizeTaskFolder":boolean,"formatId":text(100),"videoFormatId":text(100),
        "audioFormatIds":{"type":"array","maxItems":100,"items":text(100)},
        "fragmentConcurrent":{"type":"integer","minimum":1,"maximum":8},"requestDelay":{"type":"number","minimum":0,"maximum":60},
        "filenameTemplate":text(240),"convertCoverJpeg":boolean,"coverJpegQuality":{"type":"integer","minimum":1,"maximum":100}
    });
    properties
        .as_object_mut()
        .unwrap()
        .extend(subtitles.as_object().unwrap().clone());
    properties
        .as_object_mut()
        .unwrap()
        .extend(processing.as_object().unwrap().clone());
    properties["encoder"] = choice(
        "copy original libx264 libx265 libsvtav1 libaom-av1 librav1e h264_nvenc hevc_nvenc av1_nvenc h264_qsv hevc_qsv av1_qsv h264_amf hevc_amf av1_amf",
    );
    json!({"type":"object","properties":properties,"additionalProperties":false})
}
fn public_options(options: &Value) -> Value {
    let schema = download_options_schema();
    Value::Object(
        options
            .as_object()
            .into_iter()
            .flatten()
            .filter(|(key, _)| schema["properties"].get(*key).is_some())
            .map(|(key, value)| (key.clone(), value.clone()))
            .collect(),
    )
}
fn download_options(saved: Value, args: &Value) -> Result<Value> {
    let mut options = public_options(&saved);
    let mut overrides = args.get("options").cloned().unwrap_or(json!({}));
    for key in [
        "quality",
        "contentMode",
        "formatId",
        "subtitleLanguage",
        "automaticSubtitles",
        "embedSubtitles",
        "fragmentConcurrent",
        "waitForLive",
    ] {
        if let Some(value) = args.get(key) {
            overrides[key] = value.clone();
        }
    }
    let manual = ["formatId", "videoFormatId", "audioFormatIds"]
        .iter()
        .any(|key| overrides.get(*key).is_some());
    if !manual && (overrides.get("quality").is_some() || overrides.get("contentMode").is_some()) {
        options["formatId"] = json!("");
        options["videoFormatId"] = json!("");
        options["audioFormatIds"] = json!([]);
        if options["contentMode"] == "manual" && overrides.get("contentMode").is_none() {
            options["contentMode"] = json!("video");
        }
    }
    options["waitForLive"] = json!(false);
    for (key, value) in overrides.as_object().context("Options must be an object")? {
        options[key] = value.clone();
    }
    // Explicit collection tools resolve their bounded selection before adding single-video tasks.
    options["collectionMode"] = json!("single");
    let mut value = serde_json::to_value(DownloadOptions::parse(options)?)?;
    for key in ["proxy", "cookieId", "processingTempDir"] {
        value.as_object_mut().unwrap().remove(key);
    }
    Ok(value)
}
fn conversion_schema() -> Value {
    let schema = download_options_schema();
    let mut properties = [
        "contentMode",
        "audioFormat",
        "container",
        "encoder",
        "embedSubtitles",
        "embedThumbnail",
        "embedMetadata",
        "embedChapters",
        "subtitleFormat",
    ]
    .iter()
    .map(|key| ((*key).to_string(), schema["properties"][*key].clone()))
    .collect::<serde_json::Map<String, Value>>();
    properties.insert(
        "contentMode".into(),
        json!({"type":"string","enum":["video","audio"]}),
    );
    json!({"type":"object","additionalProperties":false,"properties":properties})
}
fn conversion_options(args: &Value) -> Value {
    let mut options = args.get("options").cloned().unwrap_or(json!({}));
    if let Some(encoder) = args.get("encoder") {
        options["encoder"] = encoder.clone();
    }
    if options.get("encoder").is_none() && options["contentMode"] != "audio" {
        options["encoder"] = json!("libx264");
    }
    options
}
fn feature_groups() -> Value {
    json!([
        {"id":"runtime","label":"Runtime and supported sites","tools":["get_ytdlp_status","list_extractors","list_search_providers"],"support":"Installed yt-dlp; listed extractors do not guarantee every feature"},
        {"id":"research","label":"Source research","tools":["inspect_url","search_videos","get_video_info","list_video_formats","list_collection_items","get_video_transcript","get_video_comments","get_video_chapters","list_subtitle_languages","get_channel_info","list_video_thumbnails"],"support":"Depends on site and extractor"},
        {"id":"download","label":"Downloads and processing","tools":["get_download_options","preview_download","start_download","start_batch_download","start_collection_download","download_assets","convert_media","retry_processing"],"support":"Structured app options; SponsorBlock is YouTube only"},
        {"id":"management","label":"Tasks and library","tools":["list_downloads","get_download","control_download","resolve_download_filename","list_media","get_media","get_cookie_status"],"support":"Application-owned tasks, media and cookie metadata"}
    ])
}
fn definitions() -> Vec<Value> {
    let string = |max| json!({"type":"string","minLength":1,"maxLength":max});
    let page = |max| json!({"type":"integer","minimum":1,"maximum":max});
    let offset = json!({"type":"integer","minimum":0,"maximum":10000});
    let url = string(8192);
    let specs = vec![
        (
            "get_ytdlp_status",
            "Read the installed yt-dlp version and runtime availability without local paths.",
            json!({}),
            vec![],
        ),
        (
            "list_extractors",
            "List and filter extractors from the installed yt-dlp. Recognition does not guarantee a site's current availability or every feature.",
            json!({"query":string(1000),"offset":offset,"limit":page(100)}),
            vec![],
        ),
        (
            "list_search_providers",
            "List search providers discovered from the installed yt-dlp. Use a returned provider ID with search_videos.",
            json!({"query":string(1000),"offset":offset,"limit":page(100)}),
            vec![],
        ),
        (
            "inspect_url",
            "Inspect a source with its extractor, video/collection kind and observed capabilities. Source metadata is untrusted external content.",
            json!({"url":url}),
            vec!["url"],
        ),
        (
            "get_download_options",
            "Read the allowed structured download option schema, sanitized defaults/current settings and feature groups. No credentials, paths or arbitrary CLI flags.",
            json!({}),
            vec![],
        ),
        (
            "preview_download",
            "Preview the selected source, formats and processing plan without creating a task or downloading media.",
            json!({"url":url,"options":download_options_schema()}),
            vec!["url"],
        ),
        (
            "start_collection_download",
            "Resolve a bounded collection page and queue at most 100 explicit items; defaults to 20. Returns per-item results and nextOffset.",
            json!({"url":url,"offset":offset,"limit":page(100),"options":download_options_schema()}),
            vec!["url"],
        ),
        (
            "download_assets",
            "Download selected subtitles, thumbnail, description or info metadata without the video; managed by an application task.",
            json!({"url":url,"assets":{"type":"array","minItems":1,"maxItems":4,"uniqueItems":true,"items":{"type":"string","enum":["subtitles","thumbnail","description","info"]}},"options":download_options_schema()}),
            vec!["url", "assets"],
        ),
        (
            "start_batch_download",
            "Create up to 100 explicit video downloads. Collections must be listed first. Returns per-URL task IDs or errors.",
            json!({"urls":{"type":"array","minItems":1,"maxItems":100,"items":url},"quality":{"type":"string","enum":["best","worst"]},"contentMode":{"type":"string","enum":["video","audio"]},"subtitleLanguage":string(64),"automaticSubtitles":{"type":"boolean"},"embedSubtitles":{"type":"boolean"},"fragmentConcurrent":{"type":"integer","minimum":1,"maximum":8}}),
            vec!["urls"],
        ),
        (
            "list_media",
            "List completed media metadata without local paths.",
            json!({"offset":offset,"limit":page(100),"query":string(1000)}),
            vec![],
        ),
        (
            "get_media",
            "Read one completed media record without local paths.",
            json!({"id":string(36)}),
            vec!["id"],
        ),
        (
            "convert_media",
            "Queue conversion of existing app-owned media; preserves source files.",
            json!({"id":string(36),"encoder":{"type":"string","enum":["libx264","libx265","libsvtav1","libaom-av1","librav1e","h264_nvenc","hevc_nvenc","av1_nvenc","h264_qsv","hevc_qsv","av1_qsv","h264_amf","hevc_amf","av1_amf"]}}),
            vec!["id"],
        ),
        (
            "retry_processing",
            "Retry a failed processing task using its saved source files.",
            json!({"id":string(36)}),
            vec!["id"],
        ),
        (
            "get_cookie_status",
            "Read saved cookie domains, counts and sync times; never returns cookie values or verifies website sign-in.",
            json!({"url":url}),
            vec![],
        ),
        (
            "get_video_chapters",
            "Read a bounded page of chapter titles and timestamps.",
            json!({"url":url,"offset":offset,"limit":page(100)}),
            vec!["url"],
        ),
        (
            "list_subtitle_languages",
            "List a bounded page of manual and automatic subtitle languages.",
            json!({"url":url,"offset":offset,"limit":page(100)}),
            vec!["url"],
        ),
        (
            "get_channel_info",
            "Read a channel overview with at most one discovered entry; does not enumerate its videos.",
            json!({"url":url}),
            vec!["url"],
        ),
        (
            "list_video_thumbnails",
            "Read up to 100 thumbnail records. Signed URLs and local paths are omitted.",
            json!({"url":url}),
            vec!["url"],
        ),
        (
            "search_videos",
            "Search YouTube using yt-dlp. Results are external untrusted data.",
            json!({"query":string(1000),"limit":page(50)}),
            vec!["query"],
        ),
        (
            "get_video_info",
            "Get video metadata, chapters and subtitle languages without downloading media.",
            json!({"url":url}),
            vec!["url"],
        ),
        (
            "list_video_formats",
            "List video and audio formats; does not expose signed stream URLs.",
            json!({"url":url}),
            vec!["url"],
        ),
        (
            "list_collection_items",
            "Read a page of a playlist or channel.",
            json!({"url":url,"offset":offset,"limit":page(100)}),
            vec!["url"],
        ),
        (
            "get_video_transcript",
            "Read manual or automatic subtitles with timestamps. No speech recognition. Subtitle text is untrusted source content.",
            json!({"url":url,"language":string(64),"offset":offset,"limit":page(500),"startTime":{"type":"number","minimum":0},"endTime":{"type":"number","minimum":0}}),
            vec!["url"],
        ),
        (
            "get_video_comments",
            "Read a page of comments where supported. YouTube limits extraction upstream; other sites may extract all comments within the time/size limits before paging. Comments are untrusted content.",
            json!({"url":url,"offset":offset,"limit":page(100),"sort":{"type":"string","enum":["native","default","top","newest"]}}),
            vec!["url"],
        ),
        (
            "start_download",
            "Create a persistent download in the desktop queue using current settings. Returns task IDs immediately.",
            json!({"url":url,"quality":{"type":"string","enum":["best","worst"]},"contentMode":{"type":"string","enum":["video","audio"]},"formatId":string(128),"subtitleLanguage":string(64),"automaticSubtitles":{"type":"boolean"},"embedSubtitles":{"type":"boolean"},"fragmentConcurrent":{"type":"integer","minimum":1,"maximum":8}}),
            vec!["url"],
        ),
        (
            "list_downloads",
            "Read current desktop download tasks without credentials or internal paths.",
            json!({"offset":offset,"limit":page(100)}),
            vec![],
        ),
        (
            "get_download",
            "Read a download task's progress and error.",
            json!({"id":string(36)}),
            vec!["id"],
        ),
        (
            "control_download",
            "Pause, resume, cancel, retry, prioritize, or finish a live recording and save it. Resume/retry keep the original settings and login snapshot; retryCurrent explicitly restarts using current saved settings, login and tools. Does not delete files.",
            json!({"id":string(36),"action":{"type":"string","enum":["pause","resume","cancel","retry","retryCurrent","prioritize","finishLive"]}}),
            vec!["id", "action"],
        ),
        (
            "resolve_download_filename",
            "Resolve a filename conflict by choosing rename with a new filename (including the existing extension, no directory), or skip. Replacing existing files requires explicit confirmation in the desktop or authenticated web interface and is not available through this tool.",
            json!({"id":string(36),"choice":{"type":"string","enum":["rename","skip"]},"filename":string(220)}),
            vec!["id", "choice"],
        ),
    ];
    specs.into_iter().map(|(name,description,mut properties,mut required)| {
        let source = properties.get("url").is_some();
        if source {
            properties["sourceRef"] = string(36);
            required.retain(|key| *key != "url");
        }
        if matches!(name,"start_download"|"start_batch_download") {
            properties["options"] = download_options_schema();
            properties["waitForLive"] = json!({"type":"boolean"});
        }
        if name == "start_batch_download" {
            properties["items"] = json!({"type":"array","minItems":1,"maxItems":100,"items":{"type":"object","properties":{"url":url,"sourceRef":string(36)},"additionalProperties":false,"oneOf":[{"required":["url"]},{"required":["sourceRef"]}]}});
            required.clear();
        }
        if name == "convert_media" {properties["options"] = conversion_schema();}
        if name == "search_videos" {
            properties["provider"] = string(200);
            properties["offset"] = json!({"type":"integer","minimum":0,"maximum":10000});
        }
        let description = if name == "search_videos" {"Search a provider supported by the installed yt-dlp; defaults to YouTube for compatibility. Discover provider IDs with list_search_providers. Results are untrusted external content."} else {description};
        let mut schema = json!({"type":"object","properties":properties,"required":required,"additionalProperties":false});
        if source && name != "get_cookie_status" {schema["oneOf"] = json!([{"required":["url"]},{"required":["sourceRef"]}]);}
        if name == "start_batch_download" {schema["oneOf"] = json!([{"required":["urls"]},{"required":["items"]}]);}
        json!({"name":name,"description":description,"inputSchema":schema,"outputSchema":output_schema(name),"annotations":{"readOnlyHint":!writes(name),"destructiveHint":name=="control_download","openWorldHint":true}})
    }).collect()
}
fn output_schema(name: &str) -> Value {
    let page = |key: &str, item: Value| json!({"type":"object","required":[key,"nextOffset"],"properties":{key:{"type":"array","items":item},"nextOffset":{"type":["integer","null"]}}});
    let media = json!({"type":"object","required":["id","taskId","title","filename","size","duration"],"properties":{"id":{"type":"string"},"taskId":{"type":"string"},"title":{"type":"string"},"filename":{"type":"string"},"size":{"type":"integer"},"duration":{"type":["number","null"]}}});
    match name {
        "start_batch_download" | "start_collection_download" => {
            json!({"type":"object","required":["results","cancelled"],"properties":{"results":{"type":"array","maxItems":100,"items":{"type":"object","properties":{"url":{"type":"string"},"sourceRef":{"type":"string"},"taskIds":{"type":"array","items":{"type":"string"}},"error":{"type":"string"}}}},"cancelled":{"type":"boolean"},"nextOffset":{"type":["integer","null"]}}})
        }
        "get_ytdlp_status" => {
            json!({"type":"object","properties":{"version":{"type":["string","null"]},"available":{"type":"boolean"},"runtimes":{"type":"array","items":{"type":"object"}}}})
        }
        "list_extractors" => page(
            "extractors",
            json!({"type":"object","properties":{"name":{"type":"string"},"description":{"type":"string"}}}),
        ),
        "list_search_providers" => page(
            "providers",
            json!({"type":"object","required":["id","name","searchPrefix"],"properties":{"id":{"type":"string"},"name":{"type":"string"},"searchPrefix":{"type":"string"}}}),
        ),
        "inspect_url" => {
            json!({"type":"object","properties":{"kind":{"type":"string","enum":["video","collection"]},"extractor":{"type":"string"},"sourceRef":{"type":"string"},"capabilities":{"type":"object"}}})
        }
        "get_download_options" => {
            json!({"type":"object","required":["schema","defaults","current","features"],"properties":{"schema":{"type":"object"},"defaults":download_options_schema(),"current":download_options_schema(),"features":{"type":"array","items":{"type":"object"}}}})
        }
        "preview_download" => {
            json!({"type":"object","required":["formats","warnings","plan","eligible","createsTask"],"properties":{"sourceRef":{"type":"string"},"formats":{"type":"array","items":{"type":"object"}},"selectedFormats":{"type":"array","items":{"type":"object"}},"warnings":{"type":"array","items":{"type":"string"}},"plan":{"type":"object"},"eligible":{"type":"boolean"},"createsTask":{"type":"boolean","const":false}}})
        }
        "download_assets" => {
            json!({"type":"object","required":["taskIds","kind","files"],"properties":{"taskIds":{"type":"array","items":{"type":"string"}},"kind":{"type":"string","const":"assets"},"files":{"type":"array","items":{"type":"object"}}}})
        }
        "list_media" => page("media", media),
        "get_media" => media,
        "convert_media" | "retry_processing" => {
            json!({"type":"object","required":["task"],"properties":{"task":{"type":"object"}}})
        }
        "get_cookie_status" => {
            json!({"type":"object","required":["sites","hasMatchingCookies"],"properties":{"sites":{"type":"array","items":{"type":"object","properties":{"domains":{"type":"array","items":{"type":"string"}},"count":{"type":"integer"},"updatedAt":{"type":"string"}}}},"hasMatchingCookies":{"type":["boolean","null"]}}})
        }
        "get_video_chapters" => page(
            "chapters",
            json!({"type":"object","properties":{"title":{"type":"string"},"start_time":{"type":"number"},"end_time":{"type":"number"}}}),
        ),
        "list_subtitle_languages" => page(
            "languages",
            json!({"type":"object","required":["language","manual","automatic"],"properties":{"language":{"type":"string"},"manual":{"type":"boolean"},"automatic":{"type":"boolean"}}}),
        ),
        "get_channel_info" => {
            json!({"type":"object","properties":{"id":{"type":["string","null"]},"title":{"type":["string","null"]},"description":{"type":["string","null"]},"channel":{"type":["string","null"]},"channel_id":{"type":["string","null"]},"channel_follower_count":{"type":["integer","null"]},"playlist_count":{"type":["integer","null"]}}})
        }
        "list_video_thumbnails" => {
            json!({"type":"object","required":["thumbnails"],"properties":{"thumbnails":{"type":"array","maxItems":100,"items":{"type":"object","properties":{"id":{"type":["string","null"]},"width":{"type":["number","null"]},"height":{"type":["number","null"]},"url":{"type":"string"}}}}}})
        }
        "get_video_comments" => page(
            "comments",
            json!({"type":"object","properties":{"id":{"type":["string","null"]},"text":{"type":["string","null"]},"author":{"type":["string","null"]}}}),
        ),
        _ => json!({"type":"object"}),
    }
}

fn validate(name: &str, args: &Value) -> Result<()> {
    let definition = definitions()
        .into_iter()
        .find(|v| v["name"] == name)
        .context("Unknown MCP tool")?;
    let schema = &definition["inputSchema"];
    validate_rule(schema, args, "arguments")?;
    let object = args.as_object().context("Arguments must be an object")?;
    for key in schema["required"].as_array().unwrap() {
        ensure!(
            object.contains_key(key.as_str().unwrap()),
            "Missing required argument: {key}"
        );
    }
    ensure!(
        !(args.get("url").is_some() && args.get("sourceRef").is_some()),
        "Use either url or sourceRef, not both"
    );
    if let Some(url) = args["url"].as_str() {
        validate_url(url)?;
    }
    if let Some(id) = args["id"].as_str() {
        uuid::Uuid::parse_str(id).context("Invalid task ID")?;
    }
    if name == "resolve_download_filename" && args["choice"] == "rename" {
        ensure!(
            args["filename"]
                .as_str()
                .is_some_and(|name| !name.is_empty()),
            "New filename is required"
        );
    }
    if let Some(id) = args["sourceRef"].as_str() {
        uuid::Uuid::parse_str(id).context("Invalid source reference")?;
    }
    if let Some(end) = args["endTime"].as_f64() {
        ensure!(
            end > args["startTime"].as_f64().unwrap_or(0.),
            "endTime must be after startTime"
        );
    }
    Ok(())
}

fn validate_rule(rule: &Value, value: &Value, key: &str) -> Result<()> {
    match rule["type"].as_str().context("Invalid argument schema")? {
        "object" => {
            let object = value.as_object().context("Argument must be an object")?;
            if let Some(required) = rule["required"].as_array() {
                for required in required {
                    ensure!(
                        object.contains_key(required.as_str().unwrap()),
                        "Missing required argument: {required}"
                    );
                }
            }
            if let Some(one_of) = rule["oneOf"].as_array() {
                let matches = one_of
                    .iter()
                    .filter(|variant| {
                        variant["required"]
                            .as_array()
                            .unwrap()
                            .iter()
                            .all(|required| object.contains_key(required.as_str().unwrap()))
                    })
                    .count();
                ensure!(matches == 1, "Choose exactly one input source: {key}");
            }
            for (name, value) in object {
                let nested = &rule["properties"][name];
                ensure!(!nested.is_null(), "Unknown argument: {name}");
                validate_rule(nested, value, name)?;
            }
        }
        "string" => {
            let value = value.as_str().context("Argument must be text")?;
            ensure!(
                value.len() >= rule["minLength"].as_u64().unwrap_or(0) as usize
                    && value.len() <= rule["maxLength"].as_u64().unwrap_or(8192) as usize
                    && !value.chars().any(char::is_control),
                "Invalid length or characters: {key}"
            );
        }
        "integer" => {
            let value = value
                .as_u64()
                .context("Argument must be a nonnegative integer")?;
            ensure!(
                value >= rule["minimum"].as_u64().unwrap_or(0)
                    && value <= rule["maximum"].as_u64().unwrap_or(u64::MAX),
                "Argument out of range: {key}"
            );
        }
        "number" => ensure!(
            value.as_f64().is_some_and(|value| value.is_finite()
                && value >= rule["minimum"].as_f64().unwrap_or(0.)
                && value <= rule["maximum"].as_f64().unwrap_or(f64::MAX)),
            "Argument out of range: {key}"
        ),
        "boolean" => ensure!(value.is_boolean(), "Argument must be boolean: {key}"),
        "array" => {
            let values = value.as_array().context("Argument must be an array")?;
            ensure!(
                values.len() >= rule["minItems"].as_u64().unwrap_or(0) as usize
                    && values.len() <= rule["maxItems"].as_u64().unwrap_or(100) as usize,
                "Invalid array length: {key}"
            );
            for (index, value) in values.iter().enumerate() {
                validate_rule(&rule["items"], value, key)?;
                ensure!(
                    rule["uniqueItems"] != true || !values[..index].contains(value),
                    "Duplicate item: {key}"
                );
            }
        }
        _ => anyhow::bail!("Invalid argument schema"),
    }
    if let Some(allowed) = rule["enum"].as_array() {
        ensure!(allowed.contains(value), "Unsupported value: {key}");
    }
    Ok(())
}

fn validate_url(value: &str) -> Result<()> {
    let url = reqwest::Url::parse(value)?;
    ensure!(
        matches!(url.scheme(), "http" | "https")
            && url.host_str().is_some()
            && url.username().is_empty()
            && url.password().is_none(),
        "Use an HTTP(S) URL without credentials"
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn auto_start_migrates_legacy_enabled_and_stays_independent() -> Result<()> {
        let root = tempfile::tempdir()?;
        let app = App::open(huifa_core::AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("downloads"),
            tool_dir: None,
        })
        .await?;
        let host = DesktopMcp::new(app.clone());
        assert_eq!(host.status()?["autoStart"], false);
        app.mcp_save_settings(json!({"enabled":true,"downloads":true,"port":19492}))?;
        assert_eq!(host.status()?["autoStart"], true);
        host.configure(false, true, 19492, None).await?;
        assert_eq!(host.status()?["autoStart"], true);
        host.configure(false, true, 19492, Some(false)).await?;
        assert_eq!(host.status()?["autoStart"], false);
        let handler = Handler {
            app: app.clone(),
            stop: CancellationToken::new(),
            generation: app.access.generation(),
        };
        app.access.change_password("admin", "changed-password")?;
        assert!(
            handler
                .execute("list_downloads", &json!({}), &CancellationToken::new())
                .await
                .unwrap_err()
                .to_string()
                .contains("reconnect MCP")
        );
        app.shutdown().await?;
        Ok(())
    }
    #[tokio::test]
    async fn expanded_tools_enforce_permissions_limits_and_metadata_boundaries() -> Result<()> {
        let root = tempfile::tempdir()?;
        let app = App::open(huifa_core::AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("downloads"),
            tool_dir: None,
        })
        .await?;
        app.mcp_save_settings(json!({"enabled":true,"downloads":false,"port":19492}))?;
        app.command("cookies.import", json!({"name":"fixture","text":"# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tTRUE\t0\tSID\tnever-return-this-value\n"})).await?;
        let handler = Handler {
            app: app.clone(),
            stop: CancellationToken::new(),
            generation: app.access.generation(),
        };
        let cancel = CancellationToken::new();
        for name in [
            "start_batch_download",
            "start_collection_download",
            "download_assets",
            "convert_media",
            "retry_processing",
        ] {
            assert!(
                handler
                    .execute(name, &json!({}), &cancel)
                    .await
                    .unwrap_err()
                    .to_string()
                    .contains("Enable MCP download management")
            );
        }
        let cookies = handler
            .execute(
                "get_cookie_status",
                &json!({"url":"https://www.example.com/video"}),
                &cancel,
            )
            .await?;
        assert_eq!(cookies["hasMatchingCookies"], true);
        let options = handler
            .execute("get_download_options", &json!({}), &cancel)
            .await?;
        assert_eq!(options["defaults"]["fragmentConcurrent"], 4);
        for private in ["cookieId", "proxy", "processingTempDir"] {
            assert!(options["schema"]["properties"].get(private).is_none());
            assert!(options["current"].get(private).is_none());
        }
        assert!(!cookies.to_string().contains("never-return"));
        assert!(
            !cookies
                .to_string()
                .contains(root.path().to_string_lossy().as_ref())
        );
        assert_eq!(
            handler
                .execute("list_media", &json!({"limit":1}), &cancel)
                .await?["media"],
            json!([])
        );
        assert!(
            validate(
                "start_batch_download",
                &json!({"urls":vec!["https://example.com/";101]})
            )
            .is_err()
        );
        assert!(validate("start_batch_download", &json!({"urls":[42]})).is_err());
        assert!(
            validate(
                "get_video_chapters",
                &json!({"url":"https://example.com/","limit":101})
            )
            .is_err()
        );
        assert!(
            validate(
                "convert_media",
                &json!({"id":uuid::Uuid::new_v4(),"encoder":"arbitrary-command"})
            )
            .is_err()
        );
        assert!(
            validate(
                "start_download",
                &json!({"url":"https://example.com/","fragmentConcurrent":9})
            )
            .is_err()
        );
        app.mcp_save_settings(json!({"enabled":true,"downloads":true,"port":19492}))?;
        let result = handler.execute("start_batch_download", &json!({"urls":["file:///private","https://user:private@example.com/a","file:///private"]}), &cancel).await?;
        assert_eq!(result["results"].as_array().unwrap().len(), 2);
        assert!(result["results"][0]["error"].is_string());
        assert!(!result.to_string().contains("user:private"));
        assert!(
            handler
                .execute("get_media", &json!({"id":uuid::Uuid::new_v4()}), &cancel)
                .await
                .is_err()
        );
        app.shutdown().await?;
        Ok(())
    }
    #[test]
    fn boundaries() {
        assert!(validate("search_videos", &json!({"query":"rust","limit":50})).is_ok());
        for args in [
            json!({"query":"rust","limit":51}),
            json!({"query":"rust","exec":"evil"}),
        ] {
            assert!(validate("search_videos", &args).is_err());
        }
        assert!(validate("get_video_info", &json!({"url":"file:///secret"})).is_err());
        assert_eq!(
            classify_error("Metadata stdout exceeds its byte limit").0,
            "result_too_large"
        );
        assert_eq!(
            classify_error("Comments were not provided by this site's extractor for this media").0,
            "unsupported_operation"
        );
        assert!(
            validate(
                "control_download",
                &json!({"id":uuid::Uuid::new_v4(),"action":"delete"})
            )
            .is_err()
        );
        for definition in definitions() {
            serde_json::from_value::<Tool>(definition).unwrap();
        }
    }
    #[test]
    fn recovery_controls_and_filename_resolution_are_bounded() {
        let id = uuid::Uuid::new_v4().to_string();
        for action in ["retryCurrent", "prioritize", "finishLive"] {
            assert!(validate("control_download", &json!({"id":id,"action":action})).is_ok());
        }
        assert!(writes("resolve_download_filename"));
        assert_eq!(
            classify_error("Requirements unmet: subtitles=ja").0,
            "requirements_unmet"
        );
        assert_eq!(
            classify_error("Destination conflict: choose a name").0,
            "filename_conflict"
        );
        assert!(
            validate(
                "resolve_download_filename",
                &json!({"id":id,"choice":"rename","filename":"Another title.mp4"})
            )
            .is_ok()
        );
        assert!(
            validate(
                "resolve_download_filename",
                &json!({"id":id,"choice":"skip"})
            )
            .is_ok()
        );
        for args in [
            json!({"id":id,"choice":"overwrite","confirmed":true}),
            json!({"id":id,"choice":"rename"}),
            json!({"id":id,"choice":"rename","filename":"x".repeat(221)}),
            json!({"id":id,"choice":"skip","path":"C:/private"}),
        ] {
            assert!(validate("resolve_download_filename", &args).is_err());
        }
    }

    #[test]
    fn structured_options_sources_and_precedence_are_bounded() -> Result<()> {
        let source = uuid::Uuid::new_v4().to_string();
        assert!(validate("get_video_info", &json!({"sourceRef":source})).is_ok());
        for args in [
            json!({}),
            json!({"sourceRef":"bad"}),
            json!({"url":"https://example.com/","sourceRef":source}),
        ] {
            assert!(validate("get_video_info", &args).is_err());
        }
        assert!(
            validate(
                "start_batch_download",
                &json!({"items":[{"sourceRef":source},{"url":"https://example.com/"}]})
            )
            .is_ok()
        );
        assert!(
            validate(
                "start_batch_download",
                &json!({"urls":["https://example.com/"],"items":[{"sourceRef":source}]})
            )
            .is_err()
        );
        for forbidden in [
            "cookieId",
            "proxy",
            "processingTempDir",
            "exec",
            "extractorArgs",
            "output",
        ] {
            let mut args = json!({"url":"https://example.com/","options":{}});
            args["options"][forbidden] = json!("private");
            assert!(validate("start_download", &args).is_err(), "{forbidden}");
        }
        assert!(
            validate(
                "download_assets",
                &json!({"url":"https://example.com/","assets":["comments"]})
            )
            .is_err()
        );
        assert!(
            validate(
                "download_assets",
                &json!({"url":"https://example.com/","assets":["thumbnail","thumbnail"]})
            )
            .is_err()
        );
        assert!(validate("start_download",&json!({"url":"https://example.com/","options":{"audioFormat":"flac","sectionStart":"2","fragmentConcurrent":8}})).is_ok());
        let saved = json!({"quality":"worst","fragmentConcurrent":32,"waitForLive":true,"collectionMode":"all","cookieId":"private","proxy":"http://private","processingTempDir":"C:/private","formatId":"old","videoFormatId":"old","audioFormatIds":["old"]});
        let merged = download_options(
            saved.clone(),
            &json!({"options":{"quality":"720p","fragmentConcurrent":6},"quality":"best"}),
        )?;
        assert_eq!(merged["quality"], "best");
        assert_eq!(merged["fragmentConcurrent"], 6);
        assert_eq!(merged["waitForLive"], false);
        assert_eq!(merged["collectionMode"], "single");
        assert_eq!(merged["formatId"], "");
        assert_eq!(merged["audioFormatIds"], json!([]));
        assert!(merged.get("proxy").is_none());
        assert!(merged.get("cookieId").is_none());
        assert_eq!(
            download_options(saved, &json!({"options":{"waitForLive":true}}))?["waitForLive"],
            true
        );
        assert_eq!(definitions().len(), 29);
        assert_eq!(
            definitions()
                .iter()
                .filter(|tool| !writes(tool["name"].as_str().unwrap()))
                .count(),
            21
        );
        Ok(())
    }

    #[tokio::test]
    async fn http_protocol_password_sessions_and_loopback_guards() -> Result<()> {
        use rmcp::{
            ServiceExt,
            transport::{
                StreamableHttpClientTransport,
                streamable_http_client::StreamableHttpClientTransportConfig,
            },
        };
        let root = tempfile::tempdir()?;
        let app = App::open(huifa_core::AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("downloads"),
            tool_dir: None,
        })
        .await?;
        let host = DesktopMcp::new(app.clone());
        assert_eq!(
            host.status()?,
            json!({"enabled":false,"downloads":false,"port":19492,"autoStart":false})
        );
        let socket = std::net::TcpListener::bind("127.0.0.1:0")?;
        let port = socket.local_addr()?.port();
        drop(socket);
        host.configure(true, false, port, None).await?;
        app.command(
            "settings.save",
            json!({"settings":{"mcp":{"enabled":false}}}),
        )
        .await?;
        assert_eq!(
            host.status()?["enabled"],
            true,
            "A stale settings draft must not change the listener"
        );
        let url = format!("http://127.0.0.1:{port}/mcp");
        let http = reqwest::Client::new();
        assert_eq!(
            http.post(&url)
                .header("Origin", "https://evil.example")
                .bearer_auth("admin")
                .send()
                .await?
                .status(),
            StatusCode::FORBIDDEN
        );
        assert_eq!(
            http.post(&url)
                .header("Host", "evil.example")
                .bearer_auth("admin")
                .send()
                .await?
                .status(),
            StatusCode::FORBIDDEN
        );
        let client = ()
            .serve(StreamableHttpClientTransport::from_config(
                StreamableHttpClientTransportConfig::with_uri(url.clone()).auth_header("admin"),
            ))
            .await?;
        let tools = client.list_tools(None).await?;
        assert_eq!(tools.tools.len(), 21);
        let request = CallToolRequestParams::new("list_downloads");
        let result = client.call_tool(request).await?;
        assert_ne!(result.is_error, Some(true));
        let denied = client
            .call_tool(
                CallToolRequestParams::new("start_download").with_arguments(
                    json!({"url":"https://www.youtube.com/watch?v=abc"})
                        .as_object()
                        .unwrap()
                        .clone(),
                ),
            )
            .await?;
        assert_eq!(denied.is_error, Some(true));
        let connection = host.connection("bridge")?;
        assert_eq!(
            connection["http"],
            json!({"mcpServers":{"huifa":{"url":url,"headers":{"Authorization":"Bearer YOUR_ACCESS_PASSWORD"}}}})
        );
        assert!(connection.get("token").is_none());
        assert_eq!(connection["tools"].as_array().unwrap().len(), 29);
        assert!(!connection.to_string().contains("test-password"));
        assert_eq!(
            http.post(&url).send().await?.status(),
            StatusCode::UNAUTHORIZED
        );
        assert_eq!(
            http.post(&url)
                .bearer_auth("incorrect")
                .send()
                .await?
                .status(),
            StatusCode::UNAUTHORIZED
        );
        app.access.change_password("admin", "changed-password")?;
        assert_eq!(
            http.post(&url).bearer_auth("admin").send().await?.status(),
            StatusCode::UNAUTHORIZED
        );
        assert!(client.list_tools(None).await.is_err());
        client.cancel().await?;
        host.configure(true, true, port, None).await?;
        let client = ()
            .serve(StreamableHttpClientTransport::from_config(
                StreamableHttpClientTransportConfig::with_uri(url).auth_header("changed-password"),
            ))
            .await?;
        assert_eq!(client.list_tools(None).await?.tools.len(), 29);
        client.cancel().await?;
        host.shutdown().await;
        app.shutdown().await?;
        Ok(())
    }
}

mod auth;
mod browser;
mod browser_network;
mod files;

use anyhow::Result;
use axum::{
    Extension, Json, Router,
    extract::{ConnectInfo, DefaultBodyLimit, Multipart, Path, Query, State},
    http::{HeaderMap, HeaderValue, StatusCode, header},
    middleware::{self, Next},
    response::{
        IntoResponse, Response, Sse,
        sse::{Event, KeepAlive},
    },
    routing::{any, get, post},
};
use futures_util::Stream;
use huifa_core::{App, AppConfig};
use serde::Deserialize;
use serde_json::{Value, json};
use std::{
    convert::Infallible,
    net::{IpAddr, Ipv4Addr, SocketAddr},
    path::PathBuf,
    sync::Arc,
    time::Duration,
};
use tokio_util::sync::CancellationToken;
use tower::ServiceExt;
use tower_http::services::{ServeDir, ServeFile};

pub struct Config {
    pub account: String,
    pub password: String,
    pub public_origin: Option<String>,
    pub data_dir: PathBuf,
    pub download_dir: PathBuf,
    pub tool_dir: Option<PathBuf>,
    pub frontend_dir: PathBuf,
    pub chromium: PathBuf,
}

pub struct Server {
    app: Arc<App>,
    mcp: huifa_mcp::DesktopMcp,
    mcp_transport: tokio::sync::RwLock<(CancellationToken, Router)>,
    public_origin: Option<String>,
    auth: Arc<auth::Auth>,
    browser: Arc<browser::Browser>,
    assets: PathBuf,
    tasks: PathBuf,
    downloads: PathBuf,
    frontend: PathBuf,
    shutdown: CancellationToken,
}

type HttpResult<T> = std::result::Result<T, ApiError>;

pub struct ApiError(StatusCode, String);
impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (self.0, Json(json!({"error":self.1}))).into_response()
    }
}
impl From<StatusCode> for ApiError {
    fn from(status: StatusCode) -> Self {
        Self(
            status,
            status.canonical_reason().unwrap_or("request failed").into(),
        )
    }
}
fn failed(error: anyhow::Error) -> ApiError {
    ApiError(
        StatusCode::BAD_REQUEST,
        huifa_core::process::redact(&format!("{error:#}")),
    )
}

impl Server {
    pub async fn open(config: Config) -> Result<Arc<Self>> {
        huifa_core::access::Access::initialize(
            &config.data_dir,
            &config.account,
            &config.password,
        )?;
        let app = App::open(AppConfig {
            data_dir: config.data_dir.clone(),
            download_dir: config.download_dir.clone(),
            tool_dir: config.tool_dir,
        })
        .await?;
        let auth = Arc::new(auth::Auth::new(app.clone(), config.public_origin.clone())?);
        let assets = config.data_dir.join("assets");
        std::fs::create_dir_all(&assets)?;
        anyhow::ensure!(
            !std::fs::symlink_metadata(&assets)?.file_type().is_symlink(),
            "asset storage must not be a link"
        );
        let assets = assets.canonicalize()?;
        let tasks = config.data_dir.join("tasks");
        std::fs::create_dir_all(&tasks)?;
        let tasks = tasks.canonicalize()?;
        let downloads = config.download_dir.canonicalize()?;
        let browser = browser::Browser::new(
            app.clone(),
            config.chromium,
            config.data_dir.join("browser"),
        )?;
        let mcp = huifa_mcp::DesktopMcp::new(app.clone());
        // The container uses its existing web port rather than a second loopback listener.
        let saved = mcp.status()?;
        app.mcp_save_settings(json!({"enabled":saved["autoStart"]==true,"autoStart":saved["autoStart"]==true,"downloads":true,"port":saved["port"].as_u64().unwrap_or(19492)}))?;
        let shutdown = CancellationToken::new();
        let mcp_stop = shutdown.child_token();
        let mcp_router = huifa_mcp::web_router(app.clone(), mcp_stop.clone());
        Ok(Arc::new(Self {
            mcp,
            mcp_transport: tokio::sync::RwLock::new((mcp_stop, mcp_router)),
            public_origin: config.public_origin,
            app,
            auth,
            browser,
            assets,
            tasks,
            downloads,
            frontend: config.frontend_dir,
            shutdown,
        }))
    }

    pub fn router(self: &Arc<Self>) -> Router {
        let static_files = ServeDir::new(&self.frontend)
            .not_found_service(ServeFile::new(self.frontend.join("index.html")));
        Router::new()
            .route_service("/mcp", ServeFile::new(self.frontend.join("index.html")))
            .route_service("/mcp/", ServeFile::new(self.frontend.join("index.html")))
            .route("/mcp-server", any(mcp_dispatch))
            .route("/api/session", get(session))
            .route(
                "/api/login",
                post(login).layer(DefaultBodyLimit::max(32 * 1024)),
            )
            .route("/api/logout", post(logout))
            .route("/api/mcp", get(mcp_status).post(mcp_configure))
            .route(
                "/api/command",
                post(command).layer(DefaultBodyLimit::max(3 * 1024 * 1024)),
            )
            .route("/api/events", get(events))
            .route("/api/files/{id}", get(file))
            .route("/api/upload", post(upload))
            .route("/api/browser", get(browser_status))
            .route(
                "/api/browser/start",
                post(browser_start).layer(DefaultBodyLimit::max(16 * 1024)),
            )
            .route(
                "/api/browser/input",
                post(browser_input).layer(DefaultBodyLimit::max(64 * 1024)),
            )
            .route(
                "/api/browser/save",
                post(browser_save).layer(DefaultBodyLimit::max(1024)),
            )
            .route("/api/browser/stop", post(browser_stop))
            .route("/api/browser/events", get(browser_events))
            .route("/api/{*path}", get(api_missing).post(api_missing))
            .fallback_service(static_files)
            .layer(DefaultBodyLimit::max(26 * 1024 * 1024))
            .layer(middleware::from_fn(security_headers))
            .with_state(self.clone())
    }

    async fn configure_mcp(&self, enabled: Option<bool>, auto_start: Option<bool>) -> Result<()> {
        let mut transport = self.mcp_transport.write().await;
        let config = self.mcp.status()?;
        let enabled = enabled.unwrap_or(config["enabled"] == true);
        self.app.mcp_save_settings(json!({"enabled":enabled,"autoStart":auto_start.unwrap_or(config["autoStart"] == true),"downloads":true,"port":config["port"].as_u64().unwrap_or(19492)}))?;
        if auto_start.is_some() && config["enabled"] == enabled {
            return Ok(());
        }
        transport.0.cancel();
        let stop = self.shutdown.child_token();
        *transport = (stop.clone(), huifa_mcp::web_router(self.app.clone(), stop));
        Ok(())
    }

    pub async fn shutdown(&self) {
        self.shutdown.cancel();
        self.browser.shutdown().await;
        if let Err(error) = self.app.shutdown().await {
            tracing::error!("core shutdown failed: {error}");
        }
    }
}

async fn mcp_dispatch(
    State(server): State<Arc<Server>>,
    request: axum::extract::Request,
) -> Response {
    let headers = request.headers();
    let Some(authority) = headers.get(header::HOST).and_then(|h| h.to_str().ok()) else {
        return StatusCode::BAD_REQUEST.into_response();
    };
    let allowed = if let Some(origin) = &server.public_origin {
        reqwest::Url::parse(origin).ok().is_some_and(|expected| {
            reqwest::Url::parse(&format!("{}://{authority}", expected.scheme()))
                .is_ok_and(|actual| actual.origin() == expected.origin())
        })
    } else {
        reqwest::Url::parse(&format!("http://{authority}")).is_ok_and(|url| {
            url.username().is_empty()
                && url.host_str().is_some_and(|host| {
                    host == "localhost" || host.trim_matches(['[', ']']).parse::<IpAddr>().is_ok()
                })
        })
    };
    if !allowed {
        return StatusCode::FORBIDDEN.into_response();
    }
    let router = server.mcp_transport.read().await.1.clone();
    router.oneshot(request).await.unwrap()
}

fn mcp_state(server: &Server) -> HttpResult<Value> {
    let config = server.mcp.status().map_err(failed)?;
    let mut connection = server.mcp.connection("").map_err(failed)?;
    connection.as_object_mut().unwrap().remove("stdio");
    // The browser builds the external URL from its origin, including Docker port mapping.
    connection["url"] = json!("/mcp-server");
    connection["http"] = json!({"mcpServers":{"huifa":{"url":"/mcp-server","headers":{"Authorization":"Bearer YOUR_ACCESS_PASSWORD"}}}});
    connection["codex"] = json!("");
    Ok(
        json!({"enabled":config["enabled"]==true,"autoStart":config["autoStart"]==true,"connection":connection}),
    )
}
async fn mcp_status(
    State(server): State<Arc<Server>>,
    headers: HeaderMap,
) -> HttpResult<Json<Value>> {
    server.auth.authorize(&headers, false)?;
    Ok(Json(mcp_state(&server)?))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct McpToggle {
    #[serde(default)]
    enabled: Option<bool>,
    #[serde(default, rename = "autoStart")]
    auto_start: Option<bool>,
}
async fn mcp_configure(
    State(server): State<Arc<Server>>,
    headers: HeaderMap,
    Json(toggle): Json<McpToggle>,
) -> HttpResult<Json<Value>> {
    server.auth.authorize(&headers, true)?;
    server
        .configure_mcp(toggle.enabled, toggle.auto_start)
        .await
        .map_err(failed)?;
    Ok(Json(mcp_state(&server)?))
}

fn capabilities() -> Value {
    json!({"host":"web", "remoteBrowser":true, "nativeDialogs":false, "browserCookies":false, "uploads":true})
}

async fn session(State(server): State<Arc<Server>>, headers: HeaderMap) -> HttpResult<Json<Value>> {
    let session = server.auth.authorize(&headers, false)?;
    Ok(Json(
        json!({"authenticated":true, "csrfToken":session.csrf, "capabilities":capabilities()}),
    ))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Login {
    account: String,
    password: String,
}

async fn login(
    State(server): State<Arc<Server>>,
    connection: Option<Extension<ConnectInfo<SocketAddr>>>,
    headers: HeaderMap,
    Json(input): Json<Login>,
) -> HttpResult<Response> {
    if !server.auth.same_origin(&headers) {
        return Err(StatusCode::FORBIDDEN.into());
    }
    if input.account.len() > 128 || input.password.len() > 4096 {
        return Err(StatusCode::BAD_REQUEST.into());
    }
    let ip = connection
        .map(|v| v.0.0.ip())
        .unwrap_or(IpAddr::V4(Ipv4Addr::LOCALHOST));
    let permit = server
        .auth
        .password_slots
        .clone()
        .acquire_owned()
        .await
        .map_err(|_| ApiError::from(StatusCode::SERVICE_UNAVAILABLE))?;
    if !server.app.access.check_attempt(ip) {
        return Err(ApiError(
            StatusCode::TOO_MANY_REQUESTS,
            "too many failed login attempts; try again later".into(),
        ));
    }
    let verifier = server.app.clone();
    let generation = server.app.access.generation();
    let valid = tokio::task::spawn_blocking(move || {
        let _permit = permit;
        let valid = verifier.access.verify(&input.account, &input.password);
        if verifier.access.generation() == generation {
            verifier.access.record_attempt(ip, valid);
        }
        valid
    })
    .await
    .map_err(|_| ApiError::from(StatusCode::INTERNAL_SERVER_ERROR))?;
    if server.app.access.generation() != generation {
        return Err(StatusCode::UNAUTHORIZED.into());
    }
    if !valid {
        return Err(ApiError(
            StatusCode::UNAUTHORIZED,
            "invalid account or password".into(),
        ));
    }
    let (token, session) = server.auth.create_session(generation);
    let mut response = Json(
        json!({"authenticated":true, "csrfToken":session.csrf, "capabilities":capabilities()}),
    )
    .into_response();
    response.headers_mut().insert(
        header::SET_COOKIE,
        HeaderValue::from_str(&server.auth.session_cookie(&token)).unwrap(),
    );
    Ok(response)
}

async fn logout(State(server): State<Arc<Server>>, headers: HeaderMap) -> HttpResult<Response> {
    server.auth.authorize(&headers, true)?;
    server.auth.logout(&headers);
    server.browser.stop().await;
    let mut response = Json(json!({"ok":true})).into_response();
    response.headers_mut().insert(
        header::SET_COOKIE,
        HeaderValue::from_static("huifa_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"),
    );
    Ok(response)
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Command {
    command: String,
    #[serde(default)]
    args: Value,
}

fn allowed_command(command: &str) -> bool {
    matches!(
        command,
        "snapshot"
            | "access.status"
            | "access.change_password"
            | "app.release_status"
            | "tasks.add"
            | "tasks.info"
            | "tasks.get"
            | "tasks.logs"
            | "tasks.pause"
            | "tasks.resume"
            | "tasks.cancel"
            | "tasks.retry"
            | "tasks.retryCurrent"
            | "tasks.prioritize"
            | "tasks.finishLive"
            | "tasks.destination"
            | "tasks.delete"
            | "tasks.deleteBatch"
            | "tasks.pauseAll"
            | "tasks.resumeAll"
            | "tasks.convert"
            | "tasks.convertPreview"
            | "probe.video"
            | "probe.collection"
            | "probe.cancel"
            | "storage.status"
            | "storage.cleanup"
            | "settings.save"
            | "settings.preferences"
            | "tools.check"
            | "tools.latest"
            | "cookies.import"
            | "cookies.list"
            | "cookies.status"
            | "ai.models"
            | "cookies.delete"
            | "cover.preview"
            | "cover.export"
            | "cover.generate"
            | "cover.cancel"
            | "media.list"
    )
}

fn validate_host_args(args: &Value) -> HttpResult<()> {
    for options in [&args["options"], &args["settings"]["downloadOptions"]] {
        for key in ["processingTempDir", "processing_temp_dir"] {
            if options
                .get(key)
                .is_some_and(|value| value.as_str() != Some(""))
            {
                return Err(ApiError(
                    StatusCode::BAD_REQUEST,
                    "temporary paths are managed by the web host".into(),
                ));
            }
        }
    }
    Ok(())
}

async fn command(
    State(server): State<Arc<Server>>,
    headers: HeaderMap,
    Json(input): Json<Command>,
) -> HttpResult<Json<Value>> {
    server.auth.authorize(&headers, true)?;
    if !allowed_command(&input.command) {
        return Err(ApiError(
            StatusCode::FORBIDDEN,
            "command is unavailable in the web host".into(),
        ));
    }
    validate_host_args(&input.args)?;
    if serde_json::to_vec(&input.args)
        .map_err(|e| failed(e.into()))?
        .len()
        > 2 * 1024 * 1024
    {
        return Err(StatusCode::PAYLOAD_TOO_LARGE.into());
    }
    let mut value = server
        .app
        .command(&input.command, input.args)
        .await
        .map_err(failed)?;
    if input.command == "snapshot" {
        strip_processing_folder(&mut value["settings"]);
    }
    if matches!(
        input.command.as_str(),
        "settings.save" | "settings.preferences"
    ) {
        strip_processing_folder(&mut value);
    }
    if input.command == "access.change_password" {
        server
            .configure_mcp(
                Some(server.app.mcp_settings().map_err(failed)?["enabled"] == true),
                None,
            )
            .await
            .map_err(failed)?;
    }
    Ok(Json(json!({"value":value})))
}

fn strip_processing_folder(settings: &mut Value) {
    if let Some(options) = settings
        .get_mut("downloadOptions")
        .and_then(Value::as_object_mut)
    {
        options.remove("processingTempDir");
        options.remove("processing_temp_dir");
    }
}

async fn events(
    State(server): State<Arc<Server>>,
    headers: HeaderMap,
) -> HttpResult<Sse<impl Stream<Item = std::result::Result<Event, Infallible>>>> {
    server.auth.authorize(&headers, false)?;
    let receiver = server.app.subscribe();
    let shutdown = server.shutdown.clone();
    let auth = server.auth.clone();
    let stream = futures_util::stream::unfold(receiver, move |mut receiver| {
        let shutdown = shutdown.clone();
        let auth = auth.clone();
        let headers = headers.clone();
        async move {
            loop {
                let item = tokio::select! {
                    _ = shutdown.cancelled() => return None,
                    item = receiver.recv() => item,
                    _ = tokio::time::sleep(Duration::from_secs(30)) => {
                        auth.session(&headers)?;
                        continue;
                    }
                };
                match item {
                    Ok(mut value) => {
                        if value.kind == "settings.updated" {
                            strip_processing_folder(&mut value.data);
                        }
                        auth.session(&headers)?;
                        return Some((
                            Ok(Event::default()
                                .json_data(value)
                                .unwrap_or_else(|_| Event::default().data("{}"))),
                            receiver,
                        ));
                    }
                    Err(tokio::sync::broadcast::error::RecvError::Lagged(_)) => {
                        return Some((
                            Ok(Event::default()
                                .data("{\"kind\":\"snapshot.required\",\"data\":null}")),
                            receiver,
                        ));
                    }
                    Err(_) => return None,
                }
            }
        }
    });
    Ok(Sse::new(stream).keep_alive(KeepAlive::new().interval(Duration::from_secs(15))))
}

#[derive(Deserialize)]
struct FileQuery {
    kind: Option<String>,
}
async fn file(
    State(server): State<Arc<Server>>,
    headers: HeaderMap,
    Path(id): Path<String>,
    Query(query): Query<FileQuery>,
) -> HttpResult<Response> {
    server.auth.authorize(&headers, false)?;
    uuid::Uuid::parse_str(&id).map_err(|_| ApiError::from(StatusCode::NOT_FOUND))?;
    let kind = match query.kind.as_deref().unwrap_or("video") {
        "video" | "media" => "media",
        "original" | "originalCover" => "originalCover",
        "cover" => "cover",
        "thumbnail" => "thumbnail",
        "taskThumbnail" => "taskThumbnail",
        "description" => "description",
        "info" => "info",
        "subtitle" => "subtitle",
        "chapter" => "chapter",
        _ => return Err(StatusCode::BAD_REQUEST.into()),
    };
    let value = server
        .app
        .command("media.file", json!({"id":id,"kind":kind}))
        .await
        .map_err(|_| ApiError::from(StatusCode::NOT_FOUND))?;
    let path = PathBuf::from(value["path"].as_str().ok_or(StatusCode::NOT_FOUND)?);
    let roots = if kind == "taskThumbnail" {
        vec![server.tasks.clone()]
    } else {
        vec![server.downloads.clone(), server.assets.clone()]
    };
    let opened =
        files::open_owned(&path, &roots).map_err(|_| ApiError::from(StatusCode::NOT_FOUND))?;
    files::download(
        opened,
        &path,
        value["mimeType"]
            .as_str()
            .unwrap_or("application/octet-stream"),
        &headers,
    )
    .await
    .map_err(Into::into)
}

async fn upload(
    State(server): State<Arc<Server>>,
    headers: HeaderMap,
    mut multipart: Multipart,
) -> HttpResult<Json<Value>> {
    server.auth.authorize(&headers, true)?;
    let field = multipart
        .next_field()
        .await
        .map_err(|_| ApiError::from(StatusCode::BAD_REQUEST))?
        .ok_or(StatusCode::BAD_REQUEST)?;
    if field.name() != Some("file") {
        return Err(StatusCode::BAD_REQUEST.into());
    }
    let name = field
        .file_name()
        .unwrap_or("upload")
        .chars()
        .filter(|c| !c.is_control())
        .take(128)
        .collect::<String>();
    let bytes = field
        .bytes()
        .await
        .map_err(|_| ApiError::from(StatusCode::PAYLOAD_TOO_LARGE))?;
    if bytes.len() > 25 * 1024 * 1024 || bytes.is_empty() {
        return Err(StatusCode::PAYLOAD_TOO_LARGE.into());
    }
    if multipart
        .next_field()
        .await
        .map_err(|_| ApiError::from(StatusCode::BAD_REQUEST))?
        .is_some()
    {
        return Err(StatusCode::BAD_REQUEST.into());
    }
    if bytes.starts_with(b"# Netscape HTTP Cookie File") || bytes.starts_with(b"# HTTP Cookie File")
    {
        let text =
            std::str::from_utf8(&bytes).map_err(|_| ApiError::from(StatusCode::BAD_REQUEST))?;
        let value = server
            .app
            .command("cookies.import", json!({"name":name,"text":text}))
            .await
            .map_err(failed)?;
        return Ok(Json(
            json!({"id":value["id"], "name":value["name"], "kind":"cookies"}),
        ));
    }
    let directory = server.assets.clone();
    let id = uuid::Uuid::new_v4().to_string();
    let filename = format!("{id}.png");
    tokio::task::spawn_blocking(move || -> Result<()> {
        let format = image::guess_format(&bytes)?;
        anyhow::ensure!(
            matches!(
                format,
                image::ImageFormat::Png | image::ImageFormat::Jpeg | image::ImageFormat::WebP
            ),
            "only PNG, JPEG and WebP images are accepted"
        );
        let mut reader = image::ImageReader::with_format(std::io::Cursor::new(bytes), format);
        let mut limits = image::Limits::default();
        limits.max_image_width = Some(8192);
        limits.max_image_height = Some(8192);
        limits.max_alloc = Some(128 * 1024 * 1024);
        reader.limits(limits);
        let image = reader.decode()?;
        let mut temporary = tempfile::NamedTempFile::new_in(&directory)?;
        image.write_to(&mut temporary, image::ImageFormat::Png)?;
        temporary.as_file().sync_all()?;
        temporary
            .persist_noclobber(directory.join(filename))
            .map_err(|e| e.error)?;
        Ok(())
    })
    .await
    .map_err(|_| ApiError::from(StatusCode::INTERNAL_SERVER_ERROR))?
    .map_err(failed)?;
    Ok(Json(json!({"id":id,"name":name,"kind":"image"})))
}

async fn browser_status(
    State(server): State<Arc<Server>>,
    headers: HeaderMap,
) -> HttpResult<Json<Value>> {
    server.auth.authorize(&headers, false)?;
    Ok(Json(server.browser.status().await))
}
async fn browser_start(
    State(server): State<Arc<Server>>,
    headers: HeaderMap,
    Json(input): Json<browser::Start>,
) -> HttpResult<Json<Value>> {
    server.auth.authorize(&headers, true)?;
    Ok(Json(server.browser.start(input).await.map_err(failed)?))
}
async fn browser_input(
    State(server): State<Arc<Server>>,
    headers: HeaderMap,
    Json(input): Json<browser::Input>,
) -> HttpResult<Json<Value>> {
    server.auth.authorize(&headers, true)?;
    server.browser.input(input).await.map_err(failed)?;
    Ok(Json(json!({"ok":true})))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Save {
    name: String,
}
async fn browser_save(
    State(server): State<Arc<Server>>,
    headers: HeaderMap,
    Json(input): Json<Save>,
) -> HttpResult<Json<Value>> {
    server.auth.authorize(&headers, true)?;
    Ok(Json(
        server.browser.save(&input.name).await.map_err(failed)?,
    ))
}
async fn browser_stop(
    State(server): State<Arc<Server>>,
    headers: HeaderMap,
) -> HttpResult<Json<Value>> {
    server.auth.authorize(&headers, true)?;
    server.browser.stop().await;
    Ok(Json(json!({"ok":true})))
}
async fn browser_events(
    State(server): State<Arc<Server>>,
    headers: HeaderMap,
) -> HttpResult<Sse<impl Stream<Item = std::result::Result<Event, Infallible>>>> {
    server.auth.authorize(&headers, false)?;
    let receiver = server.browser.subscribe();
    let shutdown = server.shutdown.clone();
    let auth = server.auth.clone();
    let stream = futures_util::stream::unfold(receiver, move |mut receiver| {
        let shutdown = shutdown.clone();
        let auth = auth.clone();
        let headers = headers.clone();
        async move {
            loop {
                let item = tokio::select! {
                    _ = shutdown.cancelled() => return None,
                    item = receiver.recv() => item,
                    _ = tokio::time::sleep(Duration::from_secs(30)) => { auth.session(&headers)?; continue; }
                };
                match item {
                    Ok(value) => {
                        auth.session(&headers)?;
                        return Some((
                            Ok(Event::default()
                                .json_data(value)
                                .unwrap_or_else(|_| Event::default().data("{}"))),
                            receiver,
                        ));
                    }
                    Err(tokio::sync::broadcast::error::RecvError::Lagged(_)) => continue,
                    Err(_) => return None,
                }
            }
        }
    });
    Ok(Sse::new(stream).keep_alive(KeepAlive::new().interval(Duration::from_secs(15))))
}

async fn api_missing() -> ApiError {
    StatusCode::NOT_FOUND.into()
}
async fn security_headers(request: axum::extract::Request, next: Next) -> Response {
    let api = request.uri().path().starts_with("/api/");
    let mut response = next.run(request).await;
    let headers = response.headers_mut();
    if api {
        headers.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    }
    headers.insert(
        "x-content-type-options",
        HeaderValue::from_static("nosniff"),
    );
    headers.insert("referrer-policy", HeaderValue::from_static("no-referrer"));
    headers.insert("content-security-policy", HeaderValue::from_static("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'"));
    response
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::Body;
    use axum::http::Request;
    use http_body_util::BodyExt;
    use tower::ServiceExt;

    #[test]
    fn remote_version_checks_are_read_only_in_the_web_host() {
        assert!(allowed_command("tools.latest"));
        assert!(!allowed_command("tools.update"));
    }

    #[test]
    fn cookie_plaintext_is_not_exposed_by_web_commands() {
        assert!(allowed_command("cookies.list"));
        assert!(!allowed_command("cookies.read"));
    }

    #[test]
    fn media_info_only_uses_the_id_scoped_core_command() {
        assert!(allowed_command("tasks.info"));
        assert!(allowed_command("tasks.get"));
        assert!(allowed_command("tasks.logs"));
        assert!(!allowed_command("media.file"));
        assert!(!allowed_command("host.paths"));
        assert!(!allowed_command("host.download_directory"));
    }

    #[test]
    fn batch_deletion_uses_the_same_authenticated_web_command_route() {
        assert!(allowed_command("tasks.delete"));
        assert!(allowed_command("tasks.deleteBatch"));
    }

    #[test]
    fn errors_keep_the_cause_and_remove_signed_urls() {
        let reason = anyhow::anyhow!("HTTP 403 https://example.test/file?token=secret")
            .context("Failed to parse video with yt-dlp");
        let message = failed(reason).1;
        assert!(message.contains("Failed to parse video with yt-dlp: HTTP 403"));
        assert!(!message.contains("secret"));
    }

    #[tokio::test]
    async fn docker_mcp_page_is_separate_from_ai_transport() -> Result<()> {
        let root = tempfile::tempdir()?;
        let frontend = root.path().join("frontend");
        std::fs::create_dir(&frontend)?;
        std::fs::write(
            frontend.join("index.html"),
            "<!doctype html><title>MCP management page</title>",
        )?;
        let server = Server::open(Config {
            account: "admin".into(),
            password: "test-password".into(),
            public_origin: None,
            data_dir: root.path().join("data"),
            download_dir: root.path().join("downloads"),
            tool_dir: None,
            frontend_dir: frontend,
            chromium: "missing-chromium".into(),
        })
        .await?;
        let router = server.router();
        let page = router
            .clone()
            .oneshot(
                Request::builder()
                    .uri("/mcp")
                    .header("accept", "text/html")
                    .body(Body::empty())?,
            )
            .await?;
        assert_eq!(page.status(), StatusCode::OK);
        assert!(
            String::from_utf8(page.into_body().collect().await?.to_bytes().to_vec())?
                .contains("MCP management page")
        );
        let response = router
            .clone()
            .oneshot(Request::builder().uri("/api/mcp").body(Body::empty())?)
            .await?;
        assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
        let request = || {
            Request::builder().method("POST").uri("/mcp-server").header("host","192.168.5.23:11510").header("authorization","Bearer test-password").header("content-type","application/json").header("accept","application/json, text/event-stream").body(Body::from(r#"{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"docker-test","version":"1"}}}"#))
        };
        assert_eq!(
            router.clone().oneshot(request()?).await?.status(),
            StatusCode::SERVICE_UNAVAILABLE
        );
        server.configure_mcp(Some(true), None).await?;
        let mut missing = request()?;
        missing.headers_mut().remove("authorization");
        assert_eq!(
            router.clone().oneshot(missing).await?.status(),
            StatusCode::UNAUTHORIZED
        );
        let response = router.clone().oneshot(request()?).await?;
        assert_eq!(response.status(), StatusCode::OK);
        let old_session = response.headers()["mcp-session-id"].clone();
        let old_stop = server.mcp_transport.read().await.0.clone();
        let body = String::from_utf8(response.into_body().collect().await?.to_bytes().to_vec())?;
        assert!(body.contains("serverInfo"), "{body}");
        server.configure_mcp(Some(false), None).await?;
        assert!(
            old_stop.is_cancelled(),
            "Disabling must stop existing sessions and queries"
        );
        assert_eq!(
            router.clone().oneshot(request()?).await?.status(),
            StatusCode::SERVICE_UNAVAILABLE
        );
        server.configure_mcp(Some(true), None).await?;
        let mut old_request = request()?;
        old_request
            .headers_mut()
            .insert("mcp-session-id", old_session);
        assert_eq!(
            router.clone().oneshot(old_request).await?.status(),
            StatusCode::NOT_FOUND
        );
        assert_eq!(
            router.clone().oneshot(request()?).await?.status(),
            StatusCode::OK
        );
        let rejected = router
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/mcp-server")
                    .header("host", "evil.example")
                    .body(Body::empty())?,
            )
            .await?;
        assert_eq!(rejected.status(), StatusCode::FORBIDDEN);
        let rejected = router
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/mcp-server")
                    .header("host", "192.168.5.23:11510")
                    .header("origin", "https://evil.example")
                    .header("authorization", "Bearer test-password")
                    .body(Body::empty())?,
            )
            .await?;
        assert_eq!(rejected.status(), StatusCode::FORBIDDEN);
        let state = mcp_state(&server).map_err(|error| anyhow::anyhow!(error.1))?;
        assert_eq!(state["connection"]["url"], "/mcp-server");
        assert!(state["connection"].get("token").is_none());
        server.shutdown().await;
        Ok(())
    }

    #[tokio::test]
    async fn web_and_mcp_share_only_failed_password_attempts_from_the_actual_peer() -> Result<()> {
        let root = tempfile::tempdir()?;
        let server = Server::open(Config {
            account: "admin".into(),
            password: "test-password".into(),
            public_origin: None,
            data_dir: root.path().join("data"),
            download_dir: root.path().join("downloads"),
            tool_dir: None,
            frontend_dir: root.path().join("frontend"),
            chromium: "missing-chromium".into(),
        })
        .await?;
        server.configure_mcp(Some(true), None).await?;
        let router = server.router();
        let peer: SocketAddr = "192.0.2.1:12345".parse()?;
        let login = |password: &str, peer: SocketAddr| {
            Request::builder()
                .method("POST")
                .uri("/api/login")
                .extension(ConnectInfo(peer))
                .header("host", "localhost:19491")
                .header("origin", "http://localhost:19491")
                .header("x-forwarded-for", "198.51.100.1")
                .header("content-type", "application/json")
                .body(Body::from(
                    json!({"account":"admin","password":password}).to_string(),
                ))
        };
        let mcp = |password: &str| {
            Request::builder()
                .method("POST")
                .uri("/mcp-server")
                .extension(ConnectInfo(peer))
                .header("host", "localhost:19491")
                .header("authorization", format!("Bearer {password}"))
                .header("content-type", "application/json")
                .header("accept", "application/json, text/event-stream")
                .body(Body::from(r#"{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"limit-test","version":"1"}}}"#))
        };
        for _ in 0..6 {
            assert_eq!(
                router
                    .clone()
                    .oneshot(login("test-password", peer)?)
                    .await?
                    .status(),
                StatusCode::OK
            );
        }
        for _ in 0..4 {
            assert_eq!(
                router
                    .clone()
                    .oneshot(login("wrong", peer)?)
                    .await?
                    .status(),
                StatusCode::UNAUTHORIZED
            );
        }
        assert_eq!(
            router
                .clone()
                .oneshot(mcp("test-password")?)
                .await?
                .status(),
            StatusCode::OK
        );
        for _ in 0..4 {
            assert_eq!(
                router.clone().oneshot(mcp("wrong")?).await?.status(),
                StatusCode::UNAUTHORIZED
            );
        }
        assert_eq!(
            router
                .clone()
                .oneshot(login("test-password", peer)?)
                .await?
                .status(),
            StatusCode::OK
        );
        for _ in 0..2 {
            assert_eq!(
                router
                    .clone()
                    .oneshot(login("wrong", peer)?)
                    .await?
                    .status(),
                StatusCode::UNAUTHORIZED
            );
        }
        for _ in 0..3 {
            assert_eq!(
                router.clone().oneshot(mcp("wrong")?).await?.status(),
                StatusCode::UNAUTHORIZED
            );
        }
        assert_eq!(
            router
                .clone()
                .oneshot(login("test-password", "192.0.2.1:54321".parse()?)?)
                .await?
                .status(),
            StatusCode::TOO_MANY_REQUESTS
        );
        assert_eq!(
            router
                .clone()
                .oneshot(mcp("test-password")?)
                .await?
                .status(),
            StatusCode::TOO_MANY_REQUESTS
        );
        assert_eq!(
            router
                .clone()
                .oneshot(login("test-password", "192.0.2.2:12345".parse()?)?)
                .await?
                .status(),
            StatusCode::OK
        );
        server
            .app
            .access
            .change_password("test-password", "changed-password")?;
        assert_eq!(
            router
                .clone()
                .oneshot(login("changed-password", peer)?)
                .await?
                .status(),
            StatusCode::OK
        );
        for _ in 0..5 {
            let request = login("wrong", peer)?;
            let service = router.clone();
            let pending = tokio::spawn(async move { service.oneshot(request).await });
            tokio::time::timeout(Duration::from_secs(5), async {
                while server.auth.password_slots.available_permits() == 2 {
                    tokio::task::yield_now().await;
                }
            })
            .await?;
            pending.abort();
            let _ = pending.await;
            let finished = tokio::time::timeout(
                Duration::from_secs(5),
                server.auth.password_slots.clone().acquire_many_owned(2),
            )
            .await??;
            drop(finished);
        }
        assert!(
            !server.app.access.check_attempt(peer.ip()),
            "Disconnecting during password verification must still count the failure"
        );
        server.shutdown().await;
        Ok(())
    }

    #[tokio::test]
    async fn web_ui_preferences_preserve_settings_drafts_and_hide_internal_paths() -> Result<()> {
        let root = tempfile::tempdir()?;
        let server = Server::open(Config {
            account: "admin".into(),
            password: "test-password".into(),
            public_origin: None,
            data_dir: root.path().join("data"),
            download_dir: root.path().join("downloads"),
            tool_dir: None,
            frontend_dir: root.path().join("frontend"),
            chromium: "missing-chromium".into(),
        })
        .await?;
        let private = root.path().join("private-processing");
        server
            .app
            .command(
                "settings.save",
                json!({"settings":{
                    "uiLanguage":"zh-CN","aiApiKey":"fixture-api-key",
                    "downloadOptions":{"quality":"1080p","processingTempDir":private,
                    "proxy":"http://fixture-user:fixture-password@proxy.example.test:8080"}
                }}),
            )
            .await?;
        let router = server.router();
        let (token, session) = server.auth.create_session(server.app.access.generation());
        let request = |command: &str, args: Value, csrf: &str| {
            Request::builder()
                .method("POST")
                .uri("/api/command")
                .header("host", "localhost:19491")
                .header("origin", "http://localhost:19491")
                .header("cookie", format!("huifa_session={token}"))
                .header("x-csrf-token", csrf)
                .header("content-type", "application/json")
                .body(Body::from(
                    json!({"command":command,"args":args}).to_string(),
                ))
        };
        let args = json!({"preferences":{"deleteMediaOnTaskDelete":true}});
        let forbidden = router
            .clone()
            .oneshot(request("settings.preferences", args.clone(), "incorrect")?)
            .await?;
        assert_eq!(forbidden.status(), StatusCode::FORBIDDEN);
        let response = router
            .clone()
            .oneshot(request("settings.preferences", args, &session.csrf)?)
            .await?;
        assert_eq!(response.status(), StatusCode::OK);
        let body: Value =
            serde_json::from_slice(&response.into_body().collect().await?.to_bytes())?;
        assert_eq!(
            body["value"]["uiPreferences"]["deleteMediaOnTaskDelete"],
            true
        );
        assert_eq!(body["value"]["downloadOptions"]["quality"], "1080p");
        assert!(
            body["value"]["downloadOptions"]
                .get("processingTempDir")
                .is_none()
        );
        assert!(!body.to_string().contains("fixture-password"));
        assert!(!body.to_string().contains("fixture-api-key"));
        let snapshot = server.app.command("snapshot", json!({})).await?;
        assert_eq!(
            snapshot["settings"]["downloadOptions"]["processingTempDir"],
            json!(private)
        );
        let response = router
            .clone()
            .oneshot(request(
                "settings.save",
                json!({"settings":{
                    "uiLanguage":"en-US","uiPreferences":{"deleteMediaOnTaskDelete":false},
                    "downloadOptions":{"quality":"720p"}
                }}),
                &session.csrf,
            )?)
            .await?;
        assert_eq!(response.status(), StatusCode::OK);
        let body: Value =
            serde_json::from_slice(&response.into_body().collect().await?.to_bytes())?;
        assert_eq!(
            body["value"]["uiPreferences"]["deleteMediaOnTaskDelete"],
            true
        );
        assert_eq!(body["value"]["uiLanguage"], "en-US");
        assert_eq!(body["value"]["hasProxy"], true);
        assert_eq!(body["value"]["hasAiApiKey"], true);
        server.shutdown().await;
        Ok(())
    }

    #[tokio::test]
    async fn web_mcp_preferences_do_not_require_or_change_enabled() -> Result<()> {
        let root = tempfile::tempdir()?;
        let server = Server::open(Config {
            account: "admin".into(),
            password: "test-password".into(),
            public_origin: None,
            data_dir: root.path().join("data"),
            download_dir: root.path().join("downloads"),
            tool_dir: None,
            frontend_dir: root.path().join("frontend"),
            chromium: "missing-chromium".into(),
        })
        .await?;
        let router = server.router();
        let (token, session) = server.auth.create_session(server.app.access.generation());
        for (body, enabled, automatic) in [
            (json!({"autoStart":true}), false, true),
            (json!({"enabled":true}), true, true),
            (json!({"autoStart":false}), true, false),
            (json!({"enabled":false}), false, false),
        ] {
            let stop = server.mcp_transport.read().await.0.clone();
            let preference_only = body.get("enabled").is_none();
            let response = router
                .clone()
                .oneshot(
                    Request::builder()
                        .method("POST")
                        .uri("/api/mcp")
                        .header("host", "localhost:19491")
                        .header("origin", "http://localhost:19491")
                        .header("cookie", format!("huifa_session={token}"))
                        .header("x-csrf-token", &session.csrf)
                        .header("content-type", "application/json")
                        .body(Body::from(body.to_string()))?,
                )
                .await?;
            assert_eq!(response.status(), StatusCode::OK);
            let value: Value =
                serde_json::from_slice(&response.into_body().collect().await?.to_bytes())?;
            assert_eq!(value["enabled"], enabled);
            assert_eq!(value["autoStart"], automatic);
            assert_eq!(stop.is_cancelled(), !preference_only);
        }
        server.shutdown().await;
        Ok(())
    }

    #[tokio::test]
    async fn shared_password_change_revokes_sessions_and_persists() -> Result<()> {
        let root = tempfile::tempdir()?;
        let config = || Config {
            account: "admin".into(),
            password: "admin".into(),
            public_origin: None,
            data_dir: root.path().join("data"),
            download_dir: root.path().join("downloads"),
            tool_dir: None,
            frontend_dir: root.path().join("frontend"),
            chromium: "missing-chromium".into(),
        };
        let server = Server::open(config()).await?;
        let router = server.router();
        let response = router
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/login")
                    .header("host", "localhost:19491")
                    .header("origin", "http://localhost:19491")
                    .header("content-type", "application/json")
                    .body(Body::from(r#"{"account":"admin","password":"admin"}"#))?,
            )
            .await?;
        assert_eq!(response.status(), StatusCode::OK);
        let cookie = response.headers()[header::SET_COOKIE]
            .to_str()?
            .split(';')
            .next()
            .unwrap()
            .to_string();
        let body: Value =
            serde_json::from_slice(&response.into_body().collect().await?.to_bytes())?;
        let csrf = body["csrfToken"].as_str().unwrap();
        server.configure_mcp(Some(true), Some(true)).await?;
        let initialize = |password: &str| {
            Request::builder().method("POST").uri("/mcp-server").header("host", "localhost:19491").header("authorization", format!("Bearer {password}")).header("content-type", "application/json").header("accept", "application/json, text/event-stream").body(Body::from(r#"{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"password-test","version":"1"}}}"#))
        };
        let response = router.clone().oneshot(initialize("admin")?).await?;
        assert_eq!(response.status(), StatusCode::OK);
        let old_session = response.headers()["mcp-session-id"].clone();
        let old_stop = server.mcp_transport.read().await.0.clone();
        let response = router.clone().oneshot(Request::builder().method("POST").uri("/api/command").header("host", "localhost:19491").header("origin", "http://localhost:19491").header("cookie", &cookie).header("x-csrf-token", csrf).header("content-type", "application/json").body(Body::from(r#"{"command":"access.change_password","args":{"currentPassword":"admin","newPassword":"changed-password"}}"#))?).await?;
        assert_eq!(response.status(), StatusCode::OK);
        assert!(old_stop.is_cancelled());
        assert_eq!(
            router
                .clone()
                .oneshot(
                    Request::builder()
                        .uri("/api/session")
                        .header("cookie", &cookie)
                        .body(Body::empty())?
                )
                .await?
                .status(),
            StatusCode::UNAUTHORIZED
        );
        assert_eq!(
            router.clone().oneshot(initialize("admin")?).await?.status(),
            StatusCode::UNAUTHORIZED
        );
        assert_eq!(
            router
                .clone()
                .oneshot(initialize("changed-password")?)
                .await?
                .status(),
            StatusCode::OK
        );
        let mut old = initialize("changed-password")?;
        old.headers_mut().insert("mcp-session-id", old_session);
        assert_eq!(
            router.clone().oneshot(old).await?.status(),
            StatusCode::NOT_FOUND
        );
        server.shutdown().await;
        drop(router);
        drop(server);
        let mut changed_environment = config();
        changed_environment.account = "environment-account".into();
        changed_environment.password = "environment-password".into();
        let reopened = Server::open(changed_environment).await?;
        assert!(reopened.app.access.verify("admin", "changed-password"));
        assert!(
            !reopened
                .app
                .access
                .verify("environment-account", "environment-password")
        );
        assert_eq!(
            mcp_state(&reopened).map_err(|error| anyhow::anyhow!(error.1))?["enabled"],
            true
        );
        reopened.shutdown().await;
        Ok(())
    }

    #[tokio::test]
    async fn protected_real_core_command_flow() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let server = Server::open(Config {
            account: "admin".into(),
            password: "test-password".into(),
            public_origin: None,
            data_dir: dir.path().join("data"),
            download_dir: dir.path().join("downloads"),
            tool_dir: None,
            frontend_dir: dir.path().join("frontend"),
            chromium: "missing-chromium".into(),
        })
        .await?;
        let router = server.router();
        let response = router
            .clone()
            .oneshot(Request::builder().uri("/api/session").body(Body::empty())?)
            .await?;
        assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
        let response = router
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/command")
                    .header("host", "localhost:19491")
                    .header("origin", "http://localhost:19491")
                    .header("content-type", "application/json")
                    .body(Body::from(
                        r#"{"command":"tasks.logs","args":{"id":"missing"}}"#,
                    ))?,
            )
            .await?;
        assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
        let response = router
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/login")
                    .header("host", "localhost:19491")
                    .header("origin", "http://localhost:19491")
                    .header("content-type", "application/json")
                    .body(Body::from(
                        r#"{"account":"admin","password":"test-password"}"#,
                    ))?,
            )
            .await?;
        assert_eq!(response.status(), StatusCode::OK);
        let cookie = response.headers()[header::SET_COOKIE]
            .to_str()?
            .split(';')
            .next()
            .unwrap()
            .to_owned();
        assert!(
            response.headers()[header::SET_COOKIE]
                .to_str()?
                .contains("HttpOnly; SameSite=Strict")
        );
        let body: Value =
            serde_json::from_slice(&response.into_body().collect().await?.to_bytes())?;
        let csrf = body["csrfToken"].as_str().unwrap();
        for (command, expected) in [
            ("snapshot", StatusCode::OK),
            ("tasks.logs", StatusCode::BAD_REQUEST),
            ("media.file", StatusCode::FORBIDDEN),
            ("sql.execute", StatusCode::FORBIDDEN),
        ] {
            let response = router
                .clone()
                .oneshot(
                    Request::builder()
                        .method("POST")
                        .uri("/api/command")
                        .header("host", "localhost:19491")
                        .header("origin", "http://localhost:19491")
                        .header("cookie", &cookie)
                        .header("x-csrf-token", csrf)
                        .header("content-type", "application/json")
                        .body(Body::from(json!({"command":command,"args":{}}).to_string()))?,
                )
                .await?;
            assert_eq!(response.status(), expected);
            if command == "snapshot" {
                let body: Value =
                    serde_json::from_slice(&response.into_body().collect().await?.to_bytes())?;
                assert!(body["value"]["tasks"].is_array());
                assert!(!body.to_string().contains("test-password"));
            } else if command == "tasks.logs" {
                let body: Value =
                    serde_json::from_slice(&response.into_body().collect().await?.to_bytes())?;
                assert!(body.to_string().contains("An ID is required"));
            }
        }
        let response = router
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/command")
                    .header("host", "localhost:19491")
                    .header("origin", "http://evil.test")
                    .header("cookie", &cookie)
                    .header("x-csrf-token", csrf)
                    .header("content-type", "application/json")
                    .body(Body::from(r#"{"command":"snapshot","args":{}}"#))?,
            )
            .await?;
        assert_eq!(response.status(), StatusCode::FORBIDDEN);

        let mut png = std::io::Cursor::new(Vec::new());
        image::DynamicImage::new_rgba8(2, 2).write_to(&mut png, image::ImageFormat::Png)?;
        for (payload, expected) in [
            (png.into_inner(), StatusCode::OK),
            (b"#!/bin/sh\necho unsafe".to_vec(), StatusCode::BAD_REQUEST),
        ] {
            let mut body = b"--boundary\r\nContent-Disposition: form-data; name=\"file\"; filename=\"../../escape.exe\"\r\nContent-Type: application/octet-stream\r\n\r\n".to_vec();
            body.extend_from_slice(&payload);
            body.extend_from_slice(b"\r\n--boundary--\r\n");
            let response = router
                .clone()
                .oneshot(
                    Request::builder()
                        .method("POST")
                        .uri("/api/upload")
                        .header("host", "localhost:19491")
                        .header("origin", "http://localhost:19491")
                        .header("cookie", &cookie)
                        .header("x-csrf-token", csrf)
                        .header("content-type", "multipart/form-data; boundary=boundary")
                        .body(Body::from(body))?,
                )
                .await?;
            assert_eq!(response.status(), expected);
            if expected == StatusCode::OK {
                let value: Value =
                    serde_json::from_slice(&response.into_body().collect().await?.to_bytes())?;
                let id = value["id"].as_str().unwrap();
                assert!(uuid::Uuid::parse_str(id).is_ok());
                assert!(server.assets.join(format!("{id}.png")).is_file());
                assert!(!dir.path().join("escape.exe").exists());
            }
        }
        assert!(validate_host_args(&json!({"options":{"processingTempDir":"/etc"}})).is_err());
        assert!(
            validate_host_args(
                &json!({"settings":{"downloadOptions":{"processing_temp_dir":"/etc"}}})
            )
            .is_err()
        );
        assert!(validate_host_args(&json!({"options":{"processingTempDir":""}})).is_ok());
        let mut settings =
            json!({"downloadOptions":{"processingTempDir":"C:/private-folder","quality":"best"}});
        strip_processing_folder(&mut settings);
        assert!(
            settings["downloadOptions"]
                .get("processingTempDir")
                .is_none()
        );
        assert_eq!(settings["downloadOptions"]["quality"], "best");
        server.shutdown().await;
        Ok(())
    }
}

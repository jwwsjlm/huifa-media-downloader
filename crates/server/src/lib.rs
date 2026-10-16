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
    routing::{get, post},
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
    auth: Arc<auth::Auth>,
    browser: Arc<browser::Browser>,
    assets: PathBuf,
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
        // Validate authentication before opening storage or starting any work.
        let auth = Arc::new(auth::Auth::new(
            &config.account,
            &config.password,
            config.public_origin,
        )?);
        let app = App::open(AppConfig {
            data_dir: config.data_dir.clone(),
            download_dir: config.download_dir.clone(),
            tool_dir: config.tool_dir,
        })
        .await?;
        let assets = config.data_dir.join("assets");
        std::fs::create_dir_all(&assets)?;
        anyhow::ensure!(
            !std::fs::symlink_metadata(&assets)?.file_type().is_symlink(),
            "asset storage must not be a link"
        );
        let assets = assets.canonicalize()?;
        let downloads = config.download_dir.canonicalize()?;
        let browser = browser::Browser::new(
            app.clone(),
            config.chromium,
            config.data_dir.join("browser"),
        )?;
        Ok(Arc::new(Self {
            app,
            auth,
            browser,
            assets,
            downloads,
            frontend: config.frontend_dir,
            shutdown: CancellationToken::new(),
        }))
    }

    pub fn router(self: &Arc<Self>) -> Router {
        let static_files = ServeDir::new(&self.frontend)
            .not_found_service(ServeFile::new(self.frontend.join("index.html")));
        Router::new()
            .route("/api/session", get(session))
            .route(
                "/api/login",
                post(login).layer(DefaultBodyLimit::max(32 * 1024)),
            )
            .route("/api/logout", post(logout))
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

    pub async fn shutdown(&self) {
        self.shutdown.cancel();
        self.browser.shutdown().await;
        if let Err(error) = self.app.shutdown().await {
            tracing::error!("core shutdown failed: {error}");
        }
    }
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
    if !server.auth.check_attempt(ip) {
        return Err(ApiError(
            StatusCode::TOO_MANY_REQUESTS,
            "too many login attempts; try again later".into(),
        ));
    }
    let permit = server
        .auth
        .password_slots
        .acquire()
        .await
        .map_err(|_| ApiError::from(StatusCode::SERVICE_UNAVAILABLE))?;
    let auth = server.auth.clone();
    let valid = tokio::task::spawn_blocking(move || auth.verify(&input.account, &input.password))
        .await
        .map_err(|_| ApiError::from(StatusCode::INTERNAL_SERVER_ERROR))?;
    drop(permit);
    if !valid {
        return Err(ApiError(
            StatusCode::UNAUTHORIZED,
            "invalid account or password".into(),
        ));
    }
    let (token, session) = server.auth.create_session();
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
            | "tasks.add"
            | "tasks.pause"
            | "tasks.resume"
            | "tasks.cancel"
            | "tasks.retry"
            | "tasks.delete"
            | "tasks.pauseAll"
            | "tasks.resumeAll"
            | "tasks.convert"
            | "probe.video"
            | "probe.collection"
            | "settings.save"
            | "tools.check"
            | "cookies.import"
            | "cookies.list"
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
    if input.command == "settings.save" {
        strip_processing_folder(&mut value);
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
    let opened = files::open_owned(&path, &[server.downloads.clone(), server.assets.clone()])
        .map_err(|_| ApiError::from(StatusCode::NOT_FOUND))?;
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
    fn errors_keep_the_cause_and_remove_signed_urls() {
        let reason = anyhow::anyhow!("HTTP 403 https://example.test/file?token=secret")
            .context("Failed to parse video with yt-dlp");
        let message = failed(reason).1;
        assert!(message.contains("Failed to parse video with yt-dlp: HTTP 403"));
        assert!(!message.contains("secret"));
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

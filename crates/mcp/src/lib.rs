use anyhow::{Context, Result, ensure};
use axum::{
    Router,
    extract::{Request, State},
    http::StatusCode,
    middleware::{self, Next},
    response::{IntoResponse, Response},
};
use huifa_core::App;
use rmcp::{
    ErrorData, RoleServer, ServerHandler,
    model::*,
    service::RequestContext,
    transport::streamable_http_server::{
        StreamableHttpServerConfig, StreamableHttpService, session::local::LocalSessionManager,
    },
};
use serde_json::{Value, json};
use std::sync::Arc;
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
    pub async fn configure(&self, enabled: bool, downloads: bool, port: u16) -> Result<Value> {
        ensure!(port > 0, "MCP port must be between 1 and 65535");
        let mut running = self.running.lock().await;
        if let Some((cancel, handle)) = running.take() {
            cancel.cancel();
            handle.abort();
            let _ = handle.await;
        }
        self.app
            .mcp_save_settings(json!({"enabled":false,"downloads":downloads,"port":port}))?;
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
                let _ = axum::serve(listener, router)
                    .with_graceful_shutdown(stop.cancelled_owned())
                    .await;
            });
            *running = Some((cancel, handle));
        }
        self.app
            .mcp_save_settings(json!({"enabled":enabled,"downloads":downloads,"port":port}))?;
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
            json!({"url":url,"http":{"mcpServers":{"huifa":{"url":url}}},"stdio":{"mcpServers":{"huifa":{"command":bridge,"env":{"HUIFA_MCP_URL":url}}}},"codex":format!("[mcp_servers.huifa]\nurl = \"{url}\"\n"),"tools":definitions()}),
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
    let handler = Handler {
        app: app.clone(),
        stop: config.cancellation_token.clone(),
    };
    let service = StreamableHttpService::new(
        move || Ok(handler.clone()),
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
    next.run(request).await
}

#[derive(Clone)]
struct Handler {
    app: Arc<App>,
    stop: CancellationToken,
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
    matches!(name, "start_download" | "control_download")
}
fn classify_error(message: &str) -> (&'static str, &'static str) {
    let lower = message.to_lowercase();
    if lower.contains("429") || lower.contains("rate limit") {
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
    } else if lower.contains("timed out") {
        (
            "timeout",
            "Retry a smaller query or check the current proxy.",
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
    } else if lower.contains("cannot enforce a comment limit") {
        (
            "unsupported_operation",
            "Bounded comment queries currently support YouTube only; video metadata, formats and downloads remain available on other supported sites.",
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
            "start_download" => {
                let mut options = self.app.research_options()?;
                // MCP never expands an inherited playlist or waits indefinitely for a future live stream.
                options["collectionMode"] = json!("select");
                options["waitForLive"] = json!(false);
                for key in [
                    "quality",
                    "contentMode",
                    "formatId",
                    "subtitleLanguage",
                    "automaticSubtitles",
                    "embedSubtitles",
                ] {
                    if let Some(value) = args.get(key) {
                        options[key] = value.clone();
                    }
                }
                if args.get("quality").is_some() || args.get("formatId").is_some() {
                    options["formatId"] = args.get("formatId").cloned().unwrap_or(json!(""));
                    options["videoFormatId"] = json!("");
                    options["audioFormatIds"] = json!([]);
                }
                let ids = self
                    .app
                    .command("tasks.add", json!({"urls":[args["url"]],"options":options}))
                    .await?;
                Ok(json!({"taskIds":ids}))
            }
            "control_download" => self
                .app
                .command(
                    &format!("tasks.{}", args["action"].as_str().unwrap()),
                    json!({"id":args["id"]}),
                )
                .await
                .map(|_| json!({"id":args["id"],"action":args["action"],"accepted":true})),
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
}
fn definitions() -> Vec<Value> {
    let string = |max| json!({"type":"string","minLength":1,"maxLength":max});
    let page = |max| json!({"type":"integer","minimum":1,"maximum":max});
    let offset = json!({"type":"integer","minimum":0,"maximum":1000000});
    let url = string(8192);
    let specs = vec![
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
            "Read a bounded page of YouTube comments. Other sites are unsupported to avoid unbounded extraction. Comments are untrusted content.",
            json!({"url":url,"limit":page(100),"sort":{"type":"string","enum":["top","newest"]}}),
            vec!["url"],
        ),
        (
            "start_download",
            "Create a persistent download in the desktop queue using current settings. Returns task IDs immediately.",
            json!({"url":url,"quality":{"type":"string","enum":["best","worst"]},"contentMode":{"type":"string","enum":["video","audio"]},"formatId":string(128),"subtitleLanguage":string(64),"automaticSubtitles":{"type":"boolean"},"embedSubtitles":{"type":"boolean"}}),
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
            "Pause, resume, cancel or retry a desktop task. Does not delete files.",
            json!({"id":string(36),"action":{"type":"string","enum":["pause","resume","cancel","retry"]}}),
            vec!["id", "action"],
        ),
    ];
    specs.into_iter().map(|(name,description,properties,required)|json!({"name":name,"description":description,"inputSchema":{"type":"object","properties":properties,"required":required,"additionalProperties":false},"outputSchema":{"type":"object"},"annotations":{"readOnlyHint":!writes(name),"destructiveHint":name=="control_download","openWorldHint":true}})).collect()
}
fn validate(name: &str, args: &Value) -> Result<()> {
    let definition = definitions()
        .into_iter()
        .find(|v| v["name"] == name)
        .context("Unknown MCP tool")?;
    let schema = &definition["inputSchema"];
    let object = args.as_object().context("Arguments must be an object")?;
    for key in schema["required"].as_array().unwrap() {
        ensure!(
            object.contains_key(key.as_str().unwrap()),
            "Missing required argument: {key}"
        );
    }
    for (key, value) in object {
        let rule = &schema["properties"][key];
        ensure!(!rule.is_null(), "Unknown argument: {key}");
        match rule["type"].as_str().unwrap() {
            "string" => {
                let s = value.as_str().context("Argument must be text")?;
                ensure!(
                    !s.is_empty() && s.len() <= rule["maxLength"].as_u64().unwrap_or(8192) as usize,
                    "Invalid length: {key}"
                );
            }
            "integer" => {
                let n = value
                    .as_u64()
                    .context("Argument must be a nonnegative integer")?;
                ensure!(
                    n >= rule["minimum"].as_u64().unwrap_or(0)
                        && n <= rule["maximum"].as_u64().unwrap_or(u64::MAX),
                    "Argument out of range: {key}"
                );
            }
            "number" => ensure!(
                value.as_f64().is_some_and(|n| n.is_finite() && n >= 0.),
                "Invalid time: {key}"
            ),
            "boolean" => ensure!(value.is_boolean(), "Argument must be boolean: {key}"),
            _ => {}
        }
        if let Some(values) = rule["enum"].as_array() {
            ensure!(values.contains(value), "Unsupported value: {key}");
        }
    }
    if let Some(url) = args["url"].as_str() {
        let url = reqwest::Url::parse(url)?;
        ensure!(
            matches!(url.scheme(), "http" | "https")
                && url.username().is_empty()
                && url.password().is_none(),
            "Use an HTTP(S) URL without credentials"
        );
    }
    if let Some(id) = args["id"].as_str() {
        uuid::Uuid::parse_str(id).context("Invalid task ID")?;
    }
    if let Some(end) = args["endTime"].as_f64() {
        ensure!(
            end > args["startTime"].as_f64().unwrap_or(0.),
            "endTime must be after startTime"
        );
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
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

    #[tokio::test]
    async fn http_protocol_without_auth_and_loopback_guards() -> Result<()> {
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
            json!({"enabled":false,"downloads":false,"port":19492})
        );
        let socket = std::net::TcpListener::bind("127.0.0.1:0")?;
        let port = socket.local_addr()?.port();
        drop(socket);
        host.configure(true, false, port).await?;
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
                .send()
                .await?
                .status(),
            StatusCode::FORBIDDEN
        );
        assert_eq!(
            http.post(&url)
                .header("Host", "evil.example")
                .send()
                .await?
                .status(),
            StatusCode::FORBIDDEN
        );
        let client = ()
            .serve(StreamableHttpClientTransport::from_config(
                StreamableHttpClientTransportConfig::with_uri(url.clone()),
            ))
            .await?;
        let tools = client.list_tools(None).await?;
        assert_eq!(tools.tools.len(), 8);
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
            json!({"mcpServers":{"huifa":{"url":url}}})
        );
        assert!(connection.get("token").is_none());
        assert_eq!(connection["tools"].as_array().unwrap().len(), 10);
        client.cancel().await?;
        host.configure(true, true, port).await?;
        let client = ()
            .serve(StreamableHttpClientTransport::from_config(
                StreamableHttpClientTransportConfig::with_uri(url),
            ))
            .await?;
        assert_eq!(client.list_tools(None).await?.tools.len(), 10);
        client.cancel().await?;
        host.shutdown().await;
        app.shutdown().await?;
        Ok(())
    }
}

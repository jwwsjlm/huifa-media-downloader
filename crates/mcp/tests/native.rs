//! Opt-in native/external acceptance tests; never use the user's saved credentials.
use anyhow::{Context, Result, ensure};
use huifa_core::{App, AppConfig};
use huifa_mcp::DesktopMcp;
use rmcp::{
    ServiceExt,
    model::CallToolRequestParams,
    transport::{
        StreamableHttpClientTransport, streamable_http_client::StreamableHttpClientTransportConfig,
    },
};
use serde_json::{Value, json};

#[tokio::test]
#[ignore = "requires installed native tools; set HUIFA_MCP_UPSTREAM=1 to also check public websites"]
async fn native_metadata_download_and_upstream_diagnostics() -> Result<()> {
    let root = tempfile::tempdir()?;
    let tools = std::env::var_os("HUIFA_TEST_TOOLS")
        .map(std::path::PathBuf::from)
        .unwrap_or(std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../runtime-tools"))
        .canonicalize()?;
    let filename = root.path().join("fixture.mp4");
    let ffmpeg = tools.join(if cfg!(windows) {
        "ffmpeg.exe"
    } else {
        "ffmpeg"
    });
    let output = tokio::process::Command::new(ffmpeg)
        .args([
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=160x90:r=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100",
            "-t",
            "1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
        ])
        .arg(&filename)
        .output()
        .await?;
    ensure!(
        output.status.success(),
        "fixture FFmpeg failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let bytes = std::fs::read(&filename)?;
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
    let source = format!("http://{}/fixture.mp4", listener.local_addr()?);
    let fixture_server = tokio::spawn(async move {
        let router = axum::Router::new().route(
            "/fixture.mp4",
            axum::routing::get(move || {
                let bytes = bytes.clone();
                async move { ([("content-type", "video/mp4")], bytes) }
            }),
        );
        let _ = axum::serve(listener, router).await;
    });
    let app = App::open(AppConfig {
        data_dir: root.path().join("data"),
        download_dir: root.path().join("downloads"),
        tool_dir: Some(tools),
    })
    .await?;
    let host = DesktopMcp::new(app.clone());
    let socket = std::net::TcpListener::bind("127.0.0.1:0")?;
    let port = socket.local_addr()?.port();
    drop(socket);
    host.configure(true, true, port, None).await?;
    let client = ()
        .serve(StreamableHttpClientTransport::from_config(
            StreamableHttpClientTransportConfig::with_uri(format!("http://127.0.0.1:{port}/mcp"))
                .auth_header("admin"),
        ))
        .await?;
    let request = |name: &'static str, args: Value| {
        CallToolRequestParams::new(name).with_arguments(args.as_object().unwrap().clone())
    };
    let metadata = client
        .call_tool(request("get_video_info", json!({"url":source})))
        .await?;
    ensure!(metadata.is_error != Some(true), "{metadata:?}");
    let created = client
        .call_tool(request(
            "start_download",
            json!({"url":source,"quality":"best"}),
        ))
        .await?;
    ensure!(created.is_error != Some(true), "{created:?}");
    let id = created
        .structured_content
        .as_ref()
        .context("Missing structured result")?["taskIds"][0]
        .as_str()
        .context("Missing task ID")?;
    let completed = tokio::time::timeout(std::time::Duration::from_secs(60), async {
        loop {
            let result = client
                .call_tool(request("get_download", json!({"id":id})))
                .await?;
            let value = result.structured_content.context("Missing task result")?;
            if matches!(value["status"].as_str(), Some("completed" | "failed")) {
                return Ok::<_, anyhow::Error>(value);
            }
            tokio::time::sleep(std::time::Duration::from_millis(200)).await;
        }
    })
    .await??;
    ensure!(completed["status"] == "completed", "{completed}");
    println!("Native MCP metadata -> queued download -> completed: PASS");
    if std::env::var_os("HUIFA_MCP_UPSTREAM").is_some() {
        for (site, url) in [
            ("YouTube", "https://www.youtube.com/watch?v=jNQXAC9IVRw"),
            ("Bilibili", "https://www.bilibili.com/video/BV1GJ411x7h7"),
        ] {
            let result = client
                .call_tool(request("get_video_info", json!({"url":url})))
                .await?;
            let value = result.structured_content.unwrap_or_default();
            println!(
                "{site} metadata: {}",
                if result.is_error == Some(true) {
                    value.to_string()
                } else {
                    json!({"id":value["id"],"title":value["title"],"duration":value["duration"]})
                        .to_string()
                }
            );
        }
    }
    client.cancel().await?;
    host.shutdown().await;
    app.shutdown().await?;
    fixture_server.abort();
    Ok(())
}

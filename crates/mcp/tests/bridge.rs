use anyhow::Result;
use huifa_core::{App, AppConfig};
use huifa_mcp::DesktopMcp;
use serde_json::{Value, json};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};

#[tokio::test]
async fn real_stdio_bridge_discovers_and_calls_desktop() -> Result<()> {
    let root = tempfile::tempdir()?;
    let app = App::open(AppConfig {
        data_dir: root.path().join("data"),
        download_dir: root.path().join("downloads"),
        tool_dir: None,
    })
    .await?;
    let host = DesktopMcp::new(app.clone());
    let socket = std::net::TcpListener::bind("127.0.0.1:0")?;
    let port = socket.local_addr()?.port();
    drop(socket);
    host.configure(true, false, port, None).await?;
    let mut child = tokio::process::Command::new(env!("CARGO_BIN_EXE_huifa-mcp-bridge"))
        .env("HUIFA_MCP_URL", format!("http://127.0.0.1:{port}/mcp"))
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .kill_on_drop(true)
        .spawn()?;
    let mut input = child.stdin.take().unwrap();
    let mut output = BufReader::new(child.stdout.take().unwrap()).lines();
    for (request, id) in [
        (
            json!({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"integration-test","version":"1"}}}),
            1,
        ),
        (json!({"jsonrpc":"2.0","id":2,"method":"tools/list"}), 2),
        (
            json!({"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"list_downloads","arguments":{}}}),
            3,
        ),
    ] {
        input.write_all(format!("{request}\n").as_bytes()).await?;
        loop {
            let line = tokio::time::timeout(std::time::Duration::from_secs(15), output.next_line())
                .await??
                .expect("bridge stdout closed");
            let response: Value = serde_json::from_str(&line)?;
            if response["id"] != id {
                continue;
            }
            assert!(response.get("error").is_none(), "{response}");
            if id == 2 {
                assert_eq!(response["result"]["tools"].as_array().unwrap().len(), 21);
            }
            if id == 3 {
                assert_eq!(response["result"]["structuredContent"]["tasks"], json!([]));
            }
            break;
        }
        if id == 1 {
            input
                .write_all(b"{\"jsonrpc\":\"2.0\",\"method\":\"notifications/initialized\"}\n")
                .await?;
        }
    }
    drop(input);
    let status = tokio::time::timeout(std::time::Duration::from_secs(10), child.wait()).await??;
    assert!(status.success());
    host.shutdown().await;
    app.shutdown().await?;
    Ok(())
}

#[tokio::test]
async fn cross_site_sources_collection_and_comment_cache_reach_mcp() -> Result<()> {
    use rmcp::{
        ServiceExt,
        model::CallToolRequestParams,
        transport::{
            StreamableHttpClientTransport,
            streamable_http_client::StreamableHttpClientTransportConfig,
        },
    };
    let root = tempfile::tempdir()?;
    let tools = root.path().join("tools");
    std::fs::create_dir_all(&tools)?;
    let fake = root.path().join("fake.rs");
    let source = format!(
        "const ROOT: &str = {:?};\n{}",
        root.path().to_string_lossy(),
        r#"
use std::io::Write;
fn main() {
    let args: Vec<_> = std::env::args().skip(1).collect();
    let mut log = std::fs::OpenOptions::new().create(true).append(true).open(std::path::Path::new(ROOT).join("calls.log")).unwrap();
    writeln!(log,"{:?}",args).unwrap();
    if args.iter().any(|arg| arg == "--version") {println!("2026.10.fixture");return;}
    if args.iter().any(|arg| arg == "--list-extractors") {println!("BiliBili:search\nexample");return;}
    if args.iter().any(|arg| arg == "--extractor-descriptions") {println!("BiliBili:search: Bilibili search; \"bilisearch:\" prefix (e.g. \"bilisearch5:cats\")");return;}
    if !args.iter().any(|arg| arg == "--dump-single-json") {std::process::exit(4);}
    let input = args.last().unwrap();
    let name = if input.starts_with("bilisearch") {"search"} else if input.contains("/collection") {"collection"} else if args.iter().any(|arg|arg == "--write-comments") {"comments"} else {"video"};
    print!("{}",std::fs::read_to_string(std::path::Path::new(ROOT).join(format!("{name}.json"))).unwrap());
}
"#
    );
    std::fs::write(&fake, source)?;
    let executable = tools.join(format!("yt-dlp{}", std::env::consts::EXE_SUFFIX));
    let built = tokio::process::Command::new("rustc")
        .arg("--edition=2024")
        .arg(&fake)
        .arg("-o")
        .arg(&executable)
        .output()
        .await?;
    assert!(
        built.status.success(),
        "fixture compile: {}",
        String::from_utf8_lossy(&built.stderr)
    );
    for name in ["ffmpeg", "ffprobe"] {
        std::fs::copy(
            &executable,
            tools.join(format!("{name}{}", std::env::consts::EXE_SUFFIX)),
        )?;
    }
    let secret_url = "https://example.com/video?id=7&token=fixture-private";
    let video = json!({"id":"7","title":"Fixture","duration":30,"webpage_url":secret_url,"extractor":"example","formats":[{"format_id":"18","url":"https://cdn.invalid/stream.mp4?token=private","ext":"mp4","vcodec":"h264","acodec":"aac","height":720,"width":1280,"protocol":"https"}]});
    std::fs::write(root.path().join("video.json"), video.to_string())?;
    std::fs::write(root.path().join("search.json"),json!({"_type":"playlist","title":"Bilibili results","entries":[{"_type":"url","id":"7","title":"Result","url":secret_url,"duration":30}]}).to_string())?;
    std::fs::write(root.path().join("collection.json"),json!({"_type":"playlist","title":"Collection","entries":[{"_type":"url","id":"good","title":"Good","url":"https://example.com/video?id=8&token=fixture-private","duration":30},{"_type":"url","id":"short","title":"Filtered","url":"https://example.com/short","duration":5},{"id":"bad","title":"Unavailable URL","duration":40},{"_type":"url","id":"lookahead","url":"https://example.com/next","duration":50}]}).to_string())?;
    std::fs::write(root.path().join("comments.json"),json!({"id":"7","comments":[{"id":"a","text":"first"},{"id":"b","text":"second"},{"id":"c","text":"third"}]}).to_string())?;
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
    let call = |name: &'static str, args: Value| {
        client.call_tool(
            CallToolRequestParams::new(name).with_arguments(args.as_object().unwrap().clone()),
        )
    };
    let providers = call("list_search_providers", json!({"limit":1})).await?;
    assert_ne!(providers.is_error, Some(true), "{providers:?}");
    assert_eq!(
        providers.structured_content.unwrap()["providers"][0]["id"],
        "BiliBili:search"
    );
    let search = call(
        "search_videos",
        json!({"provider":"bilibili","query":"fixture","limit":1}),
    )
    .await?;
    assert_ne!(search.is_error, Some(true), "{search:?}");
    let search = search.structured_content.unwrap();
    assert_eq!(search["provider"], "BiliBili:search");
    assert!(!search.to_string().contains("fixture-private"));
    let reference = search["entries"][0]["sourceRef"].as_str().unwrap();
    assert_eq!(
        app.resolve_research_url(&json!({"sourceRef":reference}))?,
        secret_url
    );
    let preview = call(
        "preview_download",
        json!({"sourceRef":reference,"options":{"quality":"720p"}}),
    )
    .await?;
    assert_ne!(preview.is_error, Some(true), "{preview:?}");
    let preview = preview.structured_content.unwrap();
    assert_eq!(preview["createsTask"], false);
    assert_eq!(preview["formats"][0]["format_id"], "18");
    assert_eq!(preview["plan"]["quality"], "720p");
    assert!(!preview.to_string().contains("fixture-private"));
    let created = call("start_download", json!({"sourceRef":reference})).await?;
    assert_ne!(created.is_error, Some(true), "{created:?}");
    let created = created.structured_content.unwrap();
    assert_eq!(created["taskIds"].as_array().unwrap().len(), 1);
    assert_eq!(created["results"][0]["action"], "added");
    app.command("tasks.pause", json!({"id":created["taskIds"][0]}))
        .await?;
    let existing = call("start_download", json!({"sourceRef":reference})).await?;
    let existing = existing.structured_content.unwrap();
    assert_eq!(existing["taskIds"], created["taskIds"]);
    assert_eq!(existing["results"][0]["action"], "existing");
    let collection=call("start_collection_download",json!({"url":"https://example.com/collection","limit":3,"options":{"durationMin":20,"collectionOrder":"reverse"}})).await?;
    assert_ne!(collection.is_error, Some(true), "{collection:?}");
    let collection = collection.structured_content.unwrap();
    assert_eq!(collection["selectionScope"], "range-local");
    assert_eq!(collection["nextOffset"], 3);
    assert_eq!(collection["results"].as_array().unwrap().len(), 2);
    assert!(collection["results"][0]["error"].is_string());
    assert!(collection["results"][1]["taskIds"].is_array());
    assert_eq!(collection["results"][1]["action"], "added");
    assert!(!collection.to_string().contains("fixture-private"));
    let first = call(
        "get_video_comments",
        json!({"sourceRef":reference,"sort":"native","limit":1}),
    )
    .await?;
    assert_ne!(first.is_error, Some(true), "{first:?}");
    let first = first.structured_content.unwrap();
    assert_eq!(first["comments"][0]["id"], "a");
    assert_eq!(first["cached"], false);
    let second = call(
        "get_video_comments",
        json!({"sourceRef":reference,"sort":"native","limit":1,"offset":1}),
    )
    .await?;
    assert_ne!(second.is_error, Some(true), "{second:?}");
    let second = second.structured_content.unwrap();
    assert_eq!(second["comments"][0]["id"], "b");
    assert_eq!(second["cached"], true);
    assert_eq!(
        std::fs::read_to_string(root.path().join("calls.log"))?
            .lines()
            .filter(|line| line.contains("--write-comments"))
            .count(),
        1
    );
    client.cancel().await?;
    host.shutdown().await;
    app.shutdown().await?;
    Ok(())
}

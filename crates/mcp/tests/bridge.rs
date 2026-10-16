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
    host.configure(true, false, port).await?;
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
                assert_eq!(response["result"]["tools"].as_array().unwrap().len(), 8);
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

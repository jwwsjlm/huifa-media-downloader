use super::*;
use std::{path::PathBuf, sync::Arc};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpListener,
};

#[tokio::test]
#[ignore = "requires bundled native yt-dlp and FFmpeg; uses local fixtures only"]
async fn native_engine_discovery_and_generic_ttml_ass_transcripts() -> Result<()> {
    let root = tempfile::tempdir()?;
    let tools = std::env::var_os("HUIFA_TEST_TOOLS")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../runtime-tools"));
    let app = App::open(crate::AppConfig {
        data_dir: root.path().join("data"),
        download_dir: root.path().join("downloads"),
        tool_dir: Some(tools),
    })
    .await?;
    let cancel = CancellationToken::new();
    let engine = app.research_engine(&cancel).await?;
    ensure!(
        engine.extractors.len() > 1000,
        "Native extractor inventory was unexpectedly small"
    );
    for provider in ["youtube", "bilibili", "soundcloud", "niconico"] {
        engine::search_provider(&engine.providers, provider)?;
    }
    let repeated = app.research_engine(&cancel).await?;
    ensure!(
        repeated.version == engine.version && repeated.providers == engine.providers,
        "Engine cache changed its search inventory"
    );
    let video = root.path().join("fixture.mp4");
    process::run_limited(
        &app.tool("ffmpeg")?,
        &[
            "-y".into(),
            "-hide_banner".into(),
            "-loglevel".into(),
            "error".into(),
            "-f".into(),
            "lavfi".into(),
            "-i".into(),
            "color=c=black:s=32x32:r=1".into(),
            "-f".into(),
            "lavfi".into(),
            "-i".into(),
            "anullsrc=r=8000:cl=mono".into(),
            "-t".into(),
            "1".into(),
            "-c:v".into(),
            "libx264".into(),
            "-c:a".into(),
            "aac".into(),
            video.to_string_lossy().into_owned(),
        ],
        None,
        &cancel,
        1024 * 1024,
        Duration::from_secs(30),
    )
    .await?;
    let video = Arc::new(std::fs::read(video)?);
    let listener = TcpListener::bind("127.0.0.1:0").await?;
    let home = format!("http://{}", listener.local_addr()?);
    let server = tokio::spawn(async move {
        while let Ok((mut socket, _)) = listener.accept().await {
            let video = Arc::clone(&video);
            tokio::spawn(async move {
                let mut buffer = [0; 16384];
                let Ok(count) = socket.read(&mut buffer).await else {
                    return;
                };
                let request = String::from_utf8_lossy(&buffer[..count]);
                let path = request
                    .split_whitespace()
                    .nth(1)
                    .unwrap_or("/")
                    .split('?')
                    .next()
                    .unwrap_or("/");
                let (mime,body) = match path {
                    "/fixture.mp4" => ("video/mp4", video.as_ref().clone()),
                    "/caption.ttml" => ("application/ttml+xml",br#"<?xml version="1.0" encoding="UTF-8"?><tt xmlns="http://www.w3.org/ns/ttml"><body><div><p begin="00:00:01.000" end="00:00:02.000">hello TTML</p></div></body></tt>"#.to_vec()),
                    "/caption.ass" => ("text/x-ssa",b"[Script Info]\nScriptType: v4.00+\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\nStyle: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1,0,2,10,10,10,1\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\nDialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,hello ASS\n".to_vec()),
                    _ => {
                        let ext = if path.contains("ass") {"ass"} else {"ttml"};
                        ("text/html",format!("<html><title>Local {ext}</title><video controls src='/fixture.mp4'><track kind='subtitles' srclang='en' label='English' src='/caption.{ext}'></video></html>").into_bytes())
                    },
                };
                let header = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: {mime}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                    body.len()
                );
                let _ = socket.write_all(header.as_bytes()).await;
                if !request.starts_with("HEAD ") {
                    let _ = socket.write_all(&body).await;
                }
                let _ = socket.shutdown().await;
            });
        }
    });
    let outcome: Result<()> = async {
        for ext in ["ttml", "ass"] {
            let url = format!("{home}/page-{ext}.html?required=private");
            let info = app
                .research("get_video_info", &json!({"url":url}), &cancel)
                .await?;
            ensure!(
                info["extractor"] == "html5" || info["extractor"] == "generic",
                "Expected generic local extraction: {info}"
            );
            let reference = text(&info, "sourceRef");
            ensure!(
                app.resolve_research_url(&json!({"sourceRef":reference}))?
                    .contains("required=private"),
                "Source reference lost its private query"
            );
            ensure!(
                !info.to_string().contains("required=private"),
                "Public metadata exposed the query"
            );
            let transcript = app
                .research(
                    "get_video_transcript",
                    &json!({"sourceRef":reference,"language":"en"}),
                    &cancel,
                )
                .await?;
            ensure!(
                text(&transcript, "text").contains(if ext == "ttml" {
                    "hello TTML"
                } else {
                    "hello ASS"
                }),
                "Transcript conversion failed: {transcript}"
            );
            let preview = app
                .research(
                    "preview_download",
                    &json!({"sourceRef":reference,"options":{"liveFilter":"all"}}),
                    &cancel,
                )
                .await?;
            ensure!(
                !preview["formats"].as_array().unwrap().is_empty(),
                "Native preview selected no formats"
            );
            ensure!(
                app.research_tasks()?.is_empty(),
                "Read-only queries created a task"
            );
        }
        Ok(())
    }
    .await;
    server.abort();
    app.shutdown().await?;
    outcome
}

use crate::{
    App, AppConfig, DownloadOptions, download,
    model::{Chapter, SourceFile, SourceManifest, Task, text},
};
use anyhow::{Context, Result, ensure};
use serde_json::json;
use std::{
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpListener,
};
use tokio_util::sync::CancellationToken;

struct LocalServer {
    url: String,
    stop: CancellationToken,
    requests: Arc<Mutex<Vec<String>>>,
    handle: tokio::task::JoinHandle<()>,
}
impl LocalServer {
    async fn start(root: PathBuf) -> Result<Self> {
        let listener = TcpListener::bind("127.0.0.1:0").await?;
        let url = format!("http://{}", listener.local_addr()?);
        let stop = CancellationToken::new();
        let shutdown = stop.clone();
        let requests = Arc::new(Mutex::new(Vec::new()));
        let requested = requests.clone();
        let handle = tokio::spawn(async move {
            loop {
                let accepted =
                    tokio::select! {v=listener.accept()=>v,_=shutdown.cancelled()=>break};
                let Ok((mut socket, _)) = accepted else {
                    break;
                };
                let root = root.clone();
                let requested = requested.clone();
                tokio::spawn(async move {
                    let mut bytes = vec![0; 16384];
                    let Ok(Ok(size)) =
                        tokio::time::timeout(Duration::from_secs(5), socket.read(&mut bytes)).await
                    else {
                        return;
                    };
                    let request = String::from_utf8_lossy(&bytes[..size]);
                    let mut words = request.split_whitespace();
                    let method = words.next().unwrap_or("");
                    let url = words.next().unwrap_or("");
                    requested.lock().unwrap().push(url.into());
                    let path = url.split('?').next().unwrap_or("").trim_start_matches('/');
                    if path.contains("..") || path.contains('\\') {
                        return;
                    }
                    if path == "generic.mp4"
                        && !request.lines().any(|line| {
                            line.split_once(':').is_some_and(|(name, value)| {
                                name.eq_ignore_ascii_case("cookie")
                                    && value
                                        .split(';')
                                        .any(|cookie| cookie.trim() == "login_sid=local-auth")
                            })
                        })
                    {
                        let _ = socket.write_all(b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\nConnection: close\r\n\r\n").await;
                        return;
                    }
                    let file = root.join(path);
                    let body = tokio::fs::read(&file).await.unwrap_or_default();
                    let status = if file.is_file() {
                        "200 OK"
                    } else {
                        "404 Not Found"
                    };
                    let mime = if path.ends_with(".mpd") {
                        "application/dash+xml"
                    } else if path.ends_with(".html") {
                        "text/html"
                    } else {
                        "application/octet-stream"
                    };
                    let header = format!(
                        "HTTP/1.1 {status}\r\nContent-Type: {mime}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                        body.len()
                    );
                    let _ = socket.write_all(header.as_bytes()).await;
                    if method != "HEAD" {
                        let _ = socket.write_all(&body).await;
                    }
                    let _ = socket.shutdown().await;
                });
            }
        });
        Ok(Self {
            url,
            stop,
            requests,
            handle,
        })
    }
    async fn shutdown(self) {
        self.stop.cancel();
        let _ = self.handle.await;
    }
}

fn tool_directory() -> PathBuf {
    std::env::var_os("HUIFA_TEST_TOOLS")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../runtime-tools"))
}
fn config(root: &Path) -> AppConfig {
    AppConfig {
        data_dir: root.join("data"),
        download_dir: root.join("output"),
        tool_dir: Some(tool_directory()),
    }
}

#[tokio::test]
#[ignore = "requires HUIFA_TEST_DOUYIN=1, native tools and public Douyin access"]
async fn real_douyin_short_link_identification_and_download() -> Result<()> {
    if std::env::var("HUIFA_TEST_DOUYIN").as_deref() != Ok("1") {
        eprintln!("Skipping public Douyin test; set HUIFA_TEST_DOUYIN=1 to enable it");
        return Ok(());
    }
    let root = tempfile::tempdir()?;
    let app = App::open(config(root.path())).await?;
    let url = "https://v.douyin.com/L1nHlRt2ohQ";
    let outcome = async {
        let identified = app.command("probe.collection", json!({"url":url})).await?;
        ensure!(identified["kind"] == "video", "Douyin short link was not identified as a video");
        ensure!(identified["id"] == "7695747024182590437", "Douyin resolved to a different video");
        let result = app.command("tasks.add", json!({"urls":[url], "options":{"quality":"best","subtitleLanguage":"none","collectionMode":"single"}})).await?;
        let id = result[0].as_str().context("Download task was not created")?;
        let task = tokio::time::timeout(Duration::from_secs(180), async {
            loop {
                let task = app.db.task(id)?;
                if matches!(task.status.as_str(), "completed" | "failed" | "processing_failed" | "cancelled") {
                    return Ok::<_, anyhow::Error>(task);
                }
                tokio::time::sleep(Duration::from_millis(100)).await;
            }
        }).await.context("Douyin task timed out")??;
        ensure!(task.status == "completed", "Douyin task failed: {}", task.error.as_deref().unwrap_or(&task.stage));
        let output = task.output_path.context("Douyin result was not published")?;
        ensure!(output.is_file() && std::fs::metadata(output)?.len() > 0, "Douyin download produced no media file");
        ensure!(app.db.media()?.len() == 1, "Douyin result was not added to the media library");
        Ok::<_, anyhow::Error>(())
    }.await;
    app.shutdown().await?;
    outcome
}

async fn fixture(app: &App, root: &Path) -> Result<()> {
    let args = vec![
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=160x90:rate=12",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=44100",
        "-t",
        "4",
        "-threads",
        "1",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-c:a",
        "aac",
        "-f",
        "dash",
        "-seg_duration",
        "1",
    ]
    .into_iter()
    .map(str::to_string)
    .chain([root.join("sample.mpd").to_string_lossy().into()])
    .collect::<Vec<_>>();
    crate::process::run(
        &app.tool("ffmpeg")?,
        &args,
        Some(root),
        &CancellationToken::new(),
        None,
    )
    .await?;
    Ok(())
}
async fn wait_for(app: &App, ids: &[String], states: &[&str]) -> Result<Vec<Task>> {
    tokio::time::timeout(Duration::from_secs(60), async {
        loop {
            let tasks = ids
                .iter()
                .map(|id| app.db.task(id))
                .collect::<Result<Vec<_>>>()?;
            if tasks.iter().all(|t| states.contains(&t.status.as_str())) {
                return Ok(tasks);
            }
            if let Some(task) = tasks.iter().find(|t| {
                matches!(t.status.as_str(), "failed" | "processing_failed")
                    && !states.contains(&t.status.as_str())
            }) {
                anyhow::bail!(
                    "Task {} status {} failed at {}: {:?}",
                    task.id,
                    task.status,
                    task.stage,
                    task.error
                );
            }
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
    })
    .await
    .with_context(|| {
        format!(
            "Timed out waiting for local media tasks: {:?}",
            ids.iter()
                .filter_map(|id| app.db.task(id).ok())
                .map(|t| t.public())
                .collect::<Vec<_>>()
        )
    })?
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
#[ignore = "requires installed native yt-dlp, FFmpeg and FFprobe; run cargo test --release -p huifa-core -- --ignored"]
async fn real_split_streams_queue_offline_restart_and_publication_recovery() -> Result<()> {
    let root = tempfile::tempdir()?;
    let fixtures = root.path().join("fixtures");
    std::fs::create_dir_all(&fixtures)?;
    let app = App::open(config(root.path())).await?;
    fixture(&app, &fixtures).await?;
    image::RgbImage::from_pixel(160, 90, image::Rgb([60, 30, 90]))
        .save(fixtures.join("preview.png"))?;
    let server = LocalServer::start(fixtures.clone()).await?;
    for index in 0..4 {
        let metadata = json!({"@context":"https://schema.org","@type":"VideoObject","name":format!("local split {index}"),"duration":"PT4S","contentUrl":format!("{}/sample.mpd",server.url),"thumbnailUrl":format!("{}/preview.png?signature=private",server.url)});
        std::fs::write(
            fixtures.join(format!("sample-{index}.html")),
            format!(
                "<html><title>local split {index}</title><script type='application/ld+json'>{metadata}</script></html>"
            ),
        )?;
    }
    let processor = app.processor.clone().acquire_owned().await?;
    let options = json!({"writeThumbnail":false,"encoder":"copy","writeInfoJson":true});
    let mut events = app.subscribe();
    let ids:Vec<String>=serde_json::from_value(app.command("tasks.add",json!({"urls":(0..4).map(|i|format!("{}/sample-{i}.html",server.url)).collect::<Vec<_>>(),"options":options})).await?)?;
    tokio::time::timeout(Duration::from_secs(60), async {
        loop {
            let event = events.recv().await?;
            if event.kind == "task.upsert" && event.data["hasThumbnail"] == true {
                let id = event.data["id"]
                    .as_str()
                    .context("Preview event has no task ID")?;
                ensure!(
                    ids.iter().any(|candidate| candidate == id),
                    "Unexpected preview task"
                );
                ensure!(
                    event.data["status"] == "probing",
                    "Preview was not ready immediately after metadata parsing"
                );
                ensure!(
                    app.db.media_for_task(id)?.is_none(),
                    "Task preview required completed media"
                );
                let file = app
                    .command("media.file", json!({"id":id,"kind":"taskThumbnail"}))
                    .await?;
                let path = PathBuf::from(file["path"].as_str().context("Preview path is missing")?);
                ensure!(
                    file["mimeType"] == "image/jpeg",
                    "Preview was not converted to JPEG"
                );
                ensure!(
                    image::guess_format(&std::fs::read(path)?)? == image::ImageFormat::Jpeg,
                    "Preview endpoint did not return local JPEG bytes"
                );
                ensure!(
                    !event.data.to_string().contains("signature=private"),
                    "Task preview exposed a signed source URL"
                );
                return Ok::<_, anyhow::Error>(());
            }
        }
    })
    .await
    .context("Timed out waiting for a task thumbnail before media publication")??;
    let waiting = wait_for(&app, &ids, &["waiting_processing"]).await?;
    ensure!(
        app.network.available_permits() == 3,
        "Processing held network permits"
    );
    ensure!(
        app.db.media()?.is_empty(),
        "A task was published before processing"
    );
    for task in &waiting {
        ensure!(
            task.public()["hasThumbnail"] == true,
            "Preview was lost when cover export was disabled"
        );
        let manifest = task.manifest.as_ref().context("No source manifest")?;
        ensure!(
            manifest.files.iter().filter(|f| f.kind == "video").count() == 1
                && manifest.files.iter().filter(|f| f.kind == "audio").count() == 1,
            "Split media was not downloaded as individual exact streams"
        );
        for file in &manifest.files {
            ensure!(
                download::hash_file(&file.path)? == (file.hash.clone(), file.size),
                "Source manifest hash mismatch"
            );
        }
    }
    let mut paused = waiting[0].clone();
    paused.progress = 42.0;
    paused.stage_progress = Some(42.0);
    paused.stage_indeterminate = false;
    app.save_task(&paused)?;
    app.command("tasks.pause", json!({"id":ids[0]})).await?;
    ensure!(
        app.db.task(&ids[0])?.stage_progress == Some(42.0),
        "Pause discarded measured progress"
    );
    let requests = server.requests.clone();
    let request_count = requests.lock().unwrap().len();
    server.shutdown().await;
    app.shutdown().await?;
    drop(processor);
    drop(app);
    let app = App::open(config(root.path())).await?;
    app.command("tasks.resume", json!({"id":ids[0]})).await?;
    let completed = wait_for(&app, &ids, &["completed"]).await?;
    ensure!(
        requests.lock().unwrap().len() == request_count,
        "Restart re-downloaded local inputs"
    );
    ensure!(
        app.db.media()?.len() == 4,
        "Publication inserted duplicate catalog rows"
    );
    let first = completed[0].clone();
    let media = app
        .db
        .media_for_task(&first.id)?
        .context("No completed media")?;
    let output = crate::process::run(
        &app.tool("ffprobe")?,
        &[
            "-v".into(),
            "error".into(),
            "-show_streams".into(),
            "-of".into(),
            "json".into(),
            media.path.to_string_lossy().into(),
        ],
        None,
        &CancellationToken::new(),
        None,
    )
    .await?;
    let probe: serde_json::Value = serde_json::from_str(&output.stdout)?;
    ensure!(
        probe["streams"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|s| text(s, "codec_type") == "video")
            .count()
            == 1
            && probe["streams"]
                .as_array()
                .unwrap()
                .iter()
                .filter(|s| text(s, "codec_type") == "audio")
                .count()
                == 1,
        "Processed split output topology is invalid"
    );
    let expected_hash = download::hash_file(&media.path)?;
    let mut journal = first.clone();
    // Replay the crash after file installation but before the atomic task/media commit.
    journal
        .publication
        .as_mut()
        .context("No publication journal")?
        .committed = false;
    journal.stage("processing", "publishing", None);
    journal
        .tools
        .insert("ffmpeg".into(), root.path().join("must-not-run-ffmpeg"));
    journal
        .tools
        .insert("yt-dlp".into(), root.path().join("must-not-run-ytdlp"));
    app.save_task(&journal)?;
    app.shutdown().await?;
    drop(app);
    let database = rusqlite::Connection::open(root.path().join("data/rust-core.sqlite3"))?;
    database.execute("DELETE FROM media WHERE task_id=?1", [&first.id])?;
    drop(database);
    let app = App::open(config(root.path())).await?;
    wait_for(&app, std::slice::from_ref(&first.id), &["completed"]).await?;
    ensure!(
        app.db.media()?.len() == 4,
        "Publication recovery catalog insert was not unique"
    );
    ensure!(
        download::hash_file(&media.path)? == expected_hash,
        "Publication recovery altered validated media"
    );
    app.shutdown().await?;
    Ok(())
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
#[ignore = "requires installed FFmpeg/FFprobe; run cargo test --release -p huifa-core -- --ignored"]
async fn real_processing_failure_retry_uses_local_sources_and_manual_conversion_one_catalog_row()
-> Result<()> {
    let root = tempfile::tempdir()?;
    let fixtures = root.path().join("fixtures");
    std::fs::create_dir_all(&fixtures)?;
    let app = App::open(config(root.path())).await?;
    fixture(&app, &fixtures).await?;
    let server = LocalServer::start(fixtures.clone()).await?;
    let processor = app.processor.clone().acquire_owned().await?;
    let ids:Vec<String>=serde_json::from_value(app.command("tasks.add",json!({"urls":[format!("{}/sample.mpd",server.url)],"options":{"writeThumbnail":false}})).await?)?;
    let mut task = wait_for(&app, &ids, &["waiting_processing"])
        .await?
        .remove(0);
    task.options.encoder = "invalid_test_encoder".into();
    app.save_task(&task)?;
    let requests = server.requests.clone();
    let request_count = requests.lock().unwrap().len();
    server.shutdown().await;
    drop(processor);
    wait_for(&app, &ids, &["processing_failed"]).await?;
    let failed = app.db.task(&ids[0])?;
    for file in &failed.manifest.as_ref().unwrap().files {
        ensure!(
            download::hash_file(&file.path)? == (file.hash.clone(), file.size),
            "Processing failure changed its source"
        );
    }
    let mut task = failed;
    task.options.encoder = "libx264".into();
    app.save_task(&task)?;
    app.command("tasks.retry", json!({"id":ids[0]})).await?;
    wait_for(&app, &ids, &["completed"]).await?;
    let original = app.db.media_for_task(&ids[0])?.unwrap();
    app.command("tasks.convert", json!({"id":ids[0],"encoder":"libx264"}))
        .await?;
    wait_for(&app, &ids, &["completed"]).await?;
    let converted = app.db.media_for_task(&ids[0])?.unwrap();
    ensure!(
        converted.id == original.id && app.db.media()?.len() == 1,
        "Manual conversion duplicated its media row"
    );
    ensure!(
        requests.lock().unwrap().len() == request_count,
        "Local retry or manual conversion used network"
    );
    app.shutdown().await?;
    Ok(())
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
#[ignore = "requires installed FFmpeg/FFprobe; run cargo test --release -p huifa-core -- --ignored"]
async fn real_multiple_audio_cuts_subtitles_chapters_and_cover_processing() -> Result<()> {
    let root = tempfile::tempdir()?;
    let app = App::open(config(root.path())).await?;
    let id = uuid::Uuid::new_v4().to_string();
    let sources = app.config.data_dir.join("tasks").join(&id).join("sources");
    std::fs::create_dir_all(&sources)?;
    let media = sources.join("video.mkv");
    let args = [
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=160x90:rate=12",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=880:sample_rate=48000",
        "-t",
        "4",
        "-map",
        "0:v",
        "-map",
        "1:a",
        "-map",
        "2:a",
        "-metadata:s:a:0",
        "language=eng",
        "-metadata:s:a:1",
        "language=jpn",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-c:a",
        "aac",
        "-threads",
        "1",
    ]
    .into_iter()
    .map(str::to_string)
    .chain([media.to_string_lossy().into()])
    .collect::<Vec<_>>();
    crate::process::run(
        &app.tool("ffmpeg")?,
        &args,
        None,
        &CancellationToken::new(),
        None,
    )
    .await?;
    let subtitle = sources.join("subtitle.en.srt");
    std::fs::write(
        &subtitle,
        "1\n00:00:01,000 --> 00:00:03,000\nRetimed subtitle\n\n",
    )?;
    let thumbnail = sources.join("thumbnail.jpg");
    image::RgbImage::from_pixel(80, 80, image::Rgb([20, 60, 180])).save(&thumbnail)?;
    let mut files = Vec::new();
    for (path, kind, language) in [
        (media, "video", None),
        (subtitle, "subtitle", Some("en".to_string())),
        (thumbnail, "thumbnail", None),
    ] {
        let (hash, size) = download::hash_file(&path)?;
        files.push(SourceFile {
            path,
            hash,
            size,
            kind: kind.into(),
            format_id: "local".into(),
            language,
        });
    }
    let options = DownloadOptions::parse(
        json!({"container":"mkv","audioTrack":"all","subtitleLanguages":["en"],"subtitleFormat":"ass","embedSubtitles":true,"splitChapters":true,"embedChapters":true,"embedMetadata":true,"embedThumbnail":true,"prependCoverEnabled":true,"prependCoverFrames":3,"sectionStart":"1","sectionEnd":"3.5","sponsorblockMode":"remove","sponsorblockCategories":["sponsor"],"encoder":"libx264","writeInfoJson":false}),
    )?;
    let manifest = SourceManifest {
        version: 1,
        directory: sources,
        files,
        metadata: json!({"id":"local-options","title":"Option sample","uploader":"Local artist","description":"Metadata preserved"}),
        duration: Some(4.0),
        chapters: vec![
            Chapter {
                start: 0.0,
                end: 2.0,
                title: "First".into(),
            },
            Chapter {
                start: 2.0,
                end: 4.0,
                title: "Second".into(),
            },
        ],
        sponsor_segments: vec![Chapter {
            start: 1.5,
            end: 2.0,
            title: "sponsor".into(),
        }],
        output_extension: "mkv".into(),
    };
    let task = Task {
        kind: "media".into(),
        files: vec![],
        id: id.clone(),
        url: "https://example.test/offline".into(),
        title: "Option sample".into(),
        thumbnail: None,
        size: None,
        source_info: None,
        media_info: None,
        steps: vec![],
        step_index: None,
        status: "waiting_processing".into(),
        stage: "waiting_processing".into(),
        progress: 0.0,
        stage_progress: None,
        stage_indeterminate: true,
        speed: None,
        eta: None,
        error: None,
        warnings: vec![],
        created_at: chrono::Utc::now().to_rfc3339(),
        options,
        output_path: None,
        previous_outputs: vec![],
        cookie_snapshot: None,
        proxy_snapshot: None,
        manifest: Some(manifest),
        publication: None,
        conversion: false,
        tools: app.freeze_tools().await?,
    };
    app.db.insert_task(&task)?;
    app.schedule(&id)?;
    let complete = wait_for(&app, std::slice::from_ref(&id), &["completed"])
        .await?
        .remove(0);
    let media = app.db.media_for_task(&id)?.unwrap();
    let output = crate::process::run(
        &app.tool("ffprobe")?,
        &[
            "-v".into(),
            "error".into(),
            "-show_streams".into(),
            "-show_chapters".into(),
            "-show_format".into(),
            "-of".into(),
            "json".into(),
            media.path.to_string_lossy().into(),
        ],
        None,
        &CancellationToken::new(),
        None,
    )
    .await?;
    let probe: serde_json::Value = serde_json::from_str(&output.stdout)?;
    let streams = probe["streams"].as_array().unwrap();
    ensure!(
        streams
            .iter()
            .filter(|s| text(s, "codec_type") == "audio")
            .count()
            == 2,
        "Multiple audio tracks were lost"
    );
    ensure!(
        streams
            .iter()
            .any(|s| text(s, "codec_type") == "subtitle" && text(s, "codec_name") == "ass"),
        "Requested ASS subtitle was not embedded"
    );
    ensure!(
        streams.iter().any(|s| text(s, "codec_type") == "attachment"
            || s["disposition"]["attached_pic"].as_u64() == Some(1)),
        "Cover was not embedded: {streams:?}"
    );
    let chapters = probe["chapters"]
        .as_array()
        .context("Retimed chapters and cover chapter were not embedded")?;
    ensure!(
        chapters.len() == 3,
        "Retimed chapters and cover chapter were not embedded"
    );
    for (chapter, (start, end, title)) in chapters.iter().zip([
        (0.0, 0.25, "Cover"),
        (0.25, 0.75, "First"),
        (0.75, 2.25, "Second"),
    ]) {
        ensure!(
            (text(chapter, "start_time").parse::<f64>()? - start).abs() < 0.002
                && (text(chapter, "end_time").parse::<f64>()? - end).abs() < 0.002,
            "Chapter timing is invalid: {chapter}"
        );
        ensure!(
            text(&chapter["tags"], "title") == title,
            "Chapter title was lost: {chapter}"
        );
    }
    ensure!(
        text(&probe["format"]["tags"], "title") == "Option sample",
        "Metadata title was not embedded"
    );
    ensure!(
        (media.duration - 2.25).abs() < 0.25,
        "Cuts or prepend cover duration are incorrect: {}",
        media.duration
    );
    ensure!(
        media
            .assets
            .iter()
            .filter(|p| p
                .file_name()
                .unwrap()
                .to_string_lossy()
                .contains(".chapter-"))
            .count()
            == 3,
        "Split chapter outputs were not published"
    );
    let subtitle = media
        .assets
        .iter()
        .find(|p| p.extension().is_some_and(|e| e == "ass"))
        .context("Converted subtitle sidecar was not published")?;
    let subtitle = std::fs::read_to_string(subtitle)?;
    ensure!(
        subtitle.contains("Retimed subtitle")
            && subtitle.contains("0:00:00.25,0:00:00.75")
            && subtitle.contains("0:00:00.75,0:00:01.75"),
        "Subtitle text or timing was lost: {subtitle}"
    );
    for (index, duration) in [0.25, 0.5, 1.5].into_iter().enumerate() {
        let name = format!(".chapter-{:02}-", index + 1);
        let chapter = media
            .assets
            .iter()
            .find(|p| p.file_name().unwrap().to_string_lossy().contains(&name))
            .context("Chapter output is missing")?;
        let output = crate::process::run(
            &app.tool("ffprobe")?,
            &[
                "-v".into(),
                "error".into(),
                "-show_format".into(),
                "-of".into(),
                "json".into(),
                chapter.to_string_lossy().into(),
            ],
            None,
            &CancellationToken::new(),
            None,
        )
        .await?;
        let probe: serde_json::Value = serde_json::from_str(&output.stdout)?;
        let measured = text(&probe["format"], "duration").parse::<f64>()?;
        ensure!(
            (measured - duration).abs() < 0.2,
            "Split chapter {} duration is incorrect: expected {duration}, measured {measured}",
            index + 1
        );
    }
    ensure!(
        complete.stage_progress == Some(100.0) && app.db.media()?.len() == 1,
        "Completed progress/catalog are invalid"
    );
    let mut marked = task.clone();
    marked.id = uuid::Uuid::new_v4().to_string();
    marked.title = "Sponsor outro metadata".into();
    marked.options = DownloadOptions::parse(
        json!({"container":"mp4","encoder":"copy","subtitleLanguages":["en"],"subtitleFormat":"srt","embedSubtitles":true,"embedChapters":true,"embedMetadata":true,"embedThumbnail":true,"writeThumbnail":true,"writeInfoJson":false,"sponsorblockMode":"mark","sponsorblockCategories":["outro"]}),
    )?;
    let manifest = marked.manifest.as_mut().unwrap();
    manifest.directory = app
        .config
        .data_dir
        .join("tasks")
        .join(&marked.id)
        .join("sources");
    std::fs::create_dir_all(&manifest.directory)?;
    for source in &mut manifest.files {
        let destination = manifest.directory.join(source.path.file_name().unwrap());
        std::fs::copy(&source.path, &destination)?;
        source.path = destination;
    }
    manifest.metadata["title"] = json!(marked.title);
    manifest.output_extension = "mp4".into();
    manifest.sponsor_segments = vec![Chapter {
        start: 3.0,
        end: 4.0,
        title: "SponsorBlock: outro".into(),
    }];
    app.db.insert_task(&marked)?;
    app.schedule(&marked.id)?;
    wait_for(&app, std::slice::from_ref(&marked.id), &["completed"]).await?;
    let media = app.db.media_for_task(&marked.id)?.unwrap();
    let output = crate::process::run(
        &app.tool("ffprobe")?,
        &[
            "-v".into(),
            "error".into(),
            "-show_streams".into(),
            "-show_chapters".into(),
            "-of".into(),
            "json".into(),
            media.path.to_string_lossy().into(),
        ],
        None,
        &CancellationToken::new(),
        None,
    )
    .await?;
    let probe: serde_json::Value = serde_json::from_str(&output.stdout)?;
    let streams = probe["streams"].as_array().unwrap();
    ensure!(
        streams.iter().any(|s| text(s, "codec_name") == "mov_text"),
        "MP4 subtitles were not embedded"
    );
    ensure!(
        streams
            .iter()
            .any(|s| s["disposition"]["attached_pic"].as_u64() == Some(1)),
        "MP4 cover image was not embedded"
    );
    let sponsor = probe["chapters"]
        .as_array()
        .into_iter()
        .flatten()
        .find(|c| text(&c["tags"], "title") == "SponsorBlock: outro")
        .with_context(|| {
            format!(
                "SponsorBlock outro was not marked as a chapter: {}",
                probe["chapters"]
            )
        })?;
    ensure!(
        (text(sponsor, "start_time").parse::<f64>()? - 3.0).abs() < 0.002
            && (text(sponsor, "end_time").parse::<f64>()? - 4.0).abs() < 0.002,
        "SponsorBlock mark changed timing"
    );
    ensure!(
        (media.duration - 4.0).abs() < 0.25,
        "SponsorBlock mark removed media content"
    );
    app.shutdown().await?;
    Ok(())
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
#[ignore = "requires installed native yt-dlp, FFmpeg and FFprobe; run cargo test --release -p huifa-core -- --ignored"]
async fn real_generic_http_mp4_and_audio_extraction() -> Result<()> {
    let root = tempfile::tempdir()?;
    let fixtures = root.path().join("fixtures");
    std::fs::create_dir_all(&fixtures)?;
    let app = App::open(config(root.path())).await?;
    let input = fixtures.join("generic.mp4");
    let args = [
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=160x90:rate=12",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000",
        "-t",
        "4",
        "-threads",
        "1",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-c:a",
        "aac",
    ]
    .into_iter()
    .map(str::to_string)
    .chain([input.to_string_lossy().into()])
    .collect::<Vec<_>>();
    crate::process::run(
        &app.tool("ffmpeg")?,
        &args,
        None,
        &CancellationToken::new(),
        None,
    )
    .await?;
    let server = LocalServer::start(fixtures).await?;
    let url = format!("{}/generic.mp4", server.url);
    ensure!(
        download::public_probe(&app, &url, &DownloadOptions::default(), None)
            .await
            .is_err(),
        "Cookie-protected media was available without login"
    );
    let cookies = crate::browser::netscape(
        &json!([{"domain":"127.0.0.1","path":"/","name":"login_sid","value":"local-auth","httpOnly":true,"secure":false,"session":true}]),
    )?;
    app.sync_login_cookies("embedded", "Local login", Some(&cookies))?;
    app.command("probe.video", json!({"url":url})).await?;
    let mut previous: Option<crate::model::Media> = None;
    for (options, video, codec, duration) in [
        (
            json!({"container":"mp4","encoder":"libx264","writeThumbnail":false,"writeInfoJson":false}),
            true,
            "aac",
            4.0,
        ),
        (
            json!({"contentMode":"audio","audioFormat":"flac","sectionStart":"1","sectionEnd":"3","writeThumbnail":false,"writeInfoJson":false}),
            false,
            "flac",
            2.0,
        ),
        (
            json!({"contentMode":"audio","audioFormat":"mp3","writeThumbnail":false,"writeInfoJson":false}),
            false,
            "mp3",
            4.0,
        ),
        (
            json!({"contentMode":"audio","audioFormat":"aac","writeThumbnail":false,"writeInfoJson":false}),
            false,
            "aac",
            4.0,
        ),
    ] {
        let processor = app.processor.clone().acquire_owned().await?;
        let old_hash = previous
            .as_ref()
            .map(|media| download::hash_file(&media.path))
            .transpose()?;
        let ids: Vec<String> = serde_json::from_value(
            app.command("tasks.add", json!({"urls":[url],"options":options}))
                .await?,
        )?;
        let id = &ids[0];
        if let Some(previous) = &previous {
            ensure!(
                id == &previous.task_id,
                "Repeated URL did not reuse its task ID"
            );
        }
        wait_for(&app, &ids, &["waiting_processing"]).await?;
        if let Some(previous) = &previous {
            let retained = app
                .db
                .media_for_task(id)?
                .context("Previous media disappeared while re-downloading")?;
            ensure!(
                retained.id == previous.id && retained.path == previous.path,
                "Previous media record changed before publication"
            );
            ensure!(
                download::hash_file(&previous.path)? == old_hash.unwrap(),
                "Previous result changed before new publication succeeded"
            );
        }
        drop(processor);
        wait_for(&app, &ids, &["completed"]).await?;
        let media = app
            .db
            .media_for_task(id)?
            .context("Generic MP4 task was not published")?;
        if let Some(previous) = &previous {
            ensure!(
                media.id == previous.id,
                "Re-downloading changed the media ID"
            );
            ensure!(
                media.path == previous.path || !previous.path.exists(),
                "Obsolete managed output remained after successful replacement"
            );
        }
        let output = crate::process::run(
            &app.tool("ffprobe")?,
            &[
                "-v".into(),
                "error".into(),
                "-show_streams".into(),
                "-of".into(),
                "json".into(),
                media.path.to_string_lossy().into(),
            ],
            None,
            &CancellationToken::new(),
            None,
        )
        .await?;
        let probe: serde_json::Value = serde_json::from_str(&output.stdout)?;
        let streams = probe["streams"].as_array().unwrap();
        ensure!(
            streams
                .iter()
                .filter(|s| text(s, "codec_type") == "video")
                .count()
                == usize::from(video),
            "Generic media output has unexpected video streams"
        );
        ensure!(
            streams
                .iter()
                .filter(|s| text(s, "codec_type") == "audio" && text(s, "codec_name") == codec)
                .count()
                == 1,
            "Requested {codec} audio output is invalid"
        );
        ensure!(
            (media.duration - duration).abs() < 0.25,
            "Generic output duration is invalid: {}",
            media.duration
        );
        ensure!(
            app.db.media()?.len() == 1,
            "Re-downloading created duplicate media rows"
        );
        previous = Some(media);
    }
    ensure!(
        app.db.media()?.len() == 1 && app.db.tasks()?.len() == 1,
        "Generic media catalog rows are invalid"
    );
    server.shutdown().await;
    app.shutdown().await?;
    Ok(())
}

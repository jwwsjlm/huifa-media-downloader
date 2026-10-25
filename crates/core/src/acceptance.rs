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

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
#[ignore = "requires HUIFA_TEST_MEDIA and installed tools; reads an existing local video without compressing it"]
async fn real_compression_previews_existing_media_with_bounded_analysis() -> Result<()> {
    let Some(source) = std::env::var_os("HUIFA_TEST_MEDIA") else {
        eprintln!("Existing media preview skipped: set HUIFA_TEST_MEDIA to a local video");
        return Ok(());
    };
    let source = PathBuf::from(source).canonicalize()?;
    let before = std::fs::metadata(&source)?;
    let root = tempfile::tempdir()?;
    let mut configuration = config(root.path());
    configuration.download_dir = source
        .parent()
        .context("Video directory is missing")?
        .to_path_buf();
    let app = App::open(configuration).await?;
    let mut original = crate::tests::paused_task(Default::default());
    original.status = "completed".into();
    original.stage = "completed".into();
    original.output_path = Some(source.clone());
    app.db.insert_task(&original)?;
    app.db.complete(
        &original,
        &crate::model::Media {
            id: uuid::Uuid::new_v4().to_string(),
            task_id: original.id.clone(),
            title: original.title.clone(),
            filename: source
                .file_name()
                .context("Video filename is missing")?
                .to_string_lossy()
                .into(),
            size: before.len(),
            duration: 0.0,
            path: source.clone(),
            assets: vec![],
            media_info: None,
        },
    )?;
    let started = std::time::Instant::now();
    let preview = app.command(
        "tasks.convertPreview",
        json!({"id":original.id,"compression":{},"requestId":"existing-local-video-preview"}),
    );
    tokio::pin!(preview);
    let result = tokio::time::timeout(Duration::from_secs(60), &mut preview).await;
    if result.is_err() {
        app.command("probe.cancel", json!({"id":"existing-local-video-preview"}))
            .await?;
        let _ = tokio::time::timeout(Duration::from_secs(10), &mut preview).await;
    }
    let value = result.context("Existing local video preview exceeded 60 seconds")??;
    ensure!(
        value["source"]["sizeBytes"] == before.len(),
        "Preview used a stale size"
    );
    let duration = value["source"]["duration"]
        .as_f64()
        .context("Video duration is missing")?;
    let rate = value["source"]["videoBitrateBps"]
        .as_u64()
        .context("Video bitrate is missing")?;
    ensure!(
        duration > 0.0 && rate >= 1000 && rate as f64 <= before.len() as f64 * 16.0 / duration,
        "Preview bitrate is inconsistent with the whole video duration: {value}"
    );
    ensure!(
        value["capabilities"]
            .as_array()
            .is_some_and(|items| !items.is_empty()),
        "Encoder capabilities are missing"
    );
    let after = std::fs::metadata(&source)?;
    ensure!(
        before.len() == after.len() && before.modified()? == after.modified()?,
        "Preview changed the original video"
    );
    eprintln!(
        "Existing local video preview: {:.2}s; {} bytes; {:.3}s duration; {} bps; {}",
        started.elapsed().as_secs_f64(),
        before.len(),
        duration,
        rate,
        value["capabilities"]
    );
    // Reserving the processor proves submission does not scan or checksum a multi-GB source.
    let slot = app.processor.clone().acquire_many_owned(2).await?;
    let queued_at = std::time::Instant::now();
    let queued = tokio::time::timeout(
        Duration::from_secs(2),
        app.command(
            "tasks.convert",
            json!({"id":original.id,"compression":{"targetBytes":before.len()*7/10}}),
        ),
    )
    .await??;
    let id = queued["id"]
        .as_str()
        .context("Compression task has no ID")?;
    ensure!(
        queued["status"] == "waiting_processing",
        "Compression did not return a visible waiting task"
    );
    ensure!(
        app.db.task(id)?.manifest.unwrap().files[0]
            .hash
            .starts_with("pending-sha256:"),
        "Compression submission read the full source before entering the queue"
    );
    eprintln!(
        "Existing {}-byte source queued in {:.3}s without acquiring a processing slot",
        before.len(),
        queued_at.elapsed().as_secs_f64()
    );
    app.command("tasks.cancel", json!({"id":id})).await?;
    drop(slot);
    let after = std::fs::metadata(&source)?;
    ensure!(
        before.len() == after.len() && before.modified()? == after.modified()?,
        "Queue/cancellation changed the original video"
    );
    app.shutdown().await?;
    Ok(())
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
#[ignore = "requires installed FFmpeg/FFprobe; local fixture only"]
async fn real_compression_preserves_sources_tracks_chapters_and_estimates_size() -> Result<()> {
    let root = tempfile::tempdir()?;
    let app = App::open(config(root.path())).await?;
    let token = CancellationToken::new();
    let source = app.download_directory().join("compression source.mkv");
    let subtitle = root.path().join("captions.srt");
    let attachment = root.path().join("fixture.txt");
    let chapters = root.path().join("chapters.ffmeta");
    std::fs::write(
        &subtitle,
        "1\n00:00:00,000 --> 00:00:07,000\nCompression keeps this subtitle.\n",
    )?;
    std::fs::write(&attachment, "preserved attachment bytes")?;
    std::fs::write(
        &chapters,
        ";FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=4000\ntitle=First half\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=4000\nEND=8000\ntitle=Second half\n",
    )?;
    let args = [
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=640x360:rate=30000/1001",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=880:sample_rate=48000",
        "-i",
        subtitle.to_str().unwrap(),
        "-f",
        "ffmetadata",
        "-i",
        chapters.to_str().unwrap(),
        "-map",
        "0:v",
        "-map",
        "1:a",
        "-map",
        "2:a",
        "-map",
        "3:s",
        "-map_chapters",
        "4",
        "-t",
        "8",
        "-c:v",
        "libx264",
        "-threads",
        "2",
        "-preset",
        "ultrafast",
        "-qp",
        "8",
        "-c:a",
        "aac",
        "-b:a",
        "64k",
        "-c:s",
        "srt",
        "-metadata:s:a:0",
        "language=eng",
        "-metadata:s:a:1",
        "language=jpn",
        "-disposition:a:0",
        "default",
        "-disposition:a:1",
        "0",
        "-metadata:s:s:0",
        "language=eng",
        "-disposition:s:0",
        "forced",
        "-attach",
        attachment.to_str().unwrap(),
        "-metadata:s:t:0",
        "mimetype=text/plain",
        source.to_str().unwrap(),
    ]
    .into_iter()
    .map(str::to_string)
    .collect::<Vec<_>>();
    crate::process::run(&app.tool("ffmpeg")?, &args, None, &token, None).await?;
    let original_hash = download::hash_file(&source)?;
    let mut original = crate::tests::paused_task(app.freeze_tools().await?);
    original.title = "Compression fixture".into();
    original.status = "completed".into();
    original.stage = "completed".into();
    original.progress = 100.0;
    original.size = Some(original_hash.1);
    original.output_path = Some(source.clone());
    original.download_dir = Some(app.download_directory());
    app.db.insert_task(&original)?;
    let media = crate::model::Media {
        id: uuid::Uuid::new_v4().to_string(),
        task_id: original.id.clone(),
        title: original.title.clone(),
        filename: "compression source.mkv".into(),
        size: original_hash.1,
        duration: 8.0,
        path: source.clone(),
        assets: vec![],
        media_info: None,
    };
    app.db.complete(&original, &media)?;
    let original_record = serde_json::to_value(app.db.task(&original.id)?)?;
    let target = original_hash.1 * 65 / 100;
    let preview = app.command("tasks.convertPreview", json!({"id":original.id,"compression":{"targetBytes":target},"requestId":"compression-acceptance"})).await?;
    ensure!(
        preview["source"]["sizeBytes"] == original_hash.1,
        "Preview used a stale source size"
    );
    ensure!(
        preview["blockingReasons"]
            .as_array()
            .is_some_and(Vec::is_empty),
        "Compression preview blocked: {}",
        preview["blockingReasons"]
    );
    ensure!(
        app.command(
            "tasks.convert",
            json!({"id":original.id,"compression":{"targetBytes":1}})
        )
        .await
        .is_err(),
        "An impossible target was accepted"
    );
    let queued = app
        .command(
            "tasks.convert",
            json!({"id":original.id,"compression":{"targetBytes":target}}),
        )
        .await?;
    let id = queued["id"]
        .as_str()
        .context("Compression did not create a task")?
        .to_string();
    ensure!(id != original.id, "Compression replaced the original task");
    wait_for(&app, std::slice::from_ref(&id), &["completed"]).await?;
    let compressed = app
        .db
        .media_for_task(&id)?
        .context("Compressed media is absent")?;
    ensure!(
        compressed.path != source
            && compressed
                .path
                .extension()
                .is_some_and(|value| value == "mkv"),
        "Compression did not publish a separate MKV"
    );
    ensure!(
        compressed.size > 0 && compressed.size < original_hash.1,
        "Compression did not reduce the local fixture size"
    );
    ensure!(
        (compressed.duration - 8.0).abs() < 0.2,
        "Compression truncated the source duration"
    );
    let inspect = |path: &Path| {
        [
            "-v",
            "error",
            "-show_streams",
            "-show_chapters",
            "-of",
            "json",
            path.to_str().unwrap(),
        ]
        .into_iter()
        .map(str::to_string)
        .collect::<Vec<_>>()
    };
    let output = crate::process::run(
        &app.tool("ffprobe")?,
        &inspect(&compressed.path),
        None,
        &token,
        None,
    )
    .await?;
    let probe: serde_json::Value = serde_json::from_str(&output.stdout)?;
    let streams = probe["streams"]
        .as_array()
        .context("Output has no streams")?;
    ensure!(
        streams
            .iter()
            .filter(|stream| stream["codec_type"] == "audio")
            .count()
            == 2,
        "An audio track was dropped"
    );
    ensure!(
        streams
            .iter()
            .filter(|stream| stream["codec_type"] == "subtitle")
            .count()
            == 1,
        "Subtitles were dropped"
    );
    ensure!(
        streams
            .iter()
            .filter(|stream| stream["codec_type"] == "attachment")
            .count()
            == 1,
        "Attachment was dropped"
    );
    ensure!(
        probe["chapters"]
            .as_array()
            .is_some_and(|chapters| chapters.len() == 2),
        "Chapters were dropped"
    );
    ensure!(
        streams
            .iter()
            .any(|stream| stream["codec_type"] == "audio" && stream["tags"]["language"] == "jpn"),
        "Audio language was dropped"
    );
    ensure!(
        streams.iter().any(
            |stream| stream["codec_type"] == "subtitle" && stream["disposition"]["forced"] == 1
        ),
        "Subtitle disposition was dropped"
    );
    let advanced = app.command("tasks.convert", json!({"id":original.id,"compression":{"videoBitrateBps":300_000,"maxWidth":320,"maxHeight":180,"fps":"24000/1001"}})).await?;
    let advanced_id = advanced["id"].as_str().unwrap().to_string();
    wait_for(&app, std::slice::from_ref(&advanced_id), &["completed"]).await?;
    let task = app.db.task(&advanced_id)?;
    let info = task
        .media_info
        .context("Advanced output has no media info")?;
    ensure!(
        info.width == Some(320) && info.height == Some(180),
        "Advanced compression ignored downscaling"
    );
    ensure!(
        info.fps
            .is_some_and(|fps| (fps - 24000.0 / 1001.0).abs() < 0.01),
        "Advanced compression ignored the fractional frame rate"
    );
    let nvenc = preview["capabilities"].as_array().and_then(|capabilities| {
        capabilities
            .iter()
            .find(|capability| capability["encoder"] == "hevc_nvenc")
    });
    let mut expected_media = 3;
    if nvenc.is_some_and(|capability| capability["available"] == true) {
        let queued = app.command("tasks.convert", json!({"id":original.id,"compression":{"targetBytes":target,"encoder":"hevc_nvenc"}})).await?;
        let gpu_id = queued["id"].as_str().unwrap().to_string();
        wait_for(&app, std::slice::from_ref(&gpu_id), &["completed"]).await?;
        let gpu = app.db.media_for_task(&gpu_id)?.unwrap();
        ensure!(
            gpu.size > 0 && gpu.size < original_hash.1 && (gpu.duration - 8.0).abs() < 0.2,
            "GPU compression did not reduce size or truncated content"
        );
        expected_media += 1;
        eprintln!(
            "NVENC full compression verified: {} bytes, approximate target {target} bytes",
            gpu.size
        );
    } else {
        ensure!(
            nvenc.is_none_or(|capability| capability["reason"]
                .as_str()
                .is_some_and(|reason| !reason.is_empty())),
            "An unavailable GPU had no explanation"
        );
        eprintln!("NVENC unavailable on this test host: {nvenc:?}");
    }
    ensure!(
        download::hash_file(&source)? == original_hash,
        "Compression changed its source"
    );
    ensure!(
        serde_json::to_value(app.db.task(&original.id)?)? == original_record,
        "Compression altered the original task record"
    );
    ensure!(
        app.db.media()?.len() == expected_media,
        "Compression catalog did not retain independent results"
    );
    app.shutdown().await?;
    Ok(())
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
#[ignore = "requires installed FFmpeg/FFprobe with libx265; local fixture only"]
async fn real_compression_preserves_variable_frame_timestamps() -> Result<()> {
    let root = tempfile::tempdir()?;
    let app = App::open(config(root.path())).await?;
    let token = CancellationToken::new();
    let source = app.download_directory().join("variable-frame-rate.mkv");
    let args = [
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x180:rate=30",
        "-frames:v",
        "60",
        "-vf",
        "setpts=if(lt(N\\,30)\\,N/(30*TB)\\,(1+(N-30)/15)/TB)",
        "-fps_mode",
        "vfr",
        "-threads",
        "2",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-color_primaries",
        "bt709",
        "-color_trc",
        "bt709",
        "-colorspace",
        "bt709",
    ]
    .into_iter()
    .map(str::to_string)
    .chain([source.to_string_lossy().into_owned()])
    .collect::<Vec<_>>();
    crate::process::run(&app.tool("ffmpeg")?, &args, None, &token, None).await?;
    let original_hash = download::hash_file(&source)?;
    let mut original = crate::tests::paused_task(app.freeze_tools().await?);
    original.stage("completed", "completed", Some(100.0));
    original.title = "Variable frame rate fixture".into();
    original.output_path = Some(source.clone());
    original.download_dir = Some(app.download_directory());
    app.db.insert_task(&original)?;
    app.db.complete(
        &original,
        &crate::model::Media {
            id: uuid::Uuid::new_v4().to_string(),
            task_id: original.id.clone(),
            title: original.title.clone(),
            filename: "variable-frame-rate.mkv".into(),
            size: original_hash.1,
            duration: 3.0,
            path: source.clone(),
            assets: vec![],
            media_info: None,
        },
    )?;
    let queued = app
        .command(
            "tasks.convert",
            json!({"id":original.id,"compression":{"videoBitrateBps":200_000}}),
        )
        .await?;
    let id = queued["id"].as_str().unwrap().to_string();
    wait_for(&app, std::slice::from_ref(&id), &["completed"]).await?;
    let output = app
        .db
        .media_for_task(&id)?
        .context("VFR compression did not publish media")?;
    let mut timestamps = Vec::new();
    for path in [&source, &output.path] {
        let args = [
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_frames",
            "-show_entries",
            "frame=best_effort_timestamp_time",
            "-of",
            "json",
        ]
        .into_iter()
        .map(str::to_string)
        .chain([path.to_string_lossy().into_owned()])
        .collect::<Vec<_>>();
        let probe = crate::process::run(&app.tool("ffprobe")?, &args, None, &token, None).await?;
        let probe: serde_json::Value = serde_json::from_str(&probe.stdout)?;
        timestamps.push(
            probe["frames"]
                .as_array()
                .context("VFR frames are missing")?
                .iter()
                .map(|frame| {
                    frame["best_effort_timestamp_time"]
                        .as_str()
                        .context("VFR timestamp missing")?
                        .parse::<f64>()
                        .map_err(Into::into)
                })
                .collect::<Result<Vec<_>>>()?,
        );
    }
    ensure!(
        timestamps[0].len() == 60 && timestamps[1].len() == 60,
        "VFR compression duplicated or dropped frames"
    );
    ensure!(
        (timestamps[0][1] - timestamps[0][0] - 1.0 / 30.0).abs() < 0.002
            && (timestamps[0][59] - timestamps[0][58] - 1.0 / 15.0).abs() < 0.002,
        "Fixture is not variable frame rate"
    );
    ensure!(
        timestamps[0]
            .iter()
            .zip(&timestamps[1])
            .all(|(before, after)| (before - after).abs() < 0.002),
        "Compression changed VFR presentation times"
    );
    ensure!(
        download::hash_file(&source)? == original_hash,
        "VFR compression changed its source"
    );
    app.shutdown().await?;
    Ok(())
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
#[ignore = "requires installed FFmpeg/FFprobe with libx265; local fixture only"]
async fn real_compression_preserves_hdr10_and_hlg() -> Result<()> {
    let root = tempfile::tempdir()?;
    let app = App::open(config(root.path())).await?;
    let token = CancellationToken::new();
    let frozen = app.freeze_tools().await?;
    let inventory = crate::process::run_limited_text(
        &app.tool("ffmpeg")?,
        &["-hide_banner".into(), "-h".into(), "encoder=libx265".into()],
        &token,
        1024 * 1024,
        Duration::from_secs(10),
    )
    .await?;
    let cpu_10bit = inventory
        .stdout
        .lines()
        .filter_map(|line| line.trim().strip_prefix("Supported pixel formats:"))
        .flat_map(str::split_whitespace)
        .any(|format| format == "yuv420p10le");
    for (name, transfer, parameters) in [
        (
            "HDR10",
            "smpte2084",
            "pools=1:frame-threads=1:log-level=error:colorprim=bt2020:transfer=16:colormatrix=bt2020nc:master-display=G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1):max-cll=1000,400",
        ),
        (
            "HLG",
            "arib-std-b67",
            "pools=1:frame-threads=1:log-level=error:colorprim=bt2020:transfer=18:colormatrix=bt2020nc",
        ),
    ] {
        let path = app.download_directory().join(format!("{name}.mkv"));
        let filter = format!(
            "scale=iw:ih,format=yuv420p10le,setparams=color_primaries=bt2020:color_trc={transfer}:colorspace=bt2020nc:range=tv"
        );
        let mut args = [
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=24",
            "-t",
            "2",
            "-vf",
            &filter,
            "-pix_fmt",
            "+yuv420p10le",
            "-c:v",
            if cpu_10bit { "libx265" } else { "ffv1" },
        ]
        .into_iter()
        .map(str::to_string)
        .collect::<Vec<_>>();
        if cpu_10bit {
            args.extend([
                "-preset".into(),
                "ultrafast".into(),
                "-x265-params".into(),
                parameters.into(),
            ]);
        }
        args.extend(
            [
                "-color_primaries",
                "bt2020",
                "-color_trc",
                transfer,
                "-colorspace",
                "bt2020nc",
                "-color_range",
                "tv",
                path.to_str().unwrap(),
            ]
            .into_iter()
            .map(str::to_string)
            .collect::<Vec<_>>(),
        );
        crate::process::run(&app.tool("ffmpeg")?, &args, None, &token, None).await?;
        let before = download::hash_file(&path)?;
        let mut task = crate::tests::paused_task(frozen.clone());
        task.title = name.into();
        task.status = "completed".into();
        task.output_path = Some(path.clone());
        task.download_dir = Some(app.download_directory());
        app.db.insert_task(&task)?;
        app.db.complete(
            &task,
            &crate::model::Media {
                id: uuid::Uuid::new_v4().to_string(),
                task_id: task.id.clone(),
                title: name.into(),
                filename: format!("{name}.mkv"),
                size: before.1,
                duration: 2.0,
                path: path.clone(),
                assets: vec![],
                media_info: None,
            },
        )?;
        let original_record = serde_json::to_value(app.db.task(&task.id)?)?;
        let preview = app
            .command(
                "tasks.convertPreview",
                json!({"id":task.id,"compression":{}}),
            )
            .await?;
        ensure!(
            preview["source"]["bitDepth"] == 10,
            "{name} source depth was misidentified"
        );
        ensure!(
            preview["source"]["hdr"] == if name == "HDR10" { "hdr10" } else { "hlg" },
            "{name} source HDR was misidentified: {}",
            preview["source"]
        );
        if !cpu_10bit {
            let cpu = preview["capabilities"]
                .as_array()
                .into_iter()
                .flatten()
                .find(|capability| capability["encoder"] == "libx265")
                .context("CPU capability is missing")?;
            ensure!(
                cpu["available"] == false
                    && cpu["reason"]
                        .as_str()
                        .is_some_and(|reason| !reason.is_empty()),
                "An 8-bit CPU encoder was allowed to reduce {name} bit depth"
            );
            let queued = app
                .command(
                    "tasks.convert",
                    json!({"id":task.id,"compression":{"videoBitrateBps":200_000}}),
                )
                .await?;
            let rejected_id = queued["id"]
                .as_str()
                .context("Queued HDR task has no ID")?
                .to_string();
            wait_for(
                &app,
                std::slice::from_ref(&rejected_id),
                &["processing_failed"],
            )
            .await?;
            ensure!(
                app.db.media_for_task(&rejected_id)?.is_none(),
                "Unsupported HDR compression published an output"
            );
            ensure!(
                serde_json::to_value(app.db.task(&task.id)?)? == original_record
                    && download::hash_file(&path)? == before,
                "Rejected HDR compression changed the original"
            );
            eprintln!(
                "{name}: true 10-bit FFV1 source was preserved; this CPU build correctly refuses 10-bit HEVC"
            );
            continue;
        }
        let queued = app
            .command(
                "tasks.convert",
                json!({"id":task.id,"compression":{"videoBitrateBps":200_000}}),
            )
            .await?;
        let id = queued["id"].as_str().unwrap().to_string();
        wait_for(&app, std::slice::from_ref(&id), &["completed"]).await?;
        let output = app.db.media_for_task(&id)?.unwrap();
        let args = [
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_streams",
            "-show_frames",
            "-read_intervals",
            "%+#2",
            "-of",
            "json",
            output.path.to_str().unwrap(),
        ]
        .into_iter()
        .map(str::to_string)
        .collect::<Vec<_>>();
        let probe = crate::process::run(&app.tool("ffprobe")?, &args, None, &token, None).await?;
        let probe: serde_json::Value = serde_json::from_str(&probe.stdout)?;
        ensure!(
            probe["streams"][0]["pix_fmt"] == "yuv420p10le"
                && probe["streams"][0]["color_transfer"] == transfer,
            "{name} lost its bit depth or HDR transfer"
        );
        if name == "HDR10" {
            let content = probe.to_string();
            ensure!(
                content.contains("Mastering display metadata")
                    && content.contains("Content light level metadata"),
                "HDR10 static metadata was dropped"
            );
        }
        ensure!(
            download::hash_file(&path)? == before,
            "{name} source changed"
        );
    }
    app.shutdown().await?;
    Ok(())
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
#[ignore = "requires installed native yt-dlp, FFmpeg and FFprobe; local fixture only"]
async fn real_transfer_uses_resolved_metadata_without_reextracting_webpage() -> Result<()> {
    let root = tempfile::tempdir()?;
    let fixtures = root.path().join("fixtures");
    std::fs::create_dir_all(&fixtures)?;
    // Transfer stores the source unchanged; this test deliberately does not process or probe it.
    let input = fixtures.join("resolved.mp4");
    let payload = (0u8..=255).cycle().take(1024).collect::<Vec<_>>();
    std::fs::write(&input, &payload)?;
    let expected = download::hash_file(&input)?;
    let app = App::open(config(root.path())).await?;
    let server = LocalServer::start(fixtures).await?;
    let outcome = async {
        let webpage = format!("{}/must-not-reextract.html", server.url);
        let mut task = crate::tests::paused_task(crate::tools::freeze(&app)?);
        task.url = webpage.clone();
        task.title = "Resolved metadata fixture".into();
        task.options.write_thumbnail = false;
        task.options.write_info_json = false;
        task.options.format_id = "resolved".into();
        app.db.insert_task(&task)?;
        let info = json!({
            "id": "resolved-fixture",
            "title": task.title,
            "extractor": "generic",
            "extractor_key": "Generic",
            "webpage_url": webpage,
            "original_url": task.url,
            "formats": [{
                "format_id": "resolved",
                "url": format!("{}/resolved.mp4", server.url),
                "ext": "mp4",
                "protocol": "http",
                "vcodec": "h264",
                "acodec": "aac",
                "filesize": payload.len()
            }]
        });
        let manifest = tokio::time::timeout(
            Duration::from_secs(30),
            download::transfer(&app, task, info, &CancellationToken::new()),
        )
        .await
        .context("Resolved metadata transfer did not finish")??;
        ensure!(
            manifest.files.len() == 1,
            "Transfer returned unexpected files"
        );
        let source = &manifest.files[0];
        ensure!(
            source.size > 0 && (source.hash.clone(), source.size) == expected,
            "Transfer did not preserve the resolved media bytes"
        );
        ensure!(
            download::hash_file(&source.path)? == expected,
            "Downloaded source content differs from the local fixture"
        );
        let requests = server.requests.lock().unwrap();
        ensure!(
            requests.iter().any(|path| path == "/resolved.mp4"),
            "Transfer did not request the resolved media URL"
        );
        ensure!(
            !requests
                .iter()
                .any(|path| path == "/must-not-reextract.html"),
            "Transfer re-extracted the original webpage instead of using resolved metadata"
        );
        drop(requests);
        ensure!(
            std::fs::read_dir(&app.config.data_dir)?.all(|entry| {
                entry.is_ok_and(|entry| {
                    !entry
                        .file_name()
                        .to_string_lossy()
                        .starts_with(".download-info-")
                })
            }),
            "Transfer left its temporary resolved metadata file behind"
        );
        Ok::<_, anyhow::Error>(())
    }
    .await;
    server.shutdown().await;
    app.shutdown().await?;
    outcome
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
#[ignore = "requires native yt-dlp; interrupted local HTTP fixture only"]
async fn real_transfer_retries_disconnected_http_and_resumes_partial_bytes() -> Result<()> {
    let root = tempfile::tempdir()?;
    let app = App::open(config(root.path())).await?;
    let listener = TcpListener::bind("127.0.0.1:0").await?;
    let url = format!("http://{}/interrupted.mp4", listener.local_addr()?);
    let payload = Arc::new(
        (0u8..=255)
            .cycle()
            .take(2 * 1024 * 1024)
            .collect::<Vec<_>>(),
    );
    let ranges = Arc::new(Mutex::new(Vec::new()));
    let stop = CancellationToken::new();
    let (body, requested, shutdown) = (payload.clone(), ranges.clone(), stop.clone());
    let server = tokio::spawn(async move {
        loop {
            let accepted = tokio::select! { accepted=listener.accept()=>accepted, _=shutdown.cancelled()=>break };
            let Ok((mut socket, _)) = accepted else { break };
            let mut bytes = [0; 16384];
            let Ok(Ok(size)) =
                tokio::time::timeout(Duration::from_secs(5), socket.read(&mut bytes)).await
            else {
                continue;
            };
            let request = String::from_utf8_lossy(&bytes[..size]);
            if !request.starts_with("GET /interrupted.mp4 ") {
                let _ = socket
                    .write_all(
                        b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
                    )
                    .await;
                continue;
            }
            let start = request
                .lines()
                .find_map(|line| {
                    let (name, value) = line.split_once(':')?;
                    name.eq_ignore_ascii_case("range")
                        .then_some(value.trim())?
                        .strip_prefix("bytes=")?
                        .split_once('-')?
                        .0
                        .parse::<usize>()
                        .ok()
                })
                .unwrap_or(0);
            let attempt = {
                let mut ranges = requested.lock().unwrap();
                ranges.push(start);
                ranges.len()
            };
            let header = if start > 0 {
                format!(
                    "HTTP/1.1 206 Partial Content\r\nContent-Range: bytes {start}-{}/{}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                    body.len() - 1,
                    body.len(),
                    body.len() - start
                )
            } else {
                format!(
                    "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                    body.len()
                )
            };
            let end = if attempt <= 6 {
                (start + 64 * 1024).min(body.len())
            } else {
                body.len()
            };
            let _ = socket.write_all(header.as_bytes()).await;
            let _ = socket.write_all(&body[start..end]).await;
            let _ = socket.shutdown().await;
        }
    });
    let outcome = async {
        let mut task = crate::tests::paused_task(crate::tools::freeze(&app)?);
        task.title = "Interrupted HTTP fixture".into();
        task.options.write_thumbnail = false;
        task.options.write_info_json = false;
        task.options.format_id = "resolved".into();
        app.db.insert_task(&task)?;
        let info = json!({"id":"interrupted","title":task.title,"extractor":"generic","extractor_key":"Generic","webpage_url":task.url,"formats":[{"format_id":"resolved","url":url,"ext":"mp4","protocol":"http","vcodec":"h264","acodec":"aac","filesize":payload.len()}]});
        let manifest = tokio::time::timeout(Duration::from_secs(90), download::transfer(&app, task.clone(), info, &CancellationToken::new())).await??;
        ensure!(manifest.files.len() == 1);
        ensure!(std::fs::read(&manifest.files[0].path)? == *payload, "Resumed bytes differ from the fixture");
        let ranges = ranges.lock().unwrap();
        ensure!(ranges.len() == 7, "Unexpected HTTP attempt count: {ranges:?}");
        ensure!(ranges[0] == 0 && ranges.windows(2).all(|pair| pair[1] > pair[0]), "Partial bytes were not resumed: {ranges:?}");
        drop(ranges);
        ensure!(app.db.task_logs(&task.id)?.iter().any(|entry| entry.message.contains("Temporary stream download failure; retry 1/2")), "No application retry was recorded");
        Ok::<_, anyhow::Error>(())
    }.await;
    stop.cancel();
    server.await?;
    app.shutdown().await?;
    outcome
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
#[ignore = "requires native FFmpeg and FFprobe; uses only a local generated live fixture"]
async fn real_live_stop_save_publishes_valid_recording_and_cancel_does_not() -> Result<()> {
    let root = tempfile::tempdir()?;
    let app = App::open(config(root.path())).await?;
    for save in [true, false] {
        let mut task = crate::tests::paused_task(app.freeze_tools().await?);
        task.title = if save {
            "Recorded live"
        } else {
            "Cancelled live"
        }
        .into();
        task.live = true;
        task.stage("downloading", "downloading_live", None);
        app.db.insert_task(&task)?;
        let directory = app
            .config
            .data_dir
            .join("tasks")
            .join(&task.id)
            .join("sources/live-fixture");
        std::fs::create_dir_all(&directory)?;
        let path = directory.join("recording.ts.part");
        let args = [
            "-hide_banner",
            "-loglevel",
            "error",
            "-re",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=12",
            "-re",
            "-f",
            "lavfi",
            "-i",
            "sine=sample_rate=44100",
            "-threads",
            "1",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            "-f",
            "mpegts",
        ]
        .into_iter()
        .map(str::to_string)
        .chain([path.to_string_lossy().into_owned()])
        .collect::<Vec<_>>();
        let cancel = CancellationToken::new();
        let finish_capture = CancellationToken::new();
        let runner_cancel = cancel.clone();
        let runner_finish = finish_capture.clone();
        let runner_app = app.clone();
        let mut runner_task = task.clone();
        let runner_path = path.clone();
        let handle = tokio::spawn(async move {
            let captured = crate::process::run_capture(
                &runner_app.task_tool(&runner_task, "ffmpeg").await?,
                &args,
                &directory,
                &runner_cancel,
                &runner_finish,
                None,
                runner_app.raw_task_log(&runner_task.id)?,
            )
            .await;
            if runner_cancel.is_cancelled() {
                ensure!(
                    captured.is_err(),
                    "Ordinary cancel was treated as a successful recording"
                );
                return Ok::<_, anyhow::Error>(());
            }
            captured?;
            let (hash, size) = download::hash_file(&runner_path)?;
            runner_task.manifest = Some(SourceManifest {
                version: 1,
                directory,
                files: vec![SourceFile {
                    path: runner_path,
                    hash,
                    size,
                    kind: "video".into(),
                    format_id: "fixture".into(),
                    language: None,
                }],
                metadata: json!({"id":"local-live","title":runner_task.title,"is_live":true}),
                duration: None,
                chapters: vec![],
                sponsor_segments: vec![],
                output_extension: "mp4".into(),
            });
            runner_task.stage("waiting_processing", "waiting_processing", None);
            runner_app.save_task(&runner_task)?;
            crate::processing::process_task(&runner_app, &runner_task.id, &runner_cancel).await
        });
        // Runtime owns the real capture future, matching the public task action's cancellation boundary.
        let completion = Arc::new(Mutex::new(None));
        let result_slot = completion.clone();
        let supervisor = tokio::spawn(async move {
            *result_slot.lock().unwrap() = Some(handle.await);
        });
        app.runners.lock().unwrap().insert(
            task.id.clone(),
            crate::Runtime {
                generation: 0,
                cancel,
                finish_capture,
                handle: supervisor,
            },
        );
        tokio::time::timeout(Duration::from_secs(15), async {
            while std::fs::metadata(&path).map_or(0, |m| m.len()) < 64 * 1024 {
                tokio::time::sleep(Duration::from_millis(50)).await;
            }
        })
        .await
        .context("Local live fixture did not start")?;
        if save {
            app.command("tasks.finishLive", json!({"id":task.id}))
                .await?;
            let saved = wait_for(&app, &[task.id.clone()], &["completed"]).await?;
            let media = app
                .db
                .media_for_task(&task.id)?
                .context("Stopped recording was not published")?;
            ensure!(
                media.duration > 0.0
                    && media.size > 0
                    && media.path.extension().is_some_and(|ext| ext == "mp4"),
                "Stopped recording did not become a valid MP4"
            );
            ensure!(saved[0].live, "Live marker was lost");
            app.stop_task(&task.id, "paused").await?;
        } else {
            app.command("tasks.cancel", json!({"id":task.id})).await?;
            ensure!(
                app.db.media_for_task(&task.id)?.is_none(),
                "Cancelled capture published media"
            );
            ensure!(
                path.is_file(),
                "Cancelled capture lost its recoverable source"
            );
        }
        let result = completion
            .lock()
            .unwrap()
            .take()
            .context("Capture supervisor did not return")?;
        result??;
    }
    app.shutdown().await?;
    Ok(())
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
#[ignore = "requires native yt-dlp, FFmpeg and FFprobe; local fixture only"]
async fn real_filename_conflict_confirmation_rename_and_successful_cache_cleanup() -> Result<()> {
    let root = tempfile::tempdir()?;
    let fixtures = root.path().join("fixtures");
    std::fs::create_dir_all(&fixtures)?;
    let app = App::open(config(root.path())).await?;
    let source = fixtures.join("clip.mp4");
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
        "-t",
        "1",
        "-threads",
        "1",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
    ]
    .into_iter()
    .map(str::to_string)
    .chain([source.to_string_lossy().into_owned()])
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
    let options = json!({"container":"mp4","encoder":"copy","writeThumbnail":false,"writeInfoJson":false,"subtitleLanguage":"none","organizeTaskFolder":false});
    let first: Vec<String> = serde_json::from_value(
        app.command(
            "tasks.add",
            json!({"urls":[format!("{}/clip.mp4?source=one",server.url)],"options":options}),
        )
        .await?,
    )?;
    wait_for(&app, &first, &["completed"]).await?;
    let original = app
        .db
        .media_for_task(&first[0])?
        .context("First result missing")?;
    let old_hash = download::hash_file(&original.path)?;
    ensure!(
        !app.config
            .data_dir
            .join("tasks")
            .join(&first[0])
            .join("sources")
            .exists(),
        "Successful task kept its source cache"
    );
    let second: Vec<String> = serde_json::from_value(
        app.command(
            "tasks.add",
            json!({"urls":[format!("{}/clip.mp4?source=two",server.url)],"options":options}),
        )
        .await?,
    )?;
    let conflict = wait_for(&app, &second, &["failed"]).await?.remove(0);
    ensure!(
        conflict
            .destination
            .as_ref()
            .and_then(|value| value.public_conflict())
            .is_some(),
        "Same-name source did not ask before transfer"
    );
    ensure!(
        download::hash_file(&original.path)? == old_hash,
        "Unconfirmed conflict changed the original"
    );
    let sources = app
        .config
        .data_dir
        .join("tasks")
        .join(&second[0])
        .join("sources");
    let pending = std::fs::read_dir(&sources)?.collect::<std::io::Result<Vec<_>>>()?;
    ensure!(
        pending.len() == 1
            && pending[0].file_name() == "download-checkpoint.json"
            && pending[0].file_type()?.is_file(),
        "Conflict downloaded media before confirmation instead of only saving metadata"
    );
    app.command(
        "tasks.destination",
        json!({"id":second[0],"choice":"overwrite","confirmed":true}),
    )
    .await?;
    wait_for(&app, &second, &["completed"]).await?;
    ensure!(
        app.db.media_for_task(&first[0])?.is_none()
            && app.db.task(&first[0])?.output_path.is_none(),
        "Old owner still claims the replacement"
    );
    ensure!(
        app.db.media_for_task(&second[0])?.unwrap().path == original.path,
        "Replacement changed the chosen filename"
    );
    let third: Vec<String> = serde_json::from_value(
        app.command(
            "tasks.add",
            json!({"urls":[format!("{}/clip.mp4?source=three",server.url)],"options":options}),
        )
        .await?,
    )?;
    wait_for(&app, &third, &["failed"]).await?;
    app.command(
        "tasks.destination",
        json!({"id":third[0],"choice":"rename","filename":"clip (2).mp4"}),
    )
    .await?;
    wait_for(&app, &third, &["completed"]).await?;
    ensure!(
        app.db.media()?.len() == 2
            && original.path.exists()
            && app.db.media_for_task(&third[0])?.unwrap().filename == "clip (2).mp4",
        "Rename failed to preserve both results"
    );
    app.command("tasks.delete", json!({"id":first[0],"deleteFiles":true}))
        .await?;
    ensure!(
        original.path.exists(),
        "Deleting replaced owner removed the new result"
    );
    ensure!(
        crate::storage::status(&app)?["completedCacheBytes"] == 0,
        "Completed caches were not cleaned"
    );
    app.shutdown().await?;
    server.shutdown().await;
    Ok(())
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
    let processor = app.processor.clone().acquire_many_owned(2).await?;
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
    let processor = app.processor.clone().acquire_many_owned(2).await?;
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
    let source_seed = tempfile::tempdir_in(root.path())?;
    for source in &manifest.files {
        std::fs::copy(
            &source.path,
            source_seed.path().join(source.path.file_name().unwrap()),
        )?;
    }
    let task = Task {
        kind: "media".into(),
        files: vec![],
        id: id.clone(),
        url: "https://example.test/offline".into(),
        title: "Option sample".into(),
        thumbnail: None,
        size: None,
        transfer_bytes: vec![],
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
        completed_at: None,
        options,
        download_dir: Some(app.download_directory()),
        output_path: None,
        previous_outputs: vec![],
        cookie_snapshot: None,
        proxy_snapshot: None,
        manifest: Some(manifest),
        publication: None,
        destination: None,
        priority: 0,
        live: false,
        conversion: false,
        compression: None,
        tools: app.freeze_tools().await?,
    };
    app.db.insert_task(&task)?;
    app.schedule(&id)?;
    let complete = wait_for(&app, std::slice::from_ref(&id), &["completed"])
        .await?
        .remove(0);
    let media = app.db.media_for_task(&id)?.unwrap();
    ensure!(
        !task.manifest.as_ref().unwrap().directory.exists(),
        "Completed media kept its download source cache"
    );
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
        std::fs::copy(
            source_seed.path().join(source.path.file_name().unwrap()),
            &destination,
        )?;
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
        download::public_probe(
            &app,
            &url,
            &DownloadOptions::default(),
            None,
            &app.shutdown_token
        )
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
        let processor = app.processor.clone().acquire_many_owned(2).await?;
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
        let progress = app.db.task(id)?.public();
        let source_size = std::fs::metadata(&input)?.len();
        ensure!(
            progress["downloadedBytes"] == source_size
                && progress["totalBytes"] == source_size
                && progress["totalBytesEstimated"] == false
                && progress["size"].is_null(),
            "Source transfer sizes did not reach the task before processing: {progress}"
        );
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

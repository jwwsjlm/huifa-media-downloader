use crate::{
    App, DownloadOptions,
    model::{Chapter, SourceFile, SourceManifest, Task, text},
    process::{self, LineCallback},
};
use anyhow::{Context, Result, bail, ensure};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    cmp::Ordering,
    io::{Read, Write},
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};
use tokio_util::sync::CancellationToken;

struct Cookies {
    file: Option<tempfile::NamedTempFile>,
}
impl Cookies {
    fn new(app: &App, text: Option<&str>) -> Result<Self> {
        let file = if let Some(text) = text {
            let mut file = tempfile::Builder::new()
                .prefix(".cookie-")
                .tempfile_in(&app.config.data_dir)?;
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                file.as_file()
                    .set_permissions(std::fs::Permissions::from_mode(0o600))?;
            }
            file.write_all(text.as_bytes())?;
            file.as_file().sync_all()?;
            Some(file)
        } else {
            None
        };
        Ok(Self { file })
    }
    fn apply(&self, args: &mut Vec<String>) {
        if let Some(file) = &self.file {
            args.extend([
                "--cookies".into(),
                file.path().to_string_lossy().to_string(),
            ]);
        }
    }
}

fn base_args(
    app: &App,
    options: &DownloadOptions,
    cookies: &Cookies,
    task: Option<&Task>,
) -> Result<Vec<String>> {
    let mut args = vec![
        "--ignore-config".into(),
        "--no-js-runtimes".into(),
        "--no-warnings".into(),
        "--no-cache-dir".into(),
        "--socket-timeout".into(),
        "30".into(),
        "--retries".into(),
        "5".into(),
        "--fragment-retries".into(),
        "10".into(),
        "--extractor-retries".into(),
        "3".into(),
    ];
    cookies.apply(&mut args);
    if !options.proxy.is_empty() {
        args.extend(["--proxy".into(), options.proxy.clone()]);
    }
    let deno = match task {
        Some(task) if task.tools.contains_key("deno") => Some(app.task_tool(task, "deno")?),
        Some(_) => None,
        None => app.tool("deno").ok(),
    };
    if let Some(deno) = deno {
        args.extend(["--js-runtimes".into(), format!("deno:{}", deno.display())]);
    }
    if options.request_delay > 0.0 {
        args.extend(["--sleep-requests".into(), options.request_delay.to_string()]);
    }
    Ok(args)
}

pub(crate) async fn probe(
    app: &App,
    url: &str,
    options: &DownloadOptions,
    cookies: Option<&str>,
    cancel: &CancellationToken,
    task: Option<&Task>,
) -> Result<Value> {
    let cookies = Cookies::new(app, cookies)?;
    let mut args = base_args(app, options, &cookies, task)?;
    args.extend([
        "--dump-single-json".into(),
        "--skip-download".into(),
        "--no-playlist".into(),
    ]);
    if options.write_comments {
        args.push("--write-comments".into());
    }
    if options.live_from_start {
        args.push("--live-from-start".into());
    }
    if options.wait_for_live {
        args.extend([
            "--wait-for-video".into(),
            format!("{}-{}", options.wait_min, options.wait_max),
        ]);
    }
    args.extend(["--".into(), url.into()]);
    let executable = if let Some(task) = task {
        app.task_tool(task, "yt-dlp")?
    } else {
        app.tool("yt-dlp")?
    };
    let output = process::run(&executable, &args, None, cancel, None).await?;
    let info: Value =
        serde_json::from_str(&output.stdout).context("yt-dlp returned invalid video metadata")?;
    ensure!(
        info.is_object() && text(&info, "_type") != "playlist",
        "A single video URL is required; use collection selection first"
    );
    ensure!(
        info["formats"].as_array().is_some_and(|v| !v.is_empty()) || info["url"].is_string(),
        "No downloadable formats were found"
    );
    Ok(info)
}

pub(crate) async fn public_probe(
    app: &App,
    url: &str,
    options: &DownloadOptions,
    cookies: Option<&str>,
) -> Result<Value> {
    let permit = app.network.acquire().await?;
    let info = probe(app, url, options, cookies, &app.shutdown_token, None).await?;
    drop(permit);
    let formats=all_formats(&info).iter().map(|f|json!({"formatId":f["format_id"],"ext":f["ext"],"width":f["width"],"height":f["height"],"fps":f["fps"],"vcodec":f["vcodec"],"acodec":f["acodec"],"language":f["language"],"filesize":f.get("filesize").or(f.get("filesize_approx")),"formatNote":f["format_note"]})).collect::<Vec<_>>();
    let subtitles = info["subtitles"]
        .as_object()
        .into_iter()
        .flat_map(|s| s.keys())
        .chain(
            info["automatic_captions"]
                .as_object()
                .into_iter()
                .flat_map(|s| s.keys()),
        )
        .cloned()
        .collect::<std::collections::BTreeSet<_>>();
    Ok(
        json!({"id":info["id"],"title":info["title"],"duration":info["duration"],"isLive":info["is_live"].as_bool().unwrap_or(false),"thumbnail":safe_thumbnail(&info),"formats":formats,"subtitles":subtitles,"chapters":chapters(&info)}),
    )
}

pub(crate) async fn collection(
    app: &App,
    url: &str,
    options: &DownloadOptions,
    cookie_text: Option<&str>,
) -> Result<Value> {
    let _permit = app.network.acquire().await?;
    let cookies = Cookies::new(app, cookie_text)?;
    let mut args = base_args(app, options, &cookies, None)?;
    args.extend([
        "--dump-single-json".into(),
        "--flat-playlist".into(),
        "--skip-download".into(),
    ]);
    if options.first_n > 0 {
        args.extend(["--playlist-end".into(), options.first_n.to_string()]);
    }
    if !options.playlist_items.is_empty() {
        args.extend(["--playlist-items".into(), options.playlist_items.clone()]);
    }
    if options.collection_order == "reverse" {
        args.push("--playlist-reverse".into());
    }
    if options.collection_order == "random" {
        args.push("--playlist-random".into());
    }
    args.extend(["--".into(), url.into()]);
    let output = process::run(&app.tool("yt-dlp")?, &args, None, &app.shutdown_token, None).await?;
    let info: Value = serde_json::from_str(&output.stdout)?;
    let all = info["entries"]
        .as_array()
        .cloned()
        .unwrap_or_else(|| vec![info.clone()]);
    let entries=all.iter().filter(|e|options.matches(e)).map(|entry|{
        let url=entry.get("webpage_url").or(entry.get("url")).and_then(Value::as_str).unwrap_or("");
        let url=if url.starts_with("http"){url.to_string()}else if text(entry,"ie_key")=="Youtube" {format!("https://www.youtube.com/watch?v={}",text(entry,"id"))}else{String::new()};
        json!({"id":entry["id"],"url":url,"title":entry["title"],"duration":entry["duration"],"thumbnail":safe_thumbnail(entry),"isLive":entry["is_live"].as_bool().unwrap_or(false)})
    }).filter(|v|!text(v,"url").is_empty()).collect::<Vec<_>>();
    Ok(json!({"id":info["id"],"title":info["title"],"entries":entries}))
}

fn safe_thumbnail(info: &Value) -> Value {
    // Source thumbnails can carry signatures; a public snapshot does not need them.
    let url = text(info, "thumbnail");
    if url::Url::parse(url).is_ok_and(|u| u.query().is_none() && u.username().is_empty()) {
        json!(url)
    } else {
        Value::Null
    }
}

fn all_formats(info: &Value) -> Vec<Value> {
    info["formats"]
        .as_array()
        .cloned()
        .unwrap_or_else(|| vec![info.clone()])
}
fn has_video(format: &Value) -> bool {
    text(format, "vcodec") != "none"
        && (!text(format, "vcodec").is_empty()
            || text(format, "acodec").is_empty()
            || metric(format, "height") > 0.0)
}
fn has_audio(format: &Value) -> bool {
    text(format, "acodec") != "none"
        && (!text(format, "acodec").is_empty() || text(format, "vcodec").is_empty())
}
fn metric(format: &Value, key: &str) -> f64 {
    format[key]
        .as_f64()
        .filter(|v| v.is_finite())
        .unwrap_or(0.0)
}
fn codec_match(codec: &str, preferred: &str) -> bool {
    match preferred {
        "h264" => codec.starts_with("avc") || codec.starts_with("h264"),
        "h265" => {
            codec.starts_with("hevc")
                || codec.starts_with("hvc")
                || codec.starts_with("hev")
                || codec.starts_with("h265")
        }
        "av1" => codec.starts_with("av01") || codec.starts_with("av1"),
        "vp9" => codec.starts_with("vp9") || codec.starts_with("vp09"),
        _ => true,
    }
}
fn vr_match(format: &Value, mode: &str) -> bool {
    let note = text(format, "format_note").to_ascii_lowercase();
    let stereo = note.contains("3d") || note.contains("vr");
    match mode {
        "2d360" => note.contains("360") && !stereo,
        "3d180" => note.contains("180") && stereo,
        "3d360" => note.contains("360") && stereo,
        "none" => !stereo && !note.contains("180") && !note.contains("360"),
        _ => true,
    }
}
fn compare_video(a: &Value, b: &Value, options: &DownloadOptions) -> Ordering {
    let res = metric(a, "height").total_cmp(&metric(b, "height"));
    if res != Ordering::Equal {
        return res;
    }
    let fps = options.video_fps.parse::<f64>().unwrap_or(f64::MAX);
    let score = |f: &Value| {
        let value = metric(f, "fps");
        if value <= fps { value } else { -(value - fps) }
    };
    score(a)
        .total_cmp(&score(b))
        .then(
            codec_match(text(a, "vcodec"), options.effective_codec())
                .cmp(&codec_match(text(b, "vcodec"), options.effective_codec())),
        )
        .then(metric(a, "tbr").total_cmp(&metric(b, "tbr")))
}
fn compare_audio(a: &Value, b: &Value, options: &DownloadOptions) -> Ordering {
    let score = |f: &Value| {
        let language = text(f, "language");
        let note = text(f, "format_note").to_ascii_lowercase();
        if options.audio_track == "original" {
            note.contains("original")
        } else if !matches!(options.audio_track.as_str(), "default" | "all") {
            language.starts_with(&options.audio_track)
        } else {
            metric(f, "language_preference") > 0.0
        }
    };
    score(a)
        .cmp(&score(b))
        .then(metric(a, "quality").total_cmp(&metric(b, "quality")))
        .then(metric(a, "abr").total_cmp(&metric(b, "abr")))
        .then(metric(a, "asr").total_cmp(&metric(b, "asr")))
}

pub(crate) fn select_formats(info: &Value, options: &DownloadOptions) -> Result<Vec<Value>> {
    let formats = all_formats(info);
    let mut selected = Vec::new();
    let exact = |id: &str| -> Result<Value> {
        formats
            .iter()
            .find(|f| text(f, "format_id") == id)
            .cloned()
            .with_context(|| format!("Selected format {id} is no longer available"))
    };
    if !options.format_id.is_empty() {
        selected.push(exact(&options.format_id)?);
    } else if options.content_mode != "audio" {
        if !options.video_format_id.is_empty() {
            let f = exact(&options.video_format_id)?;
            ensure!(has_video(&f), "Selected video format has no video");
            selected.push(f);
        } else {
            let mut videos = formats
                .iter()
                .filter(|f| {
                    has_video(f)
                        && options
                            .max_height()
                            .is_none_or(|h| metric(f, "height") <= h as f64)
                })
                .collect::<Vec<_>>();
            ensure!(
                !videos.is_empty(),
                "No video format satisfies the selected resolution"
            );
            if videos.iter().any(|f| vr_match(f, &options.vr_mode)) {
                videos.retain(|f| vr_match(f, &options.vr_mode));
            }
            let best = videos
                .into_iter()
                .max_by(|a, b| compare_video(a, b, options))
                .unwrap();
            selected.push(best.clone());
        }
    }
    if !options.audio_format_ids.is_empty() {
        for id in &options.audio_format_ids {
            let f = exact(id)?;
            ensure!(
                has_audio(&f) && !has_video(&f),
                "Selected audio format must contain audio only"
            );
            selected.push(f);
        }
    } else if options.audio_track == "all" {
        let mut tracks = std::collections::BTreeMap::<String, Value>::new();
        for format in formats.iter().filter(|f| has_audio(f) && !has_video(f)) {
            let language = if !text(format, "audio_track_id").is_empty() {
                text(format, "audio_track_id").to_string()
            } else if !text(format, "language").is_empty() {
                text(format, "language").to_string()
            } else {
                "default".into()
            };
            if tracks.get(&language).is_none_or(|previous| {
                compare_audio(format, previous, options) == Ordering::Greater
            }) {
                tracks.insert(language, format.clone());
            }
        }
        selected.extend(tracks.into_values());
    } else if selected.is_empty()
        || !selected.iter().any(has_audio)
        || !matches!(options.audio_track.as_str(), "default")
    {
        let audio = formats
            .iter()
            .filter(|f| has_audio(f) && !has_video(f))
            .max_by(|a, b| compare_audio(a, b, options));
        if let Some(audio) = audio {
            selected.push(audio.clone());
        } else if selected.is_empty()
            && let Some(combined) = formats
                .iter()
                .filter(|f| has_audio(f))
                .max_by(|a, b| compare_audio(a, b, options))
        {
            selected.push(combined.clone());
        }
    }
    if options.content_mode == "audio" {
        ensure!(
            selected.iter().any(has_audio),
            "No audio stream is available"
        );
    }
    ensure!(!selected.is_empty(), "No stream can be downloaded");
    let mut seen = std::collections::HashSet::new();
    selected.retain(|f| seen.insert(text(f, "format_id").to_string()));
    Ok(selected)
}

pub(crate) fn chapters(info: &Value) -> Vec<Chapter> {
    info["chapters"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|c| {
            let start = c["start_time"].as_f64()?;
            let end = c["end_time"].as_f64()?;
            (start.is_finite() && end.is_finite() && start >= 0.0 && end > start).then(|| Chapter {
                start,
                end,
                title: text(c, "title").into(),
            })
        })
        .collect()
}
fn sanitized_metadata(value: &Value) -> Value {
    match value {
        Value::Object(map) => Value::Object(
            map.iter()
                .filter(|(key, _)| {
                    !matches!(
                        key.as_str(),
                        "formats"
                            | "requested_formats"
                            | "requested_downloads"
                            | "url"
                            | "http_headers"
                            | "cookies"
                            | "automatic_captions"
                            | "subtitles"
                            | "thumbnails"
                            | "thumbnail"
                            | "webpage_url"
                            | "original_url"
                            | "manifest_url"
                            | "fragment_base_url"
                            | "fragments"
                            | "playlist_url"
                    )
                })
                .map(|(k, v)| (k.clone(), sanitized_metadata(v)))
                .collect(),
        ),
        Value::Array(values) => Value::Array(values.iter().map(sanitized_metadata).collect()),
        Value::String(s) if s.contains("http://") || s.contains("https://") => {
            json!(process::redact(s))
        }
        _ => value.clone(),
    }
}

pub(crate) async fn transfer(
    app: &Arc<App>,
    mut task: Task,
    info: Value,
    cancel: &CancellationToken,
) -> Result<SourceManifest> {
    let runtime_options = app.runtime_options(&task)?;
    ensure!(
        task.options.matches(&info),
        "Video is excluded by the selected date, duration or live filters"
    );
    let formats = select_formats(&info, &task.options)?;
    let directory = app
        .config
        .data_dir
        .join("tasks")
        .join(&task.id)
        .join("sources");
    crate::reject_links(&directory)?;
    std::fs::create_dir_all(&directory)?;
    let cookie_text = app.task_cookies(&task)?;
    let cookies = Cookies::new(app, cookie_text.as_deref())?;
    let live = info["is_live"].as_bool().unwrap_or(false)
        || formats
            .iter()
            .any(|f| matches!(text(f, "protocol"), "rtmp" | "rtsp" | "m3u8"));
    let mut files = Vec::new();
    for (index, format) in formats.iter().enumerate() {
        let kind = if has_video(format) { "video" } else { "audio" };
        app.set_stage(
            &task.id,
            "downloading",
            &format!("downloading_{kind}_{}", index + 1),
            None,
        )?;
        let output_template = directory.join(format!("source-{index}.%(ext)s"));
        let mut args = base_args(app, &runtime_options, &cookies, Some(&task))?;
        args.extend([
            "--no-playlist".into(),
            "--no-simulate".into(),
            "--no-progress".into(),
            "--newline".into(),
            "--progress".into(),
            "--progress-template".into(),
            "download:HU_PROGRESS:%(progress)j".into(),
            "--print".into(),
            "after_move:HU_FILE:%(filepath)j".into(),
            "--fixup".into(),
            "never".into(),
            "--no-write-info-json".into(),
            "--no-write-thumbnail".into(),
            "--no-mtime".into(),
            "--concurrent-fragments".into(),
            task.options.fragment_concurrent.to_string(),
            "--format".into(),
            text(format, "format_id").into(),
            "--output".into(),
            output_template.to_string_lossy().into(),
        ]);
        if live {
            args.extend([
                "--downloader".into(),
                "ffmpeg".into(),
                "--ffmpeg-location".into(),
                app.task_tool(&task, "ffmpeg")?.to_string_lossy().into(),
            ]);
            if task.options.live_from_start {
                args.push("--live-from-start".into());
            }
        } else {
            args.extend([
                "--downloader".into(),
                "native".into(),
                "--hls-prefer-native".into(),
            ]);
        }
        if !task.options.rate_limit.is_empty() {
            args.extend(["--limit-rate".into(), task.options.rate_limit.clone()]);
        }
        args.extend(["--".into(), task.url.clone()]);
        let app_progress = app.clone();
        let task_id = task.id.clone();
        let last = Arc::new(Mutex::new(Instant::now() - Duration::from_secs(1)));
        let callback: LineCallback = Arc::new(move |_, line| {
            if let Some(payload) = line.strip_prefix("HU_PROGRESS:")
                && let Ok(progress) = serde_json::from_str::<Value>(payload)
            {
                let mut last = last.lock().unwrap();
                if last.elapsed() < Duration::from_millis(200)
                    && text(&progress, "status") != "finished"
                {
                    return;
                }
                *last = Instant::now();
                if let Ok(mut task) = app_progress.db.task(&task_id) {
                    if task.status != "downloading" {
                        return;
                    }
                    let total = progress
                        .get("total_bytes")
                        .or(progress.get("total_bytes_estimate"))
                        .and_then(Value::as_f64)
                        .filter(|v| *v > 0.0);
                    let downloaded = progress["downloaded_bytes"].as_f64().unwrap_or(0.0);
                    task.stage_progress = total.map(|v| (downloaded / v * 100.0).clamp(0.0, 100.0));
                    task.stage_indeterminate = total.is_none();
                    task.progress = task.stage_progress.unwrap_or(0.0).min(99.0);
                    task.speed = progress["speed"].as_f64();
                    task.eta = progress["eta"].as_f64();
                    let _ = app_progress.save_task(&task);
                }
            }
        });
        let output = process::run(
            &app.task_tool(&task, "yt-dlp")?,
            &args,
            Some(&directory),
            cancel,
            Some(callback),
        )
        .await?;
        let path = output
            .stdout
            .lines()
            .filter_map(|s| s.strip_prefix("HU_FILE:"))
            .filter_map(|s| serde_json::from_str::<String>(s).ok())
            .map(PathBuf::from)
            .next_back()
            .or_else(|| {
                std::fs::read_dir(&directory)
                    .ok()?
                    .filter_map(Result::ok)
                    .map(|e| e.path())
                    .find(|p| {
                        p.file_stem()
                            .is_some_and(|s| s == format!("source-{index}").as_str())
                    })
            })
            .context("yt-dlp did not produce the requested stream")?;
        let canonical = path.canonicalize()?;
        ensure!(
            canonical.starts_with(directory.canonicalize()?),
            "yt-dlp returned an unowned stream path"
        );
        let (hash, size) = hash_file(&canonical)?;
        ensure!(size > 0, "Downloaded stream is empty");
        files.push(SourceFile {
            path: canonical,
            hash,
            size,
            kind: kind.into(),
            format_id: text(format, "format_id").into(),
            language: format["language"].as_str().map(str::to_string),
        });
    }
    task = app.db.task(&task.id)?;
    app.set_stage(&task.id, "downloading", "downloading_assets", None)?;
    download_subtitles(app, &task, &info, &directory, &cookies, cancel, &mut files).await?;
    if task.options.write_thumbnail
        || task.options.embed_thumbnail
        || task.options.prepend_cover_enabled
    {
        let thumbnail = text(&info, "thumbnail");
        ensure!(
            !thumbnail.is_empty()
                || !task.options.embed_thumbnail && !task.options.prepend_cover_enabled,
            "This video has no thumbnail for the requested cover operation"
        );
        if !thumbnail.is_empty() {
            let client = reqwest::Client::builder()
                .timeout(Duration::from_secs(60))
                .build()?;
            let bytes = tokio::select! {v=client.get(thumbnail).send()=>v?.error_for_status()?.bytes().await?,_=cancel.cancelled()=>bail!("Operation interrupted")};
            ensure!(bytes.len() <= 32 * 1024 * 1024, "Thumbnail is too large");
            let path = directory.join("thumbnail.source");
            write_sync(&path, &bytes)?;
            let extension = match image::guess_format(&bytes)? {
                image::ImageFormat::Jpeg => "jpg",
                image::ImageFormat::Png => "png",
                image::ImageFormat::WebP => "webp",
                _ => "png",
            };
            let target = directory.join(format!("thumbnail.{extension}"));
            if extension == "png" && image::guess_format(&bytes)? != image::ImageFormat::Png {
                image::load_from_memory(&bytes)?
                    .save_with_format(&target, image::ImageFormat::Png)?;
                std::fs::remove_file(&path)?;
            } else {
                std::fs::rename(&path, &target)?;
            }
            let (hash, size) = hash_file(&target)?;
            files.push(SourceFile {
                path: target,
                hash,
                size,
                kind: "thumbnail".into(),
                format_id: String::new(),
                language: None,
            });
        }
    }
    let metadata = sanitized_metadata(&info);
    if task.options.write_info_json {
        let path = directory.join("metadata.info.json");
        write_sync(&path, &serde_json::to_vec_pretty(&metadata)?)?;
        let (hash, size) = hash_file(&path)?;
        files.push(SourceFile {
            path,
            hash,
            size,
            kind: "info".into(),
            format_id: String::new(),
            language: None,
        });
    }
    if task.options.write_description {
        let path = directory.join("description.txt");
        write_sync(&path, text(&metadata, "description").as_bytes())?;
        let (hash, size) = hash_file(&path)?;
        files.push(SourceFile {
            path,
            hash,
            size,
            kind: "description".into(),
            format_id: String::new(),
            language: None,
        });
    }
    if task.options.write_comments {
        let path = directory.join("comments.json");
        write_sync(&path, &serde_json::to_vec_pretty(&metadata["comments"])?)?;
        let (hash, size) = hash_file(&path)?;
        files.push(SourceFile {
            path,
            hash,
            size,
            kind: "comments".into(),
            format_id: String::new(),
            language: None,
        });
    }
    let sponsor_segments = fetch_sponsor(&task.options, &info, cancel).await?;
    let extension = output_extension(&task.options, &formats);
    Ok(SourceManifest {
        version: 1,
        directory,
        files,
        metadata,
        duration: info["duration"].as_f64(),
        chapters: chapters(&info),
        sponsor_segments,
        output_extension: extension,
    })
}

async fn download_subtitles(
    app: &App,
    task: &Task,
    info: &Value,
    directory: &Path,
    cookies: &Cookies,
    cancel: &CancellationToken,
    files: &mut Vec<SourceFile>,
) -> Result<()> {
    let requested = task.options.languages();
    if requested.is_empty() {
        return Ok(());
    }
    let mut available = info["subtitles"]
        .as_object()
        .into_iter()
        .flat_map(|m| m.keys())
        .cloned()
        .collect::<std::collections::BTreeSet<_>>();
    if task.options.automatic_subtitles {
        available.extend(
            info["automatic_captions"]
                .as_object()
                .into_iter()
                .flat_map(|m| m.keys())
                .cloned(),
        );
    }
    let mut selected = std::collections::BTreeSet::new();
    for language in &requested {
        let matched = available
            .iter()
            .filter(|a| {
                language == "all"
                    || language == "*"
                    || *a == language
                    || a.starts_with(&format!("{}-", language.trim_end_matches(".*")))
                    || language.ends_with(".*") && a.starts_with(language.trim_end_matches(".*"))
            })
            .cloned()
            .collect::<Vec<_>>();
        ensure!(
            !matched.is_empty(),
            "Requested subtitles {language} are unavailable"
        );
        selected.extend(matched);
    }
    let runtime_options = app.runtime_options(task)?;
    let mut args = base_args(app, &runtime_options, cookies, Some(task))?;
    args.extend([
        "--no-playlist".into(),
        "--skip-download".into(),
        "--write-subs".into(),
        "--sub-langs".into(),
        selected.iter().cloned().collect::<Vec<_>>().join(","),
        "--sub-format".into(),
        "best".into(),
        "--output".into(),
        directory.join("subtitles.%(ext)s").to_string_lossy().into(),
    ]);
    if task.options.automatic_subtitles {
        args.push("--write-auto-subs".into());
    }
    args.extend(["--".into(), task.url.clone()]);
    process::run(
        &app.task_tool(task, "yt-dlp")?,
        &args,
        Some(directory),
        cancel,
        None,
    )
    .await?;
    for entry in std::fs::read_dir(directory)? {
        let path = entry?.path();
        if path
            .file_name()
            .is_some_and(|n| n.to_string_lossy().starts_with("subtitles."))
        {
            let filename = path.file_name().unwrap().to_string_lossy();
            let language = selected
                .iter()
                .find(|l| filename.contains(&format!(".{l}.")))
                .cloned();
            if language.is_some() {
                let (hash, size) = hash_file(&path)?;
                ensure!(size > 0, "Subtitle file is empty");
                files.push(SourceFile {
                    path,
                    hash,
                    size,
                    kind: "subtitle".into(),
                    format_id: String::new(),
                    language,
                });
            }
        }
    }
    for language in selected {
        ensure!(
            files
                .iter()
                .any(|f| f.kind == "subtitle" && f.language.as_deref() == Some(&language)),
            "yt-dlp did not save requested subtitles {language}"
        );
    }
    Ok(())
}

async fn fetch_sponsor(
    options: &DownloadOptions,
    info: &Value,
    cancel: &CancellationToken,
) -> Result<Vec<Chapter>> {
    if options.sponsorblock_mode == "off" {
        return Ok(vec![]);
    }
    ensure!(
        text(info, "extractor_key").eq_ignore_ascii_case("Youtube"),
        "SponsorBlock supports YouTube video IDs only"
    );
    let response = tokio::select! {r=reqwest::Client::new().get("https://sponsor.ajay.app/api/skipSegments").query(&[("videoID",text(info,"id")),("categories",&serde_json::to_string(&options.sponsorblock_categories)?)]).send()=>r?,_=cancel.cancelled()=>bail!("Operation interrupted")};
    if response.status() == reqwest::StatusCode::NOT_FOUND {
        return Ok(vec![]);
    }
    let segments: Value = response.error_for_status()?.json().await?;
    Ok(segments
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|s| {
            let start = s["segment"][0].as_f64()?;
            let end = s["segment"][1].as_f64()?;
            (start.is_finite() && end.is_finite() && start >= 0.0 && end > start).then(|| Chapter {
                start,
                end,
                title: format!("SponsorBlock: {}", text(s, "category")),
            })
        })
        .collect())
}

fn output_extension(options: &DownloadOptions, formats: &[Value]) -> String {
    if options.content_mode == "audio"
        && (options.audio_track == "all" || formats.iter().filter(|f| has_audio(f)).count() > 1)
    {
        return if options.effective_container() == "mp4" {
            "mp4"
        } else {
            "mka"
        }
        .into();
    }
    if options.content_mode == "audio" && options.audio_format != "best" {
        return match options.audio_format.as_str() {
            "aac" => "m4a",
            "alac" => "m4a",
            "vorbis" => "ogg",
            other => other,
        }
        .into();
    }
    if options.effective_container() != "auto" {
        return options.effective_container().into();
    }
    if options.audio_track == "all" || options.audio_format_ids.len() > 1 {
        return "mkv".into();
    }
    if options.content_mode == "audio" {
        return match text(&formats[0], "ext") {
            "webm" => "webm",
            "m4a" => "m4a",
            "mp3" => "mp3",
            "ogg" => "ogg",
            _ => "mka",
        }
        .into();
    }
    if formats.iter().all(|f| {
        !has_video(f)
            || codec_match(text(f, "vcodec"), "h264")
            || codec_match(text(f, "vcodec"), "h265")
    }) && formats.iter().all(|f| {
        !has_audio(f)
            || matches!(
                text(f, "acodec"),
                "aac" | "mp4a.40.2" | "mp4a.40.5" | "alac"
            )
    }) {
        "mp4".into()
    } else {
        "mkv".into()
    }
}

pub(crate) fn hash_file(path: &Path) -> Result<(String, u64)> {
    let mut file = std::fs::File::open(path)?;
    let mut hasher = Sha256::new();
    let mut buffer = [0u8; 1024 * 1024];
    let mut size = 0;
    loop {
        let n = file.read(&mut buffer)?;
        if n == 0 {
            break;
        }
        hasher.update(&buffer[..n]);
        size += n as u64;
    }
    Ok((hex::encode(hasher.finalize()), size))
}
pub(crate) fn write_sync(path: &Path, bytes: &[u8]) -> Result<()> {
    crate::reject_links(path)?;
    let mut file = std::fs::File::create(path)?;
    file.write_all(bytes)?;
    file.sync_all()?;
    Ok(())
}

impl App {
    pub(crate) async fn run_task(
        self: &Arc<Self>,
        id: &str,
        cancel: &CancellationToken,
    ) -> Result<()> {
        let mut task = self.db.task(id)?;
        if task.manifest.is_none() {
            let permit = tokio::select! {p=self.network.clone().acquire_owned()=>p?,_=cancel.cancelled()=>bail!("Operation interrupted")};
            self.set_stage(id, "probing", "probing", None)?;
            let cookies = self.task_cookies(&task)?;
            let runtime_options = self.runtime_options(&task)?;
            let info = probe(
                self,
                &task.url,
                &runtime_options,
                cookies.as_deref(),
                cancel,
                Some(&task),
            )
            .await?;
            task = self.db.task(id)?;
            task.title = text(&info, "title").to_string();
            self.save_task(&task)?;
            let formats = select_formats(&info, &task.options)?;
            let special = info["is_live"].as_bool().unwrap_or(false)
                || formats
                    .iter()
                    .any(|f| matches!(text(f, "protocol"), "rtmp" | "rtsp" | "m3u8"));
            if special {
                drop(permit);
                let _capture = tokio::select! {p=self.capture.acquire()=>p?,_=cancel.cancelled()=>bail!("Operation interrupted")};
                let manifest = transfer(self, task.clone(), info, cancel).await?;
                self.freeze_sources(id, manifest)?;
            } else {
                let manifest = transfer(self, task.clone(), info, cancel).await?;
                self.freeze_sources(id, manifest)?;
                drop(permit);
            }
        }
        let _processor = tokio::select! {p=self.processor.acquire()=>p?,_=cancel.cancelled()=>bail!("Operation interrupted")};
        crate::processing::process_task(self, id, cancel).await
    }
    fn freeze_sources(&self, id: &str, manifest: SourceManifest) -> Result<()> {
        let mut task = self.db.task(id)?;
        let file = manifest
            .directory
            .parent()
            .unwrap()
            .join("source-manifest.json");
        write_sync(&file, &serde_json::to_vec_pretty(&manifest)?)?;
        task.manifest = Some(manifest);
        task.cookie_snapshot = None;
        task.stage("waiting_processing", "waiting_processing", None);
        self.save_task(&task)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn absent_frozen_deno_disables_default_runtime_and_invalid_snapshot_is_rejected()
    -> Result<()> {
        let root = tempfile::tempdir()?;
        let app = App::open(crate::AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("output"),
            tool_dir: None,
        })
        .await?;
        let cookies = Cookies { file: None };
        let mut task = crate::tests::paused_task(std::collections::HashMap::new());
        let args = base_args(&app, &task.options, &cookies, Some(&task))?;
        ensure!(
            args.iter().any(|a| a == "--no-js-runtimes")
                && !args.iter().any(|a| a == "--js-runtimes"),
            "An absent Deno snapshot allowed implicit PATH fallback"
        );
        let mutable = root
            .path()
            .join(format!("deno{}", std::env::consts::EXE_SUFFIX));
        std::fs::write(&mutable, "mutable runtime")?;
        task.tools.insert("deno".into(), mutable);
        ensure!(
            base_args(&app, &task.options, &cookies, Some(&task)).is_err(),
            "An invalid frozen Deno runtime was silently omitted"
        );
        app.shutdown().await?;
        Ok(())
    }
    #[test]
    fn exact_plan_retains_all_audio_languages_and_prefers_resolution() {
        let info = json!({"formats":[{"format_id":"v","height":1080,"fps":30,"vcodec":"avc1","acodec":"none"},{"format_id":"v-low","height":720,"fps":60,"vcodec":"avc1","acodec":"none"},{"format_id":"en-low","language":"en","abr":64,"vcodec":"none","acodec":"aac"},{"format_id":"en","language":"en","abr":128,"vcodec":"none","acodec":"aac"},{"format_id":"zh","language":"zh-Hans","abr":128,"vcodec":"none","acodec":"aac"}]});
        let options = DownloadOptions::parse(json!({"audioTrack":"all","videoFps":"60"})).unwrap();
        let formats = select_formats(&info, &options).unwrap();
        assert_eq!(
            formats
                .iter()
                .map(|f| text(f, "format_id"))
                .collect::<Vec<_>>(),
            vec!["v", "en", "zh"]
        );
        let options = DownloadOptions::parse(json!({"videoFormatId":"missing"})).unwrap();
        assert!(select_formats(&info, &options).is_err());
    }
    #[test]
    fn durable_metadata_contains_no_signed_urls() {
        let v = sanitized_metadata(
            &json!({"title":"sample","formats":[{"url":"https://a?secret=b"}],"http_headers":{"Cookie":"secret"},"description":"https://a?secret=b"}),
        );
        assert!(!v.to_string().contains("secret"));
    }
    #[test]
    fn unknown_generic_codecs_are_probeable_media_and_explicit_audio_stays_audio() {
        let info = json!({"formats":[{"format_id":"mp4","ext":"mp4","url":"https://example.test/video.mp4"}]});
        let formats = select_formats(&info, &DownloadOptions::default()).unwrap();
        assert_eq!(text(&formats[0], "format_id"), "mp4");
        let audio =
            json!({"formats":[{"format_id":"audio","vcodec":"none","acodec":"aac","ext":"m4a"}]});
        assert!(select_formats(&audio, &DownloadOptions::default()).is_err());
        let options = DownloadOptions::parse(json!({"contentMode":"audio"})).unwrap();
        assert_eq!(
            text(&select_formats(&audio, &options).unwrap()[0], "format_id"),
            "audio"
        );
    }
}

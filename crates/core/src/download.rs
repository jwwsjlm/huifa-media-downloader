use crate::{
    App, DownloadOptions,
    model::{Chapter, SourceFile, SourceManifest, Task, TransferBytes, text},
    process::{self, LineCallback},
};
use anyhow::{Context, Result, bail, ensure};
use futures_util::future::join_all;
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

pub(crate) struct Cookies {
    file: Option<tempfile::NamedTempFile>,
    plugins: tempfile::TempDir,
}
impl Cookies {
    pub(crate) fn new(app: &App, text: Option<&str>) -> Result<Self> {
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
        let plugins = tempfile::Builder::new()
            .prefix(".ytdlp-plugins-")
            .tempdir_in(&app.config.data_dir)?;
        let extractor = plugins.path().join("huifa/yt_dlp_plugins/extractor");
        std::fs::create_dir_all(&extractor)?;
        std::fs::write(
            extractor.join("douyin.py"),
            include_bytes!("ytdlp_plugins/yt_dlp_plugins/extractor/douyin.py"),
        )?;
        Ok(Self { file, plugins })
    }
    fn apply(&self, args: &mut Vec<String>) {
        // Load only the compatibility code shipped with this application.
        args.extend([
            "--no-plugin-dirs".into(),
            "--plugin-dirs".into(),
            self.plugins.path().to_string_lossy().into_owned(),
        ]);
        if let Some(file) = &self.file {
            args.extend([
                "--cookies".into(),
                file.path().to_string_lossy().to_string(),
            ]);
        }
    }
}

pub(crate) async fn base_args(
    app: &App,
    options: &DownloadOptions,
    cookies: &Cookies,
    task: Option<&Task>,
) -> Result<Vec<String>> {
    let mut args = vec![
        "--ignore-config".into(),
        "--encoding".into(),
        "utf-8".into(),
        "--no-js-runtimes".into(),
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
    args.push(
        if task.is_some() {
            "--verbose"
        } else {
            "--no-warnings"
        }
        .into(),
    );
    cookies.apply(&mut args);
    if !options.proxy.is_empty() {
        args.extend(["--proxy".into(), options.proxy.clone()]);
    }
    for (name, runtime) in [("deno", "deno"), ("qjs", "quickjs")] {
        let path = match task {
            Some(task) if task.tools.contains_key(name) => Some(app.task_tool(task, name).await?),
            Some(_) => None,
            None => app.tool(name).ok(),
        };
        if let Some(path) = path {
            args.extend([
                "--js-runtimes".into(),
                format!("{runtime}:{}", path.display()),
            ]);
            break;
        }
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
    let mut args = base_args(app, options, &cookies, task).await?;
    args.extend([
        "--dump-single-json".into(),
        "--skip-download".into(),
        "--simulate".into(),
        "--no-playlist".into(),
    ]);
    if !options.languages().is_empty() {
        args.push("--write-subs".into());
        if options.automatic_subtitles {
            args.push("--write-auto-subs".into());
        }
    }
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
        app.task_tool(task, "yt-dlp").await?
    } else {
        app.tool("yt-dlp")?
    };
    let output = metadata_process(app, &executable, &args, cancel, options.wait_for_live, task)
        .await
        .context("Failed to parse video with yt-dlp")?;
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
    cancel: &CancellationToken,
) -> Result<Value> {
    let permit = tokio::select! { permit=app.network.acquire()=>permit?, _=cancel.cancelled()=>bail!("Operation interrupted") };
    let info = probe(app, url, options, cookies, cancel, None).await?;
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
    cancel: &CancellationToken,
) -> Result<Value> {
    let _permit = tokio::select! { permit=app.network.acquire()=>permit?, _=cancel.cancelled()=>bail!("Operation interrupted") };
    let cookies = Cookies::new(app, cookie_text)?;
    let mut args = base_args(app, options, &cookies, None).await?;
    args.extend([
        "--dump-single-json".into(),
        "--flat-playlist".into(),
        "--skip-download".into(),
    ]);
    if options.collection_mode != "all" {
        args.push("--no-playlist".into());
    }
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
    let output = metadata_process(app, &app.tool("yt-dlp")?, &args, cancel, false, None)
        .await
        .context("Failed to identify media or collection with yt-dlp")?;
    let info: Value = serde_json::from_str(&output.stdout)
        .context("yt-dlp returned invalid collection metadata")?;
    let all = info["entries"]
        .as_array()
        .cloned()
        .unwrap_or_else(|| vec![info.clone()]);
    let entries=all.iter().filter(|e|options.matches(e)).map(|entry|{
        let url=entry.get("webpage_url").or(entry.get("url")).and_then(Value::as_str).unwrap_or("");
        let url=if url.starts_with("http"){url.to_string()}else if text(entry,"ie_key")=="Youtube" {format!("https://www.youtube.com/watch?v={}",text(entry,"id"))}else{String::new()};
        json!({"id":entry["id"],"url":url,"title":entry["title"],"duration":entry["duration"],"thumbnail":safe_thumbnail(entry),"isLive":entry["is_live"].as_bool().unwrap_or(false)})
    }).filter(|v|!text(v,"url").is_empty()).collect::<Vec<_>>();
    let kind = if info["entries"].is_array()
        || matches!(text(&info, "_type"), "playlist" | "multi_video")
    {
        "collection"
    } else {
        "video"
    };
    Ok(json!({"kind":kind,"id":info["id"],"title":info["title"],"entries":entries}))
}

fn transient_metadata_error(error: &str) -> bool {
    let error = error.to_ascii_lowercase();
    if [
        "http error 403",
        "http error 404",
        "sign in",
        "sign-in",
        "fresh cookies",
        "unsupported url",
        "not available",
        "private video",
        "requirements unmet",
    ]
    .iter()
    .any(|value| error.contains(value))
    {
        return false;
    }
    [
        "http error 429",
        "http error 500",
        "http error 502",
        "http error 503",
        "http error 504",
        "too many requests",
        "timed out",
        "timeout",
        "connection reset",
        "connection refused",
        "temporary failure",
        "network is unreachable",
        "remote end closed",
        "getaddrinfo failed",
        "name or service not known",
    ]
    .iter()
    .any(|value| error.contains(value))
}

async fn metadata_process(
    app: &App,
    executable: &Path,
    args: &[String],
    cancel: &CancellationToken,
    wait_for_live: bool,
    task: Option<&Task>,
) -> Result<process::ProcessOutput> {
    let deadline = (!wait_for_live).then(|| Instant::now() + Duration::from_secs(120));
    let log = task.map(|task| app.raw_task_log(&task.id)).transpose()?;
    for attempt in 0..3 {
        let timeout = deadline.map(|deadline| deadline.saturating_duration_since(Instant::now()));
        ensure!(
            !timeout.is_some_and(|timeout| timeout.is_zero()),
            "Metadata query timed out"
        );
        let result = process::run_limited_logged(
            executable,
            args,
            cancel,
            64 * 1024 * 1024,
            timeout,
            log.clone(),
        )
        .await;
        match result {
            Err(error)
                if attempt < 2
                    && !cancel.is_cancelled()
                    && deadline.is_none_or(|deadline| Instant::now() < deadline)
                    && transient_metadata_error(&format!("{error:#}")) =>
            {
                let seconds = if attempt == 0 { 1 } else { 3 };
                if let Some(task) = task {
                    app.log_task(
                        &task.id,
                        "warning",
                        &format!(
                            "Temporary metadata request failure; retry {}/2 in {seconds}s",
                            attempt + 1
                        ),
                    )?;
                }
                let delay = Duration::from_secs(seconds).min(
                    deadline
                        .map(|deadline| deadline.saturating_duration_since(Instant::now()))
                        .unwrap_or(Duration::MAX),
                );
                tokio::select! { _=tokio::time::sleep(delay)=>{}, _=cancel.cancelled()=>bail!("Operation interrupted") }
            }
            result => return result,
        }
    }
    unreachable!()
}

fn transient_stream_error(error: &str) -> bool {
    let error = error.to_ascii_lowercase();
    if [
        "http error 401",
        "http error 403",
        "http error 404",
        "sign in",
        "sign-in",
        "fresh cookies",
        "private video",
        "not available",
        "requirements unmet",
        "no space left",
        "disk full",
        "disk quota",
        "permission denied",
        "access is denied",
        "read-only file system",
        "unable to open for writing",
        "unable to write data",
        "certificate verify failed",
        "certificate_verify_failed",
    ]
    .iter()
    .any(|value| error.contains(value))
    {
        return false;
    }
    transient_metadata_error(&error)
        || [
            "incompleteread",
            "bytes read,",
            "contenttooshorterror",
            "eof occurred",
            "unexpected eof",
            "remotedisconnected",
            "connection aborted",
            "connection broken",
            "broken pipe",
        ]
        .iter()
        .any(|value| error.contains(value))
        || error.contains("downloaded ") && error.contains(" bytes, expected ")
}

async fn stream_process(
    app: &App,
    task: &Task,
    executable: &Path,
    args: &[String],
    directory: &Path,
    cancel: &CancellationToken,
    callback: LineCallback,
) -> Result<process::ProcessOutput> {
    let log = app.raw_task_log(&task.id)?;
    for attempt in 0..3 {
        let result = process::run_logged(
            executable,
            args,
            Some(directory),
            cancel,
            Some(callback.clone()),
            log.clone(),
        )
        .await;
        match result {
            Err(error)
                if attempt < 2
                    && !cancel.is_cancelled()
                    && transient_stream_error(&format!("{error:#}")) =>
            {
                let seconds = if attempt == 0 { 1 } else { 3 };
                app.log_task(
                    &task.id,
                    "warning",
                    &format!(
                        "Temporary stream download failure; retry {}/2 in {seconds}s using saved partial files",
                        attempt + 1
                    ),
                )?;
                tokio::select! {
                    biased;
                    _=cancel.cancelled()=>bail!("Operation interrupted"),
                    _=tokio::time::sleep(Duration::from_secs(seconds))=>{},
                }
            }
            result => return result,
        }
    }
    unreachable!()
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
    !matches!(text(format, "vcodec"), "none" | "images")
        && text(format, "protocol") != "mhtml"
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
    let quality_order = |order: Ordering| {
        if options.quality == "worst" {
            order.reverse()
        } else {
            order
        }
    };
    let res = quality_order(metric(a, "height").total_cmp(&metric(b, "height")));
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
        .then(quality_order(metric(a, "tbr").total_cmp(&metric(b, "tbr"))))
}
pub(crate) fn resolution_tier(format: &Value) -> f64 {
    let height = metric(format, "height");
    let width = metric(format, "width");
    let shorter = if width > 0.0 {
        height.min(width)
    } else {
        height
    };
    let longer = height.max(width);
    // Match the displayed quality tiers, including cinematic and portrait media.
    if longer >= 7680.0 && shorter >= 3200.0 {
        return 4320.0;
    }
    if longer >= 3840.0 && shorter >= 1600.0 {
        return 2160.0;
    }
    [2160.0, 1440.0, 1080.0, 720.0, 480.0, 360.0, 240.0, 144.0]
        .into_iter()
        .find(|tier| shorter >= *tier)
        .unwrap_or(shorter)
}
fn audio_matches(format: &Value, track: &str) -> bool {
    if track == "original" {
        return text(format, "format_note")
            .to_ascii_lowercase()
            .contains("original");
    }
    let language = text(format, "language");
    let aliases: &[&str] = match track {
        "zh-Hans" => &["zh-Hans", "zh-CN", "zh"],
        "zh-Hant" => &["zh-Hant", "zh-TW", "zh-HK"],
        _ => &[track],
    };
    aliases
        .iter()
        .any(|alias| language == *alias || language.starts_with(&format!("{alias}-")))
}
fn compare_audio(a: &Value, b: &Value, options: &DownloadOptions) -> Ordering {
    let score = |f: &Value| {
        if matches!(options.audio_track.as_str(), "default" | "all") {
            metric(f, "language_preference") > 0.0
        } else {
            audio_matches(f, &options.audio_track)
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
    let explicit_audio = options.audio_format_ids.is_empty()
        && options.format_id.is_empty()
        && !matches!(options.audio_track.as_str(), "default" | "all");
    if explicit_audio {
        ensure!(
            formats
                .iter()
                .any(|f| has_audio(f) && audio_matches(f, &options.audio_track)),
            "Requirements unmet: audioTrack={}; available: {}. Adjust the requirements before retrying",
            options.audio_track,
            formats
                .iter()
                .filter(|f| has_audio(f))
                .map(|f| text(f, "language"))
                .filter(|s| !s.is_empty())
                .collect::<std::collections::BTreeSet<_>>()
                .into_iter()
                .collect::<Vec<_>>()
                .join(", ")
        );
    }
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
                            .is_none_or(|h| resolution_tier(f) == h as f64)
                        && options
                            .video_fps
                            .parse::<f64>()
                            .ok()
                            .is_none_or(|fps| metric(f, "fps").round() == fps)
                        && (!explicit_audio
                            || !has_audio(f)
                            || audio_matches(f, &options.audio_track))
                })
                .collect::<Vec<_>>();
            ensure!(
                !videos.is_empty(),
                "Requirements unmet: video={} / {} fps; available: {}. Adjust the requirements before retrying",
                options.quality,
                options.video_fps,
                formats
                    .iter()
                    .filter(|f| has_video(f))
                    .map(|f| format!(
                        "{}p{}",
                        metric(f, "height"),
                        if metric(f, "fps") > 0.0 {
                            format!(" / {} fps", metric(f, "fps"))
                        } else {
                            String::new()
                        }
                    ))
                    .collect::<std::collections::BTreeSet<_>>()
                    .into_iter()
                    .collect::<Vec<_>>()
                    .join(", ")
            );
            if videos.iter().any(|f| vr_match(f, &options.vr_mode)) {
                videos.retain(|f| vr_match(f, &options.vr_mode));
            }
            if options.quality == "worst" && videos.iter().any(|f| metric(f, "height") > 0.0) {
                videos.retain(|f| metric(f, "height") > 0.0);
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
        let track_key = |format: &Value| {
            if !text(format, "audio_track_id").is_empty() {
                text(format, "audio_track_id").to_string()
            } else if !text(format, "language").is_empty() {
                text(format, "language").to_string()
            } else {
                "default".into()
            }
        };
        let included = selected
            .iter()
            .filter(|format| has_audio(format))
            .map(track_key)
            .collect::<std::collections::BTreeSet<_>>();
        let mut tracks = std::collections::BTreeMap::<String, Value>::new();
        for format in formats.iter().filter(|f| has_audio(f)) {
            let language = track_key(format);
            if included.contains(&language) {
                continue;
            }
            if tracks.get(&language).is_none_or(|previous| {
                compare_audio(format, previous, options) == Ordering::Greater
            }) {
                tracks.insert(language, format.clone());
            }
        }
        selected.extend(tracks.into_values());
    } else if selected.is_empty()
        || !selected
            .iter()
            .any(|f| has_audio(f) && (!explicit_audio || audio_matches(f, &options.audio_track)))
    {
        let audio = formats
            .iter()
            .filter(|f| {
                has_audio(f)
                    && !has_video(f)
                    && (!explicit_audio || audio_matches(f, &options.audio_track))
            })
            .max_by(|a, b| compare_audio(a, b, options));
        if let Some(audio) = audio {
            selected.push(audio.clone());
        } else if let Some(combined) = formats
            .iter()
            .filter(|f| has_audio(f) && (!explicit_audio || audio_matches(f, &options.audio_track)))
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
    ensure!(
        !explicit_audio
            || selected
                .iter()
                .any(|f| has_audio(f) && audio_matches(f, &options.audio_track)),
        "Requirements unmet: audioTrack={}. Choose a compatible video or audio track before retrying",
        options.audio_track
    );
    let (_, missing) = subtitle_selection(info, &options.languages(), options.automatic_subtitles);
    ensure!(
        missing.is_empty() || options.skip_unavailable_subtitles,
        "Requirements unmet: subtitles={}. Adjust the subtitle languages or disable subtitles before retrying",
        missing.join(", ")
    );
    let mut seen = std::collections::HashSet::new();
    selected.retain(|f| seen.insert(text(f, "format_id").to_string()));
    Ok(selected)
}

async fn collect_transfers<F, T>(
    transfers: impl IntoIterator<Item = F>,
    live: bool,
    cancel: &CancellationToken,
) -> Result<Vec<T>>
where
    F: std::future::Future<Output = Result<T>>,
{
    if live {
        let failure = Mutex::new(None);
        let transfers = transfers.into_iter().map(|transfer| {
            let failure = &failure;
            async move {
                match transfer.await {
                    Ok(value) => Some(value),
                    Err(error) => {
                        let mut failure = failure.lock().unwrap();
                        if failure.is_none() {
                            *failure = Some(error);
                        }
                        cancel.cancel();
                        None
                    }
                }
            }
        });
        let results = join_all(transfers).await;
        if let Some(error) = failure.into_inner().unwrap() {
            return Err(error);
        }
        return Ok(results.into_iter().flatten().collect());
    }
    let mut results = Vec::new();
    for transfer in transfers {
        results.push(transfer.await?);
    }
    Ok(results)
}

pub(crate) fn is_live(info: &Value) -> bool {
    info["is_live"].as_bool().unwrap_or(false) || text(info, "live_status") == "is_live"
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

#[derive(Default)]
struct TransferProgress {
    samples: std::collections::VecDeque<(Instant, f64)>,
    last_received: Option<Instant>,
    last_emitted: Option<Instant>,
}

struct TransferUpdate {
    percent: Option<f64>,
    speed: Option<f64>,
    eta: Option<f64>,
    downloaded: Option<u64>,
    total: Option<u64>,
    estimated: bool,
}

impl TransferUpdate {
    fn apply(self, task: &mut Task, index: usize) {
        task.stage_progress = self.percent;
        task.stage_indeterminate = self.percent.is_none();
        task.progress = self.percent.unwrap_or(0.0).min(99.0);
        task.speed = self.speed;
        task.eta = self.eta;
        if let Some(downloaded) = self.downloaded
            && let Some(bytes) = task.transfer_bytes.get_mut(index)
        {
            bytes.downloaded = downloaded;
            if self.total.is_some() {
                bytes.total = self.total;
                bytes.estimated = self.estimated;
            }
            if bytes.total.is_some_and(|total| total < downloaded) {
                bytes.total = None;
                bytes.estimated = false;
            }
        }
    }

    fn live(self, speeds: &mut [Option<f64>], index: usize) -> Self {
        speeds[index] = self.speed;
        Self {
            percent: None,
            speed: speeds.iter().flatten().copied().reduce(|a, b| a + b),
            eta: None,
            ..self
        }
    }
}

fn byte_count(value: &Value) -> Option<u64> {
    value.as_u64().or_else(|| {
        value
            .as_f64()
            .filter(|value| {
                value.is_finite()
                    && *value >= 0.0
                    && *value < u64::MAX as f64
                    && value.fract() == 0.0
            })
            .map(|value| value as u64)
    })
}

impl TransferProgress {
    fn update(&mut self, line: &str, now: Instant) -> Option<TransferUpdate> {
        let payload = line.strip_prefix("HU_PROGRESS:")?;
        let progress = serde_json::from_str::<Value>(payload).ok()?;
        let finished = text(&progress, "status") == "finished";
        if !finished && text(&progress, "status") != "downloading" {
            return None;
        }
        let downloaded_bytes = byte_count(&progress["downloaded_bytes"])?;
        let downloaded = downloaded_bytes as f64;
        if self.samples.back().is_some_and(|(time, bytes)| {
            now.saturating_duration_since(*time) >= Duration::from_secs(5) || downloaded < *bytes
        }) {
            self.samples.clear();
        }
        self.last_received = Some(now);
        self.samples.push_back((now, downloaded));
        while self.samples.len() > 2
            && now.saturating_duration_since(self.samples[1].0) >= Duration::from_secs(5)
        {
            self.samples.pop_front();
        }
        let exact_total = byte_count(&progress["total_bytes"]).filter(|value| *value > 0);
        let total_bytes = if finished {
            (downloaded_bytes > 0).then_some(downloaded_bytes)
        } else {
            exact_total.or_else(|| {
                byte_count(&progress["total_bytes_estimate"]).filter(|value| *value > 0)
            })
        };
        let total = total_bytes.map(|value| value as f64);
        // Fragment hooks report burst speeds; use actual byte movement over one shared window.
        let (start, bytes) = self.samples.front().unwrap();
        let seconds = now.saturating_duration_since(*start).as_secs_f64();
        let speed = (!finished && seconds >= 1.0).then(|| (downloaded - bytes) / seconds);
        let eta = total.and_then(|total| {
            speed
                .filter(|speed| *speed > 0.0)
                .map(|speed| ((total - downloaded) / speed).max(0.0))
        });
        if finished {
            self.samples.clear();
            self.last_received = None;
        } else if self
            .last_emitted
            .is_some_and(|last| now.saturating_duration_since(last) < Duration::from_millis(200))
        {
            return None;
        }
        self.last_emitted = Some(now);
        Some(TransferUpdate {
            percent: total.map(|v| (downloaded / v * 100.0).clamp(0.0, 100.0)),
            speed,
            eta,
            downloaded: Some(downloaded_bytes),
            total: total_bytes,
            estimated: !finished && exact_total.is_none() && total_bytes.is_some(),
        })
    }

    fn expire(&mut self, now: Instant) -> bool {
        if self
            .last_received
            .is_some_and(|last| now.saturating_duration_since(last) >= Duration::from_secs(5))
        {
            self.samples.clear();
            self.last_received = None;
            return true;
        }
        false
    }
}

fn transfer_identity(info: &Value, formats: &[Value]) -> Result<String> {
    let formats = formats.iter().map(|format| {
        json!({"id":format["format_id"],"url":format["url"],"fragments":format["fragments"],"size":format["filesize"],"width":format["width"],"height":format["height"],"fps":format["fps"],"video":format["vcodec"],"audio":format["acodec"]})
    }).collect::<Vec<_>>();
    let identity = json!({"extractor":info["extractor_key"],"id":info["id"],"duration":info["duration"],"modified":info["modified_timestamp"],"formats":formats});
    Ok(hex::encode(Sha256::digest(serde_json::to_vec(&identity)?)))
}

pub(crate) async fn transfer(
    app: &Arc<App>,
    mut task: Task,
    info: Value,
    cancel: &CancellationToken,
) -> Result<SourceManifest> {
    let runtime_options = app.runtime_options(&task)?;
    if let Some(reason) = task.options.exclusion_reason(&info) {
        bail!(reason);
    }
    let formats = if task.kind == "assets" {
        vec![]
    } else {
        select_formats(&info, &task.options)?
    };
    if task.kind != "assets" {
        task.source_info = Some(crate::model::MediaInfo::from_formats(
            &formats,
            info["duration"].as_f64(),
        ));
        let extension = output_extension(&task.options, &formats);
        crate::destination::reserve(app, &mut task, &extension, &info).await?;
        let expected = formats
            .iter()
            .filter_map(|format| {
                byte_count(&format["filesize"]).or_else(|| byte_count(&format["filesize_approx"]))
            })
            .fold(0u64, u64::saturating_add);
        crate::storage::before_download(app, &task, expected)?;
    }
    task.transfer_bytes = formats
        .iter()
        .enumerate()
        .map(|(index, format)| {
            task.transfer_bytes
                .get(index)
                .filter(|bytes| bytes.format_id == text(format, "format_id"))
                .cloned()
                .unwrap_or_else(|| {
                    let exact_total = byte_count(&format["filesize"]).filter(|value| *value > 0);
                    let total = if is_live(&info) {
                        None
                    } else {
                        exact_total.or_else(|| {
                            byte_count(&format["filesize_approx"]).filter(|value| *value > 0)
                        })
                    };
                    TransferBytes {
                        format_id: text(format, "format_id").into(),
                        downloaded: 0,
                        total,
                        estimated: exact_total.is_none() && total.is_some(),
                    }
                })
        })
        .collect();
    app.save_task(&task)?;
    let live = is_live(&info);
    let directory = app
        .config
        .data_dir
        .join("tasks")
        .join(&task.id)
        .join("sources")
        .join(if live {
            format!("live-{}", uuid::Uuid::new_v4())
        } else {
            transfer_identity(&info, &formats)?
        });
    crate::reject_links(&directory)?;
    if !directory.try_exists()? && task.transfer_bytes.iter().any(|bytes| bytes.downloaded > 0) {
        for bytes in &mut task.transfer_bytes {
            bytes.downloaded = 0;
        }
        task.warnings.push("The source or format changed; this attempt starts a new transfer and preserves the previous partial files.".into());
        app.save_task(&task)?;
    }
    std::fs::create_dir_all(&directory)?;
    let cookie_text = app.task_cookies(&task)?;
    let cookies = Cookies::new(app, cookie_text.as_deref())?;
    // Re-extracting can change CDN URLs behind the selected format and source identity.
    let info_file = if live {
        None
    } else {
        let mut file = tempfile::Builder::new()
            .prefix(".download-info-")
            .tempfile_in(&app.config.data_dir)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            file.as_file()
                .set_permissions(std::fs::Permissions::from_mode(0o600))?;
        }
        serde_json::to_writer(&mut file, &info)?;
        file.flush()?;
        Some(file)
    };
    let finish_capture = if live {
        Some(app.finish_capture_token(&task.id)?)
    } else {
        None
    };
    let capture_cancel = cancel.child_token();
    let cancel = &capture_cancel;
    let speeds = Arc::new(Mutex::new(vec![None; formats.len()]));
    if live {
        app.set_stage(&task.id, "downloading", "downloading_live", None)?;
    }
    let transfers = formats
        .iter()
        .enumerate()
        .map(|(index, format)| {
            let task = &task;
            let directory = &directory;
            let cookies = &cookies;
            let info_file = &info_file;
            let runtime_options = &runtime_options;
            let speeds = speeds.clone();
            let finish_capture = &finish_capture;
            async move {
                let kind = if has_video(format) { "video" } else { "audio" };
                let stage = if live {
                    "downloading_live".into()
                } else {
                    format!("downloading_{kind}_{}", index + 1)
                };
                if !live {
                    app.set_stage(&task.id, "downloading", &stage, None)?;
                }
                let output_template = directory.join(if live { format!("source-{index}.ts") } else { format!("source-{index}.%(ext)s") });
                let mut args = base_args(app, runtime_options, cookies, Some(task)).await?;
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
                let native = !live && !matches!(text(format, "protocol"), "rtmp" | "rtsp" | "m3u8");
                if !native {
                    args.extend([
                        "--downloader".into(),
                        "ffmpeg".into(),
                        "--ffmpeg-location".into(),
                        app.task_tool(task, "ffmpeg")
                            .await?
                            .to_string_lossy()
                            .into(),
                    ]);
                    if task.options.live_from_start {
                        args.push("--live-from-start".into());
                    }
                    if live {
                        args.extend(["--downloader-args".into(), "ffmpeg_o:-f mpegts".into()]);
                    }
                } else {
                    args.extend([
                        "--downloader".into(),
                        "native".into(),
                        "--hls-prefer-native".into(),
                        "--continue".into(),
                        "--part".into(),
                    ]);
                }
                if !task.options.rate_limit.is_empty() {
                    args.extend(["--limit-rate".into(), task.options.rate_limit.clone()]);
                }
                if let Some(file) = info_file {
                    args.extend(["--load-info-json".into(), file.path().to_string_lossy().into()]);
                } else {
                    args.extend(["--".into(), task.url.clone()]);
                }
                let app_progress = app.clone();
                let task_id = task.id.clone();
                let progress_state = Arc::new(Mutex::new(TransferProgress::default()));
                let callback_state = progress_state.clone();
                let callback_stage = stage.clone();
                let callback_speeds = speeds.clone();
                let callback: LineCallback = Arc::new(move |_, line| {
                    if !line.starts_with("HU_PROGRESS:") {
                        return;
                    }
                    let mut state = callback_state.lock().unwrap();
                    let mut speeds = callback_speeds.lock().unwrap();
                    if let Some(mut update) = state.update(line, Instant::now())
                        && let Ok(mut task) = app_progress.db.task(&task_id)
                        && task.status == "downloading"
                        && task.stage == callback_stage
                    {
                        if live {
                            update = update.live(&mut speeds, index);
                        }
                        update.apply(&mut task, index);
                        let _ = app_progress.save_task(&task);
                    }
                });
                let executable = app.task_tool(task, "yt-dlp").await?;
                let process = async {
                    if let Some(finish) = &finish_capture {
                        process::run_capture(&executable, &args, directory, cancel, finish, Some(callback), app.raw_task_log(&task.id)?).await
                    } else if native {
                        stream_process(app, task, &executable, &args, directory, cancel, callback).await
                    } else {
                        process::run_logged(&executable, &args, Some(directory), cancel, Some(callback), app.raw_task_log(&task.id)?).await
                    }
                };
                tokio::pin!(process);
                let mut stale_timer = tokio::time::interval(Duration::from_secs(1));
                let output = loop {
                    tokio::select! {
                        output = &mut process => break output,
                        _ = stale_timer.tick() => {
                            let mut state = progress_state.lock().unwrap();
                            let mut speeds = speeds.lock().unwrap();
                            if state.expire(Instant::now())
                                && let Ok(mut current) = app.db.task(&task.id)
                                && current.status == "downloading" && current.stage == stage
                            {
                                if live {
                                    TransferUpdate { percent: None, speed: None, eta: None, downloaded: None, total: None, estimated: false }
                                        .live(&mut speeds, index)
                                        .apply(&mut current, index);
                                } else {
                                    current.speed = None;
                                    current.eta = None;
                                }
                                app.save_task(&current)?;
                            }
                        }
                    }
                }
                .with_context(|| {
                    format!(
                        "Failed to download {kind} stream (format {}) with yt-dlp",
                        text(format, "format_id")
                    )
                })?;
                ensure!(!cancel.is_cancelled(), "Operation interrupted");
                let path = output
                    .stdout
                    .lines()
                    .filter_map(|s| s.strip_prefix("HU_FILE:"))
                    .filter_map(|s| serde_json::from_str::<String>(s).ok())
                    .map(PathBuf::from)
                    .next_back()
                    .or_else(|| {
                        if live {
                            return [format!("source-{index}.ts"), format!("source-{index}.ts.part")].into_iter().map(|name| directory.join(name)).find(|path| path.is_file());
                        }
                        std::fs::read_dir(directory)
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
                {
                    let _guard = speeds.lock().unwrap();
                    let mut current = app.db.task(&task.id)?;
                    let bytes = &mut current.transfer_bytes[index];
                    bytes.downloaded = size;
                    bytes.total = Some(size);
                    bytes.estimated = false;
                    app.save_task(&current)?;
                }
                Ok(SourceFile {
                    path: canonical,
                    hash,
                    size,
                    kind: kind.into(),
                    format_id: text(format, "format_id").into(),
                    language: format["language"].as_str().map(str::to_string),
                })
            }
        })
        .collect::<Vec<_>>();
    let mut files = collect_transfers(transfers, live, cancel).await?;
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
        if thumbnail.is_empty() && task.kind == "assets" {
            let mut current = app.db.task(&task.id)?;
            let warning = "The media source did not provide the requested thumbnail.".to_string();
            if !current.warnings.contains(&warning) {
                current.warnings.push(warning);
                app.save_task(&current)?;
            }
        }
        if !thumbnail.is_empty() {
            let bytes = crate::thumbnail::download(
                app,
                &task,
                &info,
                32 * 1024 * 1024,
                Duration::from_secs(60),
                cancel,
            )
            .await?
            .context("This media source did not provide a thumbnail")?;
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
    let sponsor_segments = if task.kind == "assets" {
        vec![]
    } else {
        fetch_sponsor(&task.options, &info, cancel).await?
    };
    let extension = if formats.is_empty() {
        String::new()
    } else {
        output_extension(&task.options, &formats)
    };
    Ok(SourceManifest {
        version: 1,
        directory,
        files,
        metadata,
        duration: if live {
            None
        } else {
            info["duration"].as_f64()
        },
        chapters: chapters(&info),
        sponsor_segments,
        output_extension: extension,
    })
}

fn subtitle_selection(
    info: &Value,
    requested: &[String],
    automatic: bool,
) -> (std::collections::BTreeSet<String>, Vec<String>) {
    let manual: std::collections::BTreeSet<_> = info["subtitles"]
        .as_object()
        .into_iter()
        .flat_map(|values| values.keys())
        .cloned()
        .collect();
    let mut available = manual.clone();
    if automatic {
        available.extend(
            info["automatic_captions"]
                .as_object()
                .into_iter()
                .flat_map(|values| values.keys())
                .cloned(),
        );
    }
    let mut selected = std::collections::BTreeSet::new();
    let mut missing = Vec::new();
    for language in requested {
        let aliases = match language.as_str() {
            "zh-Hans" => vec!["zh-Hans", "zh-CN", "zh"],
            "zh-Hant" => vec!["zh-Hant", "zh-TW", "zh-HK"],
            _ => vec![language.as_str()],
        };
        let all = matches!(language.as_str(), "all" | "*");
        let mut matched: Vec<_> = available
            .iter()
            .filter(|candidate| {
                candidate.as_str() != "live_chat"
                    && (all
                        || aliases.iter().any(|alias| {
                            candidate.as_str() == *alias
                                || candidate
                                    .starts_with(&format!("{}-", alias.trim_end_matches(".*")))
                                || alias.ends_with(".*")
                                    && candidate.starts_with(alias.trim_end_matches(".*"))
                        }))
            })
            .cloned()
            .collect();
        if !all {
            matched.sort_by_key(|candidate| {
                (
                    !manual.contains(candidate),
                    aliases
                        .iter()
                        .position(|alias| *alias == candidate)
                        .unwrap_or(aliases.len()),
                    candidate.clone(),
                )
            });
            matched.truncate(1);
        }
        if matched.is_empty() {
            missing.push(language.clone());
        }
        selected.extend(matched);
    }
    (selected, missing)
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
    let (selected, missing) =
        subtitle_selection(info, &requested, task.options.automatic_subtitles);
    ensure!(
        missing.is_empty() || task.options.skip_unavailable_subtitles,
        "Requirements unmet: subtitles={}. Adjust the subtitle languages or disable subtitles before retrying",
        missing.join(", ")
    );
    if selected.is_empty() {
        return Ok(());
    }
    let runtime_options = app.runtime_options(task)?;
    let mut args = base_args(app, &runtime_options, cookies, Some(task)).await?;
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
    process::run_logged(
        &app.task_tool(task, "yt-dlp").await?,
        &args,
        Some(directory),
        cancel,
        None,
        app.raw_task_log(&task.id)?,
    )
    .await
    .context("Failed to download subtitles with yt-dlp")?;
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
    let segments = sponsor_response(
        reqwest::Client::new()
            .get("https://sponsor.ajay.app/api/skipSegments")
            .query(&[
                ("videoID", text(info, "id")),
                (
                    "categories",
                    &serde_json::to_string(&options.sponsorblock_categories)?,
                ),
            ]),
        cancel,
        Duration::from_secs(30),
    )
    .await?;
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

async fn sponsor_response(
    request: reqwest::RequestBuilder,
    cancel: &CancellationToken,
    timeout: Duration,
) -> Result<Value> {
    let fetch = async {
        let response = request.send().await?;
        if response.status() == reqwest::StatusCode::NOT_FOUND {
            return Ok(json!([]));
        }
        let mut response = response.error_for_status()?;
        const MAX_BYTES: usize = 4 * 1024 * 1024;
        ensure!(
            response
                .content_length()
                .is_none_or(|size| size <= MAX_BYTES as u64),
            "SponsorBlock response is too large"
        );
        let mut bytes = Vec::new();
        while let Some(chunk) = response.chunk().await? {
            ensure!(
                chunk.len() <= MAX_BYTES.saturating_sub(bytes.len()),
                "SponsorBlock response is too large"
            );
            bytes.extend_from_slice(&chunk);
        }
        serde_json::from_slice(&bytes).context("Invalid SponsorBlock response")
    };
    tokio::select! {
        biased;
        _ = cancel.cancelled() => bail!("Operation interrupted"),
        result = tokio::time::timeout(timeout, fetch) => result.context("SponsorBlock request timed out")?,
    }
}

pub(crate) fn output_extension(options: &DownloadOptions, formats: &[Value]) -> String {
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
            let permit = self
                .task_permit(id, "network", &self.network, cancel)
                .await?;
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
            crate::workflow::plan(&mut task);
            self.save_task(&task)?;
            let preview = crate::thumbnail::cache_preview(self, &task, &info, cancel).await;
            ensure!(!cancel.is_cancelled(), "Operation interrupted");
            if let Ok(Some(path)) = preview {
                task = self.db.task(id)?;
                task.thumbnail = Some(path);
                self.save_task(&task)?;
            }
            let special = task.kind != "assets" && is_live(&info);
            task.live = special;
            self.save_task(&task)?;
            if special {
                drop(permit);
                self.set_stage(id, "queued", "waiting_live", None)?;
                let _capture = self
                    .task_permit(id, "capture", &self.capture, cancel)
                    .await?;
                let manifest = transfer(self, task.clone(), info, cancel).await?;
                self.freeze_sources(id, manifest)?;
            } else {
                let manifest = transfer(self, task.clone(), info, cancel).await?;
                self.freeze_sources(id, manifest)?;
                drop(permit);
            }
        }
        let _processor = self
            .task_permit(id, "processor", &self.processor, cancel)
            .await?;
        let mut task = self.db.task(id)?;
        crate::workflow::plan(&mut task);
        self.save_task(&task)?;
        crate::processing::process_task(self, id, cancel).await
    }
    fn freeze_sources(&self, id: &str, manifest: SourceManifest) -> Result<()> {
        let mut task = self.db.task(id)?;
        let file = self
            .config
            .data_dir
            .join("tasks")
            .join(id)
            .join("source-manifest.json");
        write_sync(&file, &serde_json::to_vec_pretty(&manifest)?)?;
        task.manifest = Some(manifest);
        let cookie_snapshot = task.cookie_snapshot.take();
        task.stage("waiting_processing", "waiting_processing", None);
        self.save_task(&task)?;
        if let Some(key) = cookie_snapshot {
            self.credentials.delete_secret(&key)?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn partial_transfer_identity_reuses_only_the_same_source_and_format() -> Result<()> {
        let info =
            json!({"extractor_key":"Local","id":"same","duration":10,"modified_timestamp":1});
        let format = json!({"format_id":"video","url":"https://example.test/video?signature=one","vcodec":"h264","acodec":"none","width":1920,"height":1080,"fps":30,"filesize":1000});
        let original = transfer_identity(&info, std::slice::from_ref(&format))?;
        assert_eq!(
            original,
            transfer_identity(&info, std::slice::from_ref(&format))?
        );
        for (key, value) in [
            ("url", json!("https://example.test/video?signature=two")),
            ("format_id", json!("other")),
            ("filesize", json!(1001)),
            ("fps", json!(60)),
        ] {
            let mut changed = format.clone();
            changed[key] = value;
            assert_ne!(
                original,
                transfer_identity(&info, &[changed])?,
                "{key} changed without invalidating partial bytes"
            );
        }
        let mut changed_source = info.clone();
        changed_source["id"] = json!("new");
        assert_ne!(original, transfer_identity(&changed_source, &[format])?);
        assert!(!original.contains("signature"));
        Ok(())
    }

    #[tokio::test]
    async fn live_transfers_start_together_and_preserve_manifest_order() -> Result<()> {
        let cancel = CancellationToken::new();
        let ready = Arc::new(tokio::sync::Barrier::new(2));
        let transfers = (0..2).map(|index| {
            let ready = ready.clone();
            async move {
                ready.wait().await;
                if index == 0 {
                    tokio::time::sleep(Duration::from_millis(10)).await;
                }
                Ok(index)
            }
        });
        assert_eq!(
            tokio::time::timeout(
                Duration::from_secs(1),
                collect_transfers(transfers, true, &cancel)
            )
            .await??,
            [0, 1]
        );
        let active = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let transfers = (0..2).map(|index| {
            let active = active.clone();
            async move {
                assert_eq!(active.fetch_add(1, std::sync::atomic::Ordering::SeqCst), 0);
                tokio::task::yield_now().await;
                active.fetch_sub(1, std::sync::atomic::Ordering::SeqCst);
                Ok(index)
            }
        });
        assert_eq!(collect_transfers(transfers, false, &cancel).await?, [0, 1]);
        Ok(())
    }

    #[tokio::test]
    async fn a_failed_live_transfer_waits_for_the_other_capture_to_clean_up() {
        struct Capture(Arc<std::sync::atomic::AtomicBool>);
        impl Drop for Capture {
            fn drop(&mut self) {
                self.0.store(true, std::sync::atomic::Ordering::SeqCst);
            }
        }
        let dropped = Arc::new(std::sync::atomic::AtomicBool::new(false));
        let parent = CancellationToken::new();
        let cancel = parent.child_token();
        let transfers = (0..2).map(|index| {
            let dropped = dropped.clone();
            let cancel = &cancel;
            async move {
                if index == 0 {
                    let _capture = Capture(dropped);
                    cancel.cancelled().await;
                    tokio::time::sleep(Duration::from_millis(10)).await;
                    bail!("Operation interrupted");
                }
                bail!("capture failed")
            }
        });
        let result: Result<Vec<()>> = collect_transfers(transfers, true, &cancel).await;
        assert_eq!(result.unwrap_err().to_string(), "capture failed");
        assert!(dropped.load(std::sync::atomic::Ordering::SeqCst));
        assert!(!parent.is_cancelled());
    }

    #[cfg(windows)]
    #[tokio::test]
    async fn failed_live_group_waits_for_supervised_parent_and_descendant_exit() -> Result<()> {
        use windows_sys::Win32::{
            Foundation::CloseHandle,
            System::Threading::{
                GetExitCodeProcess, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION,
            },
        };
        let (ready, started) = tokio::sync::oneshot::channel();
        let ready = Mutex::new(Some(ready));
        let pids = Arc::new(Mutex::new(Vec::new()));
        let observed = pids.clone();
        let callback: LineCallback = Arc::new(move |_, line| {
            if let Some(value) = line.strip_prefix("READY:") {
                *observed.lock().unwrap() = value
                    .split(':')
                    .map(|pid| pid.parse::<u32>().unwrap())
                    .collect();
                if let Some(ready) = ready.lock().unwrap().take() {
                    let _ = ready.send(());
                }
            }
        });
        let (fail, failed) = tokio::sync::oneshot::channel();
        let failed = Mutex::new(Some(failed));
        let cancel = CancellationToken::new();
        let exe = PathBuf::from("powershell.exe");
        let args = vec![
            "-NoProfile".into(), "-NonInteractive".into(), "-Command".into(),
            "$worker = Start-Process powershell.exe -WindowStyle Hidden -PassThru -ArgumentList '-NoProfile','-NonInteractive','-Command','Start-Sleep -Seconds 30'; [Console]::WriteLine('READY:' + $PID + ':' + $worker.Id); Start-Sleep -Seconds 30".into(),
        ];
        let transfers = (0..2).map(|index| {
            let callback = callback.clone();
            let cancel = &cancel;
            let exe = &exe;
            let args = &args;
            let failed = &failed;
            async move {
                if index == 0 {
                    process::run(exe, args, None, cancel, Some(callback)).await?;
                    Ok(())
                } else {
                    let failed = failed.lock().unwrap().take().unwrap();
                    tokio::select! {
                        result = failed => result?,
                        _ = cancel.cancelled() => bail!("Operation interrupted"),
                    }
                    bail!("capture failed")
                }
            }
        });
        let captures = collect_transfers(transfers, true, &cancel);
        tokio::pin!(captures);
        // PowerShell cold startup is separate from the process cleanup deadline.
        tokio::select! {
            result = &mut captures => bail!("Capture ended before the fixture was ready: {result:?}"),
            ready = tokio::time::timeout(Duration::from_secs(30), started) => {
                if !matches!(ready, Ok(Ok(()))) {
                    cancel.cancel();
                    let _ = tokio::time::timeout(Duration::from_secs(5), &mut captures).await;
                    bail!("Supervised PowerShell fixture did not become ready: {ready:?}");
                }
            }
        }
        assert_eq!(pids.lock().unwrap().len(), 2);
        fail.send(()).unwrap();
        let result = tokio::time::timeout(Duration::from_secs(5), &mut captures)
            .await
            .context("Supervised captures did not exit within the cleanup deadline")?;
        assert_eq!(result.unwrap_err().to_string(), "capture failed");
        for pid in pids.lock().unwrap().iter().copied() {
            let handle = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) };
            if !handle.is_null() {
                let mut code = 259;
                let queried = unsafe { GetExitCodeProcess(handle, &mut code) };
                unsafe {
                    CloseHandle(handle);
                }
                assert_ne!(queried, 0);
                assert_ne!(code, 259, "Supervised process {pid} survived group failure");
            }
        }
        Ok(())
    }

    #[test]
    fn live_progress_sums_active_streams_without_percent_or_eta() {
        let mut task = crate::tests::paused_task(Default::default());
        let mut speeds = [None, None];
        let update = |speed| TransferUpdate {
            percent: Some(90.0),
            speed,
            eta: Some(1.0),
            downloaded: None,
            total: None,
            estimated: false,
        };
        update(Some(10.0)).live(&mut speeds, 0).apply(&mut task, 0);
        update(Some(20.0)).live(&mut speeds, 1).apply(&mut task, 1);
        assert_eq!(task.speed, Some(30.0));
        assert_eq!(task.stage_progress, None);
        assert_eq!(task.eta, None);
        assert!(task.stage_indeterminate);
        update(None).live(&mut speeds, 0).apply(&mut task, 0);
        assert_eq!(task.speed, Some(20.0));
        update(None).live(&mut speeds, 1).apply(&mut task, 1);
        assert_eq!(task.speed, None);
    }

    #[test]
    fn hls_protocol_does_not_make_on_demand_media_live() {
        assert!(!is_live(
            &json!({"is_live":false,"live_status":"not_live","formats":[{"protocol":"m3u8"}]})
        ));
        assert!(!is_live(
            &json!({"live_status":"was_live","formats":[{"protocol":"m3u8_native"}]})
        ));
        assert!(is_live(
            &json!({"is_live":true,"formats":[{"protocol":"m3u8_native"}]})
        ));
        assert!(is_live(
            &json!({"live_status":"is_live","formats":[{"protocol":"https"}]})
        ));
    }

    #[tokio::test]
    async fn sponsor_body_reads_obey_cancellation_timeout_and_size_limits() -> Result<()> {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        for cancel_body in [true, false] {
            let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
            let url = format!("http://{}/segments", listener.local_addr()?);
            let (headers_sent, headers_received) = tokio::sync::oneshot::channel();
            let server = tokio::spawn(async move {
                let (mut socket, _) = listener.accept().await.unwrap();
                let mut request = [0; 4096];
                assert!(socket.read(&mut request).await.unwrap() > 0);
                socket
                    .write_all(
                        b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\nConnection: close\r\n\r\n[",
                    )
                    .await
                    .unwrap();
                headers_sent.send(()).unwrap();
                std::future::pending::<()>().await;
            });
            let cancel = CancellationToken::new();
            let control = async {
                headers_received.await.unwrap();
                if cancel_body {
                    cancel.cancel();
                }
            };
            let (_, result) = tokio::join!(
                control,
                sponsor_response(
                    reqwest::Client::new().get(url),
                    &cancel,
                    Duration::from_millis(200),
                )
            );
            server.abort();
            let error = result.unwrap_err().to_string();
            assert!(error.contains(if cancel_body {
                "interrupted"
            } else {
                "timed out"
            }));
        }
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
        let url = format!("http://{}/segments", listener.local_addr()?);
        let server = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.unwrap();
            let mut request = [0; 4096];
            assert!(socket.read(&mut request).await.unwrap() > 0);
            socket
                .write_all(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 4194305\r\nConnection: close\r\n\r\n",
                )
                .await
                .unwrap();
        });
        let error = sponsor_response(
            reqwest::Client::new().get(url),
            &CancellationToken::new(),
            Duration::from_secs(1),
        )
        .await
        .unwrap_err();
        assert!(error.to_string().contains("too large"));
        server.await?;
        Ok(())
    }

    #[test]
    fn transfer_bytes_sum_streams_and_preserve_unknown_totals_and_resume_positions() {
        let mut task = crate::tests::paused_task(Default::default());
        task.transfer_bytes = vec![
            TransferBytes {
                format_id: "video".into(),
                downloaded: 0,
                total: Some(1000),
                estimated: false,
            },
            TransferBytes {
                format_id: "audio".into(),
                downloaded: 0,
                total: None,
                estimated: false,
            },
        ];
        let now = Instant::now();
        let mut video = TransferProgress::default();
        video.update("HU_PROGRESS:{\"status\":\"downloading\",\"downloaded_bytes\":400,\"total_bytes\":1000}", now).unwrap().apply(&mut task, 0);
        assert_eq!(task.public()["downloadedBytes"], 400);
        assert!(task.public()["totalBytes"].is_null());
        video
            .update(
                "HU_PROGRESS:{\"status\":\"finished\",\"downloaded_bytes\":1000}",
                now,
            )
            .unwrap()
            .apply(&mut task, 0);
        task.stage("downloading", "downloading_audio_2", None);
        assert_eq!(task.public()["downloadedBytes"], 1000);
        let mut audio = TransferProgress::default();
        audio.update("HU_PROGRESS:{\"status\":\"downloading\",\"downloaded_bytes\":50,\"total_bytes_estimate\":100}", now).unwrap().apply(&mut task, 1);
        assert_eq!(task.public()["downloadedBytes"], 1050);
        assert_eq!(task.public()["totalBytes"], 1100);
        assert_eq!(task.public()["totalBytesEstimated"], true);
        task.stage("paused", "paused", None);
        let mut resumed: Task =
            serde_json::from_value(serde_json::to_value(&task).unwrap()).unwrap();
        assert_eq!(resumed.public()["downloadedBytes"], 1050);
        TransferProgress::default().update("HU_PROGRESS:{\"status\":\"downloading\",\"downloaded_bytes\":50,\"total_bytes_estimate\":100}", now).unwrap().apply(&mut resumed, 1);
        assert_eq!(
            resumed.public()["downloadedBytes"],
            1050,
            "resume hooks contain absolute bytes, not deltas"
        );
        audio
            .update(
                "HU_PROGRESS:{\"status\":\"finished\",\"downloaded_bytes\":90}",
                now,
            )
            .unwrap()
            .apply(&mut resumed, 1);
        assert_eq!(resumed.public()["downloadedBytes"], 1090);
        assert_eq!(resumed.public()["totalBytes"], 1090);
        assert_eq!(resumed.public()["totalBytesEstimated"], false);
        assert!(resumed.public().get("transferBytes").is_none());
        let mut legacy = serde_json::to_value(&task).unwrap();
        legacy.as_object_mut().unwrap().remove("transferBytes");
        let legacy: Task = serde_json::from_value(legacy).unwrap();
        assert!(legacy.public()["downloadedBytes"].is_null());
        assert!(legacy.public()["totalBytes"].is_null());
    }

    #[test]
    fn transfer_bytes_live_updates_do_not_reset_other_streams_on_speed_expiry_or_restart() {
        let mut task = crate::tests::paused_task(Default::default());
        task.transfer_bytes = (0..2)
            .map(|index| TransferBytes {
                format_id: index.to_string(),
                downloaded: 0,
                total: None,
                estimated: false,
            })
            .collect();
        let now = Instant::now();
        let mut speeds = [None, None];
        for (index, bytes) in [(0, 100), (1, 20), (0, 50)] {
            TransferProgress::default()
                .update(
                    &format!(
                        "HU_PROGRESS:{}",
                        json!({"status":"downloading","downloaded_bytes":bytes})
                    ),
                    now,
                )
                .unwrap()
                .live(&mut speeds, index)
                .apply(&mut task, index);
        }
        assert_eq!(
            task.public()["downloadedBytes"],
            70,
            "a restarted stream replaces its previous absolute byte count"
        );
        TransferUpdate {
            percent: None,
            speed: None,
            eta: None,
            downloaded: None,
            total: None,
            estimated: false,
        }
        .live(&mut speeds, 0)
        .apply(&mut task, 0);
        assert_eq!(task.public()["downloadedBytes"], 70);
        assert!(task.public()["totalBytes"].is_null());
        for invalid in [
            json!(-1),
            json!(0.5),
            json!(1e40),
            json!("123"),
            Value::Null,
        ] {
            assert!(byte_count(&invalid).is_none());
        }
        assert_eq!(byte_count(&json!(u64::MAX)), Some(u64::MAX));
        assert_eq!(byte_count(&json!(123.0)), Some(123));
    }

    #[test]
    fn fragment_progress_uses_measured_throughput_instead_of_instantaneous_spikes() {
        let mut task = crate::tests::paused_task(Default::default());
        task.stage("downloading", "downloading_video_1", None);
        let now = Instant::now();
        let mib = 1024.0 * 1024.0;
        let mut tracker = TransferProgress::default();
        for (seconds, downloaded, raw_speed, raw_eta) in
            [(0, 0.0, 5.5, 37), (1, 10.0, 5.5, 37), (2, 20.0, 28.2, 3)]
        {
            let line = format!(
                "HU_PROGRESS:{}",
                json!({"status":"downloading","downloaded_bytes":downloaded*mib,"total_bytes":100.0*mib,"speed":raw_speed*mib,"eta":raw_eta})
            );
            tracker
                .update(&line, now + Duration::from_secs(seconds))
                .unwrap()
                .apply(&mut task, 0);
        }
        assert_eq!(task.stage_progress, Some(20.0));
        assert_eq!(task.speed, Some(10.0 * mib));
        assert_eq!(task.eta, Some(8.0));
    }

    #[test]
    fn transfer_progress_adapts_to_sustained_rate_changes_without_freezing_the_reading() {
        let now = Instant::now();
        let mut tracker = TransferProgress::default();
        for seconds in 0..=6 {
            let bytes = if seconds <= 1 {
                seconds * 10
            } else {
                10 + (seconds - 1) * 40
            };
            let line = format!(
                "HU_PROGRESS:{}",
                json!({"status":"downloading","downloaded_bytes":bytes,"total_bytes":1000,"speed":99999,"eta":0})
            );
            let update = tracker
                .update(&line, now + Duration::from_secs(seconds))
                .unwrap();
            if seconds == 1 {
                assert_eq!(update.speed, Some(10.0));
            }
            if seconds == 6 {
                assert_eq!(update.speed, Some(40.0));
                assert_eq!(update.eta, Some(19.75));
                assert_eq!(update.percent, Some(21.0));
            }
        }
    }

    #[test]
    fn transfer_progress_rebases_resumed_bytes_and_expires_stale_measurements() {
        let now = Instant::now();
        let mut tracker = TransferProgress::default();
        let line = |bytes| {
            format!(
                "HU_PROGRESS:{}",
                json!({"status":"downloading","downloaded_bytes":bytes,"total_bytes":1000,"speed":300,"eta":2})
            )
        };
        assert_eq!(tracker.update(&line(500), now).unwrap().speed, None);
        assert_eq!(
            tracker
                .update(&line(510), now + Duration::from_secs(1))
                .unwrap()
                .speed,
            Some(10.0)
        );
        let reset = tracker
            .update(&line(100), now + Duration::from_secs(2))
            .unwrap();
        assert_eq!(reset.speed, None);
        assert_eq!(reset.eta, None);
        assert_eq!(reset.percent, Some(10.0));
        assert_eq!(
            tracker
                .update(&line(120), now + Duration::from_secs(3))
                .unwrap()
                .speed,
            Some(20.0)
        );
        assert!(!tracker.expire(now + Duration::from_secs(7)));
        assert!(tracker.expire(now + Duration::from_secs(8)));
        assert!(!tracker.expire(now + Duration::from_secs(9)));
        assert_eq!(
            tracker
                .update(&line(130), now + Duration::from_secs(9))
                .unwrap()
                .speed,
            None
        );
        assert_eq!(
            tracker
                .update(&line(230), now + Duration::from_secs(15))
                .unwrap()
                .speed,
            None
        );
        // A new stream has a separate baseline, including when its first bytes came from a partial file.
        assert_eq!(
            TransferProgress::default()
                .update(&line(700), now)
                .unwrap()
                .speed,
            None
        );
    }

    #[test]
    fn transfer_progress_preserves_estimates_unknown_totals_and_finished_events() {
        let now = Instant::now();
        let mut tracker = TransferProgress::default();
        let line = |bytes, total| {
            format!(
                "HU_PROGRESS:{}",
                json!({"status":"downloading","downloaded_bytes":bytes,"total_bytes":null,"total_bytes_estimate":total})
            )
        };
        let first = tracker.update(&line(10, Some(100)), now).unwrap();
        assert_eq!(first.percent, Some(10.0));
        assert_eq!(first.speed, None);
        let unknown = tracker
            .update(&line(20, None), now + Duration::from_secs(1))
            .unwrap();
        assert_eq!(unknown.percent, None);
        assert_eq!(unknown.speed, Some(10.0));
        assert_eq!(unknown.eta, None);
        let finished = tracker.update("HU_PROGRESS:{\"status\":\"finished\",\"downloaded_bytes\":100,\"total_bytes\":100,\"speed\":500,\"eta\":0}", now + Duration::from_millis(1050)).unwrap();
        assert_eq!(finished.percent, Some(100.0));
        assert_eq!(finished.speed, None);
        assert_eq!(finished.eta, None);
        assert!(
            tracker
                .update(
                    "HU_PROGRESS:{\"status\":\"downloading\",\"downloaded_bytes\":-1}",
                    now
                )
                .is_none()
        );
        assert!(tracker.update("HU_PROGRESS:{invalid}", now).is_none());
        assert!(tracker.update("[download] ordinary output", now).is_none());
    }

    #[test]
    fn subtitles_prefer_manual_aliases_and_report_missing_languages() {
        let info = json!({"subtitles":{"zh":[{}],"en":[{}]},"automatic_captions":{"zh-Hans":[{}],"ja":[{}],"en":[{}],"live_chat":[{}]}});
        let (selected, missing) =
            subtitle_selection(&info, &["zh-Hans".into(), "ja".into(), "ko".into()], true);
        assert_eq!(selected.into_iter().collect::<Vec<_>>(), ["ja", "zh"]);
        assert_eq!(missing, ["ko"]);
        assert_eq!(subtitle_selection(&info, &["ja".into()], false).1, ["ja"]);
        let (selected, missing) = subtitle_selection(&info, &["all".into()], true);
        assert!(!selected.contains("live_chat"));
        assert!(missing.is_empty());
    }

    #[tokio::test]
    async fn missing_subtitles_require_a_decision_without_changing_the_task() -> Result<()> {
        let root = tempfile::tempdir()?;
        let app = App::open(crate::AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("output"),
            tool_dir: None,
        })
        .await?;
        let mut task = crate::tests::paused_task(Default::default());
        task.options.subtitle_language = "ja".into();
        app.db.insert_task(&task)?;
        let cookies = Cookies::new(&app, None)?;
        let mut files = Vec::new();
        download_subtitles(
            &app,
            &task,
            &json!({}),
            root.path(),
            &cookies,
            &CancellationToken::new(),
            &mut files,
        )
        .await?;
        assert!(files.is_empty());
        assert!(app.db.task(&task.id)?.warnings.is_empty());
        task.options.skip_unavailable_subtitles = false;
        let error = download_subtitles(
            &app,
            &task,
            &json!({}),
            root.path(),
            &cookies,
            &CancellationToken::new(),
            &mut vec![],
        )
        .await
        .unwrap_err();
        assert!(
            error
                .to_string()
                .contains("Requirements unmet: subtitles=ja")
        );
        let saved = app.db.task(&task.id)?;
        assert!(saved.warnings.is_empty());
        assert_eq!(saved.status, task.status);
        app.shutdown().await?;
        Ok(())
    }

    #[tokio::test]
    async fn login_cookies_reach_ytdlp_only_through_a_temporary_file() -> Result<()> {
        let root = tempfile::tempdir()?;
        let app = App::open(crate::AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("downloads"),
            tool_dir: None,
        })
        .await?;
        let text =
            "# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tTRUE\t0\tsid\tlogin-secret\n";
        let imported = app.import_login_cookies("Login", text)?;
        let options = DownloadOptions::parse(json!({"cookieId":imported["cookie"]["id"]}))?;
        let snapshot = app.credential_snapshot(&options, "https://www.example.com/video")?;
        let cookies = Cookies::new(&app, snapshot.as_deref())?;
        let args = base_args(&app, &options, &cookies, None).await?;
        let position = args.iter().position(|arg| arg == "--cookies").unwrap();
        let path = PathBuf::from(&args[position + 1]);
        assert_eq!(std::fs::read_to_string(&path)?, text);
        assert!(!args.join(" ").contains("login-secret"));
        let plugins = args.iter().position(|arg| arg == "--plugin-dirs").unwrap();
        assert_eq!(args[plugins - 1], "--no-plugin-dirs");
        let plugin_root = PathBuf::from(&args[plugins + 1]);
        assert_eq!(
            std::fs::read(plugin_root.join("huifa/yt_dlp_plugins/extractor/douyin.py"))?,
            include_bytes!("ytdlp_plugins/yt_dlp_plugins/extractor/douyin.py")
        );
        drop(cookies);
        assert!(!path.exists());
        assert!(!plugin_root.exists());
        app.shutdown().await?;
        Ok(())
    }

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
        let cookies = Cookies::new(&app, None)?;
        let mut task = crate::tests::paused_task(std::collections::HashMap::new());
        let args = base_args(&app, &task.options, &cookies, Some(&task)).await?;
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
            base_args(&app, &task.options, &cookies, Some(&task))
                .await
                .is_err(),
            "An invalid frozen Deno runtime was silently omitted"
        );
        app.shutdown().await?;
        Ok(())
    }
    #[tokio::test]
    async fn quickjs_uses_a_frozen_path_and_old_deno_tasks_keep_their_runtime() -> Result<()> {
        let root = tempfile::tempdir()?;
        let bundled = root.path().join("tools");
        std::fs::create_dir_all(&bundled)?;
        for name in ["yt-dlp", "ffmpeg", "ffprobe", "qjs"] {
            std::fs::write(
                bundled.join(format!("{name}{}", std::env::consts::EXE_SUFFIX)),
                format!("{name}:fixture"),
            )?;
        }
        let app = App::open(crate::AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("downloads"),
            tool_dir: Some(bundled.clone()),
        })
        .await?;
        let task = crate::tests::paused_task(crate::tools::freeze(&app)?);
        assert!(task.tools.contains_key("qjs") && !task.tools.contains_key("deno"));
        let args = base_args(&app, &task.options, &Cookies::new(&app, None)?, Some(&task)).await?;
        assert!(
            args.iter()
                .any(|arg| arg.starts_with("quickjs:") && arg.contains("frozen"))
        );
        std::fs::write(
            bundled.join(format!("deno{}", std::env::consts::EXE_SUFFIX)),
            b"deno:fixture",
        )?;
        let old = crate::tests::paused_task(crate::tools::freeze(&app)?);
        let args = base_args(&app, &old.options, &Cookies::new(&app, None)?, Some(&old)).await?;
        assert!(
            args.iter()
                .any(|arg| arg.starts_with("deno:") && arg.contains("frozen"))
        );
        let args = base_args(&app, &task.options, &Cookies::new(&app, None)?, Some(&task)).await?;
        assert!(
            args.iter()
                .any(|arg| arg.starts_with("quickjs:") && arg.contains("frozen"))
        );
        app.shutdown().await?;
        Ok(())
    }
    #[test]
    fn exact_plan_retains_all_audio_languages_and_requested_frame_rate() {
        let info = json!({"formats":[{"format_id":"v","height":1080,"fps":30,"vcodec":"avc1","acodec":"none"},{"format_id":"v-low","height":720,"fps":60,"vcodec":"avc1","acodec":"none"},{"format_id":"en-low","language":"en","abr":64,"vcodec":"none","acodec":"aac"},{"format_id":"en","language":"en","abr":128,"vcodec":"none","acodec":"aac"},{"format_id":"zh","language":"zh-Hans","abr":128,"vcodec":"none","acodec":"aac"}]});
        let options = DownloadOptions::parse(json!({"audioTrack":"all","videoFps":"60"})).unwrap();
        let formats = select_formats(&info, &options).unwrap();
        assert_eq!(
            formats
                .iter()
                .map(|f| text(f, "format_id"))
                .collect::<Vec<_>>(),
            vec!["v-low", "en", "zh"]
        );
        let options = DownloadOptions::parse(json!({"videoFormatId":"missing"})).unwrap();
        assert!(select_formats(&info, &options).is_err());
    }
    #[test]
    fn explicit_requirements_do_not_silently_fall_back() -> Result<()> {
        let info = json!({"formats":[
            {"format_id":"video","height":1080,"width":1920,"fps":30,"vcodec":"avc1","acodec":"none"},
            {"format_id":"audio","language":"en","vcodec":"none","acodec":"aac"}
        ]});
        for options in [
            json!({"quality":"4k"}),
            json!({"videoFps":"60"}),
            json!({"audioTrack":"ja"}),
            json!({"subtitleLanguage":"ja","skipUnavailableSubtitles":false}),
        ] {
            let options = DownloadOptions::parse(options)?;
            assert!(
                select_formats(&info, &options)
                    .unwrap_err()
                    .to_string()
                    .starts_with("Requirements unmet:")
            );
        }
        assert_eq!(select_formats(&info, &DownloadOptions::default())?.len(), 2);
        let optional_subtitles = DownloadOptions::parse(json!({"subtitleLanguage":"ja"}))?;
        assert_eq!(select_formats(&info, &optional_subtitles)?.len(), 2);
        let portrait = json!({"formats":[
            {"format_id":"video","height":1920,"width":1080,"fps":59.94,"vcodec":"avc1","acodec":"none"},
            {"format_id":"audio","language":"zh-CN","vcodec":"none","acodec":"aac"}
        ], "subtitles":{"zh-Hans":[{}]}});
        let options = DownloadOptions::parse(
            json!({"quality":"1080p","videoFps":"60","audioTrack":"zh-Hans","subtitleLanguage":"zh-Hans"}),
        )?;
        let selected = select_formats(&portrait, &options)?;
        assert_eq!(text(&selected[0], "format_id"), "video");
        assert_eq!(text(&selected[1], "language"), "zh-CN");
        Ok(())
    }
    #[test]
    fn explicit_quality_matches_displayed_cinematic_and_portrait_tiers() -> Result<()> {
        for (width, height, expected) in [
            (3840, 1600, "4k"),
            (1600, 3840, "4k"),
            (7680, 3200, "8k"),
            (1920, 1080, "1080p"),
        ] {
            let info = json!({"formats":[{"format_id":"video","width":width,"height":height,"fps":30,"vcodec":"h264","acodec":"aac"}]});
            let options = DownloadOptions::parse(json!({"quality":expected}))?;
            assert_eq!(select_formats(&info, &options)?.len(), 1);
            if expected != "4k" {
                assert!(
                    select_formats(&info, &DownloadOptions::parse(json!({"quality":"4k"}))?)
                        .is_err()
                );
            }
        }
        let too_narrow = json!({"formats":[{"format_id":"video","width":3840,"height":1599,"vcodec":"h264","acodec":"aac"}]});
        assert!(
            select_formats(
                &too_narrow,
                &DownloadOptions::parse(json!({"quality":"4k"}))?
            )
            .is_err()
        );
        Ok(())
    }
    #[test]
    fn metadata_retries_only_temporary_failures() {
        for message in [
            "HTTP Error 429: Too Many Requests",
            "HTTP Error 503: Service Unavailable",
            "Connection timed out",
            "Connection reset by peer",
            "getaddrinfo failed",
        ] {
            assert!(transient_metadata_error(message), "{message}");
        }
        for message in [
            "HTTP Error 403: Forbidden",
            "HTTP Error 404",
            "Fresh cookies are needed",
            "Requirements unmet: timeout subtitles",
            "Private video. Sign in",
            "Unsupported URL",
        ] {
            assert!(!transient_metadata_error(message), "{message}");
        }
    }
    #[test]
    fn stream_retries_only_temporary_transfer_failures() {
        for message in [
            "IncompleteRead: 65536 bytes read, 1709056 more expected",
            "65536 bytes read, 1709056 more expected. Giving up after 5 retries",
            "SSL: UNEXPECTED_EOF_WHILE_READING: EOF occurred in violation of protocol",
            "RemoteDisconnected('Remote end closed connection without response')",
            "Connection reset by peer",
            "HTTP Error 503: Service Unavailable",
            "Downloaded 120 bytes, expected 1000 bytes",
        ] {
            assert!(transient_stream_error(message), "{message}");
        }
        for message in [
            "HTTP Error 401: Unauthorized",
            "Connection reset; HTTP Error 403: Forbidden",
            "IncompleteRead; HTTP Error 404: Not Found",
            "Private video. Sign in",
            "Fresh cookies are needed",
            "Requested format is not available",
            "Connection reset; unable to write data: No space left on device",
            "Connection timed out; Permission denied",
            "SSL: CERTIFICATE_VERIFY_FAILED",
            "Cannot start yt-dlp",
        ] {
            assert!(!transient_stream_error(message), "{message}");
        }
    }
    #[tokio::test]
    async fn stream_process_has_bounded_retries_and_cancellable_backoff() -> Result<()> {
        let root = tempfile::tempdir()?;
        let app = App::open(crate::AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("downloads"),
            tool_dir: None,
        })
        .await?;
        #[cfg(windows)]
        let (executable, prefix) = {
            let script = root.path().join("stream.ps1");
            std::fs::write(
                &script,
                "Add-Content -LiteralPath $args[0] -Value attempt; [Console]::Error.WriteLine($args[1]); exit 1",
            )?;
            (
                PathBuf::from("powershell.exe"),
                vec![
                    "-NoProfile".into(),
                    "-NonInteractive".into(),
                    "-ExecutionPolicy".into(),
                    "Bypass".into(),
                    "-File".into(),
                    script.to_string_lossy().into(),
                ],
            )
        };
        #[cfg(not(windows))]
        let (executable, prefix) = {
            let script = root.path().join("stream.sh");
            std::fs::write(
                &script,
                "printf 'attempt\\n' >> \"$1\"; printf '%s\\n' \"$2\" >&2; exit 1",
            )?;
            (
                PathBuf::from("/bin/sh"),
                vec![script.to_string_lossy().into()],
            )
        };
        for (message, attempts, cancel_backoff) in [
            ("Connection reset by peer", 3, false),
            ("HTTP Error 403: Forbidden", 1, false),
            ("IncompleteRead: 20 bytes read, 100 more expected", 1, true),
        ] {
            let task = crate::tests::paused_task(Default::default());
            app.db.insert_task(&task)?;
            let counter = root.path().join(&task.id);
            let mut args = prefix.clone();
            args.extend([counter.to_string_lossy().into(), message.into()]);
            let cancel = CancellationToken::new();
            let result = stream_process(
                &app,
                &task,
                &executable,
                &args,
                root.path(),
                &cancel,
                Arc::new(|_, _| {}),
            );
            tokio::pin!(result);
            if cancel_backoff {
                tokio::select! {
                    _ = &mut result => bail!("Transfer ended before backoff cancellation"),
                    observed = tokio::time::timeout(Duration::from_secs(10), async {
                        loop {
                            if app.db.task_logs(&task.id)?.iter().any(|entry| entry.message.contains("retry 1/2")) {
                                return Ok::<_, anyhow::Error>(());
                            }
                            tokio::time::sleep(Duration::from_millis(10)).await;
                        }
                    }) => observed??,
                }
                cancel.cancel();
            }
            let error = tokio::time::timeout(Duration::from_secs(15), result)
                .await?
                .err()
                .context("Fixture must fail")?;
            if cancel_backoff {
                assert!(format!("{error:#}").contains("Operation interrupted"));
            }
            assert_eq!(std::fs::read_to_string(counter)?.lines().count(), attempts);
        }
        app.shutdown().await?;
        Ok(())
    }
    #[tokio::test]
    async fn metadata_process_retries_once_then_succeeds_and_obeys_cancellation() -> Result<()> {
        let root = tempfile::tempdir()?;
        let app = App::open(crate::AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("downloads"),
            tool_dir: None,
        })
        .await?;
        let task = crate::tests::paused_task(Default::default());
        app.db.insert_task(&task)?;
        let counter = root.path().join("attempts");
        #[cfg(windows)]
        let (executable, args) = {
            let script = root.path().join("metadata.ps1");
            std::fs::write(
                &script,
                "$count = if (Test-Path -LiteralPath $args[0]) { [int](Get-Content -LiteralPath $args[0]) } else { 0 }; $count++; [IO.File]::WriteAllText($args[0], [string]$count); [Console]::Error.WriteLine('Cookie: SID=metadata-log-canary'); if ($count -eq 1) { [Console]::Error.WriteLine('ERROR: HTTP Error 429'); exit 1 }; Write-Output '{\"ok\":true}'",
            )?;
            (
                PathBuf::from("powershell.exe"),
                vec![
                    "-NoProfile".into(),
                    "-NonInteractive".into(),
                    "-ExecutionPolicy".into(),
                    "Bypass".into(),
                    "-File".into(),
                    script.to_string_lossy().into(),
                    counter.to_string_lossy().into(),
                ],
            )
        };
        #[cfg(not(windows))]
        let (executable, args) = {
            let script = root.path().join("metadata.sh");
            std::fs::write(
                &script,
                "count=0; if [ -f \"$1\" ]; then count=$(cat \"$1\"); fi; count=$((count + 1)); printf %s \"$count\" > \"$1\"; echo 'Cookie: SID=metadata-log-canary' >&2; if [ \"$count\" -eq 1 ]; then echo 'ERROR: HTTP Error 429' >&2; exit 1; fi; echo '{\"ok\":true}'",
            )?;
            (
                PathBuf::from("/bin/sh"),
                vec![
                    script.to_string_lossy().into(),
                    counter.to_string_lossy().into(),
                ],
            )
        };
        let cancel = CancellationToken::new();
        let output =
            metadata_process(&app, &executable, &args, &cancel, false, Some(&task)).await?;
        assert!(output.stdout.contains("\"ok\":true"));
        assert!(!output.stderr.contains("metadata-log-canary"));
        assert_eq!(std::fs::read_to_string(&counter)?, "2");
        let logs = app.command("tasks.logs", json!({"id":task.id})).await?;
        let logs = logs["text"].as_str().unwrap();
        for expected in [
            "command",
            "stdout\n{\"ok\":true}",
            "stderr\nCookie: SID=metadata-log-canary",
            "ERROR: HTTP Error 429",
            "exit",
        ] {
            assert!(logs.contains(expected), "missing {expected}: {logs}");
        }
        cancel.cancel();
        assert!(
            metadata_process(&app, &executable, &args, &cancel, false, None)
                .await
                .is_err()
        );
        assert_eq!(std::fs::read_to_string(counter)?, "2");
        app.shutdown().await?;
        Ok(())
    }
    #[tokio::test]
    async fn ordinary_and_waiting_metadata_reject_a_single_65_mib_line() -> Result<()> {
        let root = tempfile::tempdir()?;
        let app = App::open(crate::AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("downloads"),
            tool_dir: None,
        })
        .await?;
        #[cfg(windows)]
        let (executable, args) = (
            PathBuf::from("powershell.exe"),
            vec!["-NoProfile".into(), "-NonInteractive".into(), "-Command".into(), "$data = [Text.Encoding]::ASCII.GetBytes(('x' * 1048576)); $output = [Console]::OpenStandardOutput(); for ($i=0; $i -lt 65; $i++) { $output.Write($data,0,$data.Length) }".into()],
        );
        #[cfg(not(windows))]
        let (executable, args) = (
            PathBuf::from("/bin/sh"),
            vec!["-c".into(), "head -c 68157440 /dev/zero".into()],
        );
        for wait_for_live in [false, true] {
            let error = tokio::time::timeout(
                Duration::from_secs(10),
                metadata_process(
                    &app,
                    &executable,
                    &args,
                    &CancellationToken::new(),
                    wait_for_live,
                    None,
                ),
            )
            .await?
            .err()
            .context("Oversized metadata must fail")?;
            assert!(format!("{error:#}").contains("byte limit"), "{error:#}");
        }
        app.shutdown().await?;
        Ok(())
    }
    #[test]
    fn all_audio_tracks_include_combined_sources_without_repeating_the_main_track() {
        let info = json!({"formats":[
            {"format_id":"main","height":1080,"vcodec":"avc1","acodec":"aac","language":"en","audio_track_id":"en-main"},
            {"format_id":"en-audio","vcodec":"none","acodec":"aac","language":"en","audio_track_id":"en-main","abr":128},
            {"format_id":"zh-low","height":360,"vcodec":"avc1","acodec":"aac","language":"zh-Hans","abr":64},
            {"format_id":"zh","height":720,"vcodec":"avc1","acodec":"aac","language":"zh-Hans","abr":128},
            {"format_id":"commentary","vcodec":"none","acodec":"aac","language":"en","audio_track_id":"en-commentary","abr":128}
        ]});
        let options = DownloadOptions::parse(json!({"audioTrack":"all"})).unwrap();
        let formats = select_formats(&info, &options).unwrap();
        assert_eq!(
            formats
                .iter()
                .map(|f| text(f, "format_id"))
                .collect::<Vec<_>>(),
            ["main", "commentary", "zh"]
        );
        let combined_only =
            json!({"formats":[info["formats"][0].clone(),info["formats"][3].clone()]});
        assert_eq!(
            select_formats(&combined_only, &options)
                .unwrap()
                .iter()
                .map(|f| text(f, "format_id"))
                .collect::<Vec<_>>(),
            ["main", "zh"]
        );
    }
    #[test]
    fn video_only_selection_keeps_audio_from_combined_formats() -> Result<()> {
        let info = json!({"formats":[
            {"format_id":"video","height":1080,"vcodec":"avc1","acodec":"none"},
            {"format_id":"combined-low","height":360,"vcodec":"avc1","acodec":"aac","language":"en","abr":64},
            {"format_id":"combined","height":720,"vcodec":"avc1","acodec":"aac","language":"en","abr":128}
        ]});
        for options in [
            DownloadOptions::default(),
            DownloadOptions::parse(json!({"quality":"1080p","audioTrack":"en"}))?,
        ] {
            let formats = select_formats(&info, &options)?;
            assert_eq!(
                formats
                    .iter()
                    .map(|format| text(format, "format_id"))
                    .collect::<Vec<_>>(),
                ["video", "combined"]
            );
        }
        let combined = json!({"formats":[info["formats"][2].clone()]});
        assert_eq!(
            select_formats(&combined, &DownloadOptions::default())?.len(),
            1
        );
        Ok(())
    }
    #[test]
    fn automatic_quality_selects_real_video_extremes_and_keeps_audio() {
        let info = json!({"formats":[
            {"format_id":"storyboard","height":45,"vcodec":"images","acodec":"none"},
            {"format_id":"preview","height":45,"protocol":"mhtml","acodec":"none"},
            {"format_id":"unknown","vcodec":"avc1","acodec":"none"},
            {"format_id":"low-large","height":144,"fps":30,"tbr":100,"vcodec":"avc1","acodec":"none"},
            {"format_id":"low","height":144,"fps":30,"tbr":50,"vcodec":"avc1","acodec":"none"},
            {"format_id":"medium","height":720,"fps":60,"vcodec":"avc1","acodec":"none"},
            {"format_id":"high","height":1080,"fps":30,"vcodec":"avc1","acodec":"none"},
            {"format_id":"audio-low","abr":64,"vcodec":"none","acodec":"aac"},
            {"format_id":"audio","abr":128,"vcodec":"none","acodec":"aac"}
        ]});
        for (quality, expected) in [("best", "high"), ("worst", "low"), ("720p", "medium")] {
            let options = DownloadOptions::parse(json!({"quality":quality})).unwrap();
            let formats = select_formats(&info, &options).unwrap();
            assert_eq!(text(&formats[0], "format_id"), expected);
            assert_eq!(text(&formats[1], "format_id"), "audio");
        }
        let exact =
            DownloadOptions::parse(json!({"quality":"worst","videoFormatId":"high"})).unwrap();
        assert_eq!(
            text(&select_formats(&info, &exact).unwrap()[0], "format_id"),
            "high"
        );
        let previews = json!({"formats":[info["formats"][0].clone(),info["formats"][1].clone()]});
        assert!(select_formats(&previews, &DownloadOptions::default()).is_err());
        let unknown = json!({"formats":[info["formats"][2].clone(),info["formats"][8].clone()]});
        let lowest = DownloadOptions::parse(json!({"quality":"worst"})).unwrap();
        assert_eq!(
            text(&select_formats(&unknown, &lowest).unwrap()[0], "format_id"),
            "unknown"
        );
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

use crate::{
    App, DownloadOptions,
    download::{hash_file, write_sync},
    model::{
        AuxiliaryPublication, Chapter, GeneratedFile, Media, Publication, SourceFile,
        SourceManifest, Task, parse_time, text,
    },
    process::{self, LineCallback},
};
use anyhow::{Context, Result, bail, ensure};
use serde_json::{Value, json};
use std::{
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};
use tokio_util::sync::CancellationToken;

#[derive(Clone)]
struct Input {
    path: PathBuf,
    kind: String,
    language: Option<String>,
    probe: Value,
}

pub(crate) fn workspace(app: &App, task: &Task) -> PathBuf {
    if task.options.processing_temp_dir.is_empty() {
        app.config
            .data_dir
            .join("tasks")
            .join(&task.id)
            .join("processing")
    } else {
        PathBuf::from(&task.options.processing_temp_dir).join(format!("HuifaRust-{}", task.id))
    }
}

pub(crate) async fn queue_conversion(app: &App, id: &str, encoder: &str) -> Result<Value> {
    let mut task = app.db.task(id)?;
    ensure!(
        matches!(task.status.as_str(), "completed" | "retained"),
        "Only completed tasks can be converted"
    );
    let media = app
        .db
        .media_for_task(id)?
        .context("Completed media does not exist")?;
    let mut options = task.options.clone();
    options.encoder = encoder.into();
    options.validate()?;
    ensure!(
        !matches!(encoder, "copy" | "original"),
        "Choose a video encoder for conversion"
    );
    let path = app.owned_path(&media.path)?;
    let (hash, size) = hash_file(&path)?;
    let metadata = task
        .manifest
        .as_ref()
        .map(|m| m.metadata.clone())
        .unwrap_or(json!({"title":media.title}));
    options.section_start.clear();
    options.section_end.clear();
    options.sponsorblock_mode = "off".into();
    options.prepend_cover_enabled = false;
    options.split_chapters = false;
    options.embed_subtitles = false;
    options.embed_thumbnail = false;
    options.write_thumbnail = false;
    options.write_info_json = false;
    options.write_description = false;
    options.audio_format = "best".into();
    options.content_mode = "video".into();
    task.options = options;
    task.conversion = true;
    task.source_info = task.media_info.take();
    task.size = None;
    task.steps.clear();
    task.step_index = None;
    task.publication = None;
    task.error = None;
    task.manifest = Some(SourceManifest {
        version: 1,
        directory: path.parent().unwrap().to_path_buf(),
        files: vec![SourceFile {
            path: path.clone(),
            hash,
            size,
            kind: "video".into(),
            format_id: "local".into(),
            language: None,
        }],
        metadata,
        duration: Some(media.duration),
        chapters: task
            .manifest
            .as_ref()
            .map(|m| m.chapters.clone())
            .unwrap_or_default(),
        sponsor_segments: vec![],
        output_extension: path
            .extension()
            .context("Media has no extension")?
            .to_string_lossy()
            .into(),
    });
    task.stage("waiting_processing", "waiting_processing", None);
    app.save_task(&task)?;
    app.schedule(id)?;
    Ok(task.public())
}

pub(crate) async fn queue_conversion_options(
    app: &App,
    id: &str,
    changes: &Value,
) -> Result<Value> {
    queue_conversion_prepared(app, id, changes, None).await
}

pub(crate) async fn preview_conversion(
    app: &App,
    id: &str,
    changes: &Value,
    strategy: &str,
) -> Result<Value> {
    let task = app.db.task(id)?;
    ensure!(
        matches!(task.status.as_str(), "completed" | "retained") && task.kind == "media",
        "Only completed media can be converted"
    );
    let media = app
        .db
        .media_for_task(id)?
        .context("Completed media does not exist")?;
    let path = app.owned_path(&media.path)?;
    let container = changes
        .get("container")
        .and_then(Value::as_str)
        .filter(|value| *value != "auto")
        .unwrap_or_else(|| {
            path.extension()
                .and_then(|value| value.to_str())
                .unwrap_or("mkv")
        });
    let probe = probe_file(app, &task, &path, &app.shutdown_token).await?;
    conversion_plan(&probe, container, strategy, text(changes, "encoder"))
}

pub(crate) async fn queue_conversion_options_with_strategy(
    app: &App,
    id: &str,
    changes: &Value,
    strategy: &str,
) -> Result<Value> {
    let plan = preview_conversion(app, id, changes, strategy).await?;
    queue_conversion_prepared(app, id, changes, Some(&plan)).await
}

fn conversion_plan(probe: &Value, container: &str, strategy: &str, encoder: &str) -> Result<Value> {
    ensure!(
        matches!(container, "mp4" | "mkv" | "mov" | "webm"),
        "Unsupported conversion container: {container}"
    );
    ensure!(
        matches!(strategy, "auto" | "remux" | "transcode"),
        "Invalid conversion strategy: {strategy}"
    );
    let compatible = streams(probe)
        .filter(|stream| matches!(text(stream, "codec_type"), "video" | "audio"))
        .all(|stream| {
            container_accepts(
                container,
                text(stream, "codec_type"),
                text(stream, "codec_name"),
            )
        });
    ensure!(
        strategy != "remux" || compatible,
        "The source codecs cannot be copied into {container}; choose automatic conversion or re-encoding"
    );
    let transcode = strategy == "transcode" || !compatible;
    let encoder = if !transcode {
        "copy"
    } else if strategy != "transcode" || matches!(encoder, "" | "copy" | "original") {
        if container == "webm" {
            "libvpx-vp9"
        } else {
            "libx264"
        }
    } else {
        encoder
    };
    let codec = if encoder == "libvpx-vp9" {
        "vp9"
    } else if encoder.contains("264") {
        "h264"
    } else if encoder.contains("265") || encoder.starts_with("hevc_") {
        "hevc"
    } else if encoder.contains("av1") {
        "av1"
    } else {
        encoder
    };
    ensure!(
        !transcode || container_accepts(container, "video", codec),
        "The selected video encoder is incompatible with {container}"
    );
    let audio = if !transcode {
        "copy"
    } else if container == "webm" {
        "libopus"
    } else {
        "aac"
    };
    Ok(
        json!({"strategy":if transcode {"transcode"} else {"remux"}, "container":container,"encoder":encoder,"audioEncoder":audio,
        "reason":if transcode { if compatible {"Re-encoding was selected"} else {"The source codecs are incompatible with the target container"} } else {"The source codecs can be copied without quality loss"}}),
    )
}

fn container_accepts(container: &str, kind: &str, codec: &str) -> bool {
    match (container, kind) {
        ("mkv", _) => !matches!(codec, "" | "unknown" | "none"),
        ("webm", "video") => matches!(codec, "vp8" | "vp9" | "av1"),
        ("webm", "audio") => matches!(codec, "vorbis" | "opus"),
        ("mp4" | "mov", "video") => matches!(codec, "h264" | "hevc" | "av1" | "mpeg4" | "mjpeg"),
        ("mp4" | "mov", "audio") => matches!(codec, "aac" | "alac" | "mp3"),
        _ => false,
    }
}

async fn queue_conversion_prepared(
    app: &App,
    id: &str,
    changes: &Value,
    plan: Option<&Value>,
) -> Result<Value> {
    let original = app.db.task(id)?;
    ensure!(
        matches!(original.status.as_str(), "completed" | "retained") && original.kind == "media",
        "Only completed media can be converted"
    );
    let media = app
        .db
        .media_for_task(id)?
        .context("Completed media does not exist")?;
    let changes = changes
        .as_object()
        .context("Conversion options must be an object")?;
    ensure!(!changes.is_empty(), "Choose at least one conversion option");
    let mut options = original.options.clone();
    options.encoder = "copy".into();
    options.content_mode = "video".into();
    options.audio_format = "best".into();
    options.container = "auto".into();
    options.section_start.clear();
    options.section_end.clear();
    options.sponsorblock_mode = "off".into();
    options.prepend_cover_enabled = false;
    options.split_chapters = false;
    options.embed_subtitles = false;
    options.embed_thumbnail = false;
    options.embed_metadata = false;
    options.embed_chapters = false;
    options.write_thumbnail = false;
    options.write_info_json = false;
    options.write_description = false;
    options.write_comments = false;
    let mut raw = serde_json::to_value(&options)?;
    for (key, value) in changes {
        ensure!(
            matches!(
                key.as_str(),
                "contentMode"
                    | "audioFormat"
                    | "container"
                    | "encoder"
                    | "embedSubtitles"
                    | "embedThumbnail"
                    | "embedMetadata"
                    | "embedChapters"
                    | "subtitleFormat"
            ),
            "Unsupported conversion option: {key}"
        );
        raw[key] = value.clone();
    }
    if let Some(plan) = plan {
        raw["container"] = plan["container"].clone();
        raw["encoder"] = plan["encoder"].clone();
    }
    if raw["embedSubtitles"].as_bool() == Some(true) && options.languages().is_empty() {
        raw["subtitleLanguage"] = json!("all");
    }
    let options = DownloadOptions::parse(raw)?;
    ensure!(
        matches!(options.content_mode.as_str(), "video" | "audio"),
        "Conversion contentMode must be video or audio"
    );
    let path = app.owned_path(&media.path)?;
    let (hash, size) = hash_file(&path)?;
    let mut files = vec![SourceFile {
        path: path.clone(),
        hash,
        size,
        kind: "video".into(),
        format_id: "local".into(),
        language: None,
    }];
    for asset in &media.assets {
        let kind = conversion_asset_kind(asset);
        if !(kind == "subtitle" && options.embed_subtitles
            || kind == "thumbnail" && options.embed_thumbnail)
        {
            continue;
        }
        let owned = app.owned_path(asset)?;
        let (hash, size) = hash_file(&owned)?;
        files.push(SourceFile {
            path: owned,
            hash,
            size,
            kind: kind.into(),
            format_id: String::new(),
            language: asset
                .file_stem()
                .and_then(|s| s.to_str())
                .and_then(|stem| stem.rsplit('.').next())
                .map(str::to_string),
        });
    }
    ensure!(
        !options.embed_subtitles || files.iter().any(|f| f.kind == "subtitle"),
        "No saved subtitles are available; download subtitles before embedding them"
    );
    ensure!(
        !options.embed_thumbnail || files.iter().any(|f| f.kind == "thumbnail"),
        "No saved thumbnail is available; download a thumbnail before embedding it"
    );
    let mut metadata = original
        .manifest
        .as_ref()
        .map(|m| m.metadata.clone())
        .unwrap_or(Value::Null);
    ensure!(
        !options.embed_metadata || metadata.is_object(),
        "Saved metadata is unavailable for this media"
    );
    if let Some(object) = metadata.as_object_mut() {
        object.remove("_conversionAudioEncoder");
    }
    if let Some(plan) = plan {
        if !metadata.is_object() {
            metadata = json!({"title":media.title});
        }
        metadata["_conversionAudioEncoder"] = plan["audioEncoder"].clone();
    }
    let chapters = original
        .manifest
        .as_ref()
        .map(|m| m.chapters.clone())
        .unwrap_or_default();
    ensure!(
        !options.embed_chapters || !chapters.is_empty(),
        "No saved chapters are available for this media"
    );
    let extension = if options.content_mode == "audio" {
        crate::download::output_extension(
            &options,
            &[json!({"ext":"mka","acodec":"unknown","vcodec":"none"})],
        )
    } else if options.effective_container() != "auto" {
        options.effective_container().into()
    } else {
        path.extension()
            .and_then(|v| v.to_str())
            .context("Media has no extension")?
            .into()
    };
    let mut task = original;
    task.id = uuid::Uuid::new_v4().to_string();
    task.thumbnail = None;
    task.source_info = task.media_info.take();
    task.size = None;
    task.created_at = chrono::Utc::now().to_rfc3339();
    task.options = options;
    task.conversion = true;
    task.previous_outputs.clear();
    task.steps.clear();
    task.step_index = None;
    task.files.clear();
    task.warnings.clear();
    task.publication = None;
    task.error = None;
    task.cookie_snapshot = None;
    task.proxy_snapshot = None;
    task.output_path = Some(path.with_file_name(format!(
        "{} [converted-{}].{}",
        path.file_stem().unwrap().to_string_lossy(),
        &task.id[..8],
        extension
    )));
    task.manifest = Some(SourceManifest {
        version: 1,
        directory: path.parent().unwrap().to_path_buf(),
        files,
        metadata,
        duration: Some(media.duration),
        chapters,
        sponsor_segments: vec![],
        output_extension: extension,
    });
    task.stage("waiting_processing", "waiting_processing", None);
    app.db.insert_task(&task)?;
    app.emit("task.upsert", task.public());
    app.schedule(&task.id)?;
    Ok(task.public())
}

fn conversion_asset_kind(path: &Path) -> &str {
    match path.extension().and_then(|v| v.to_str()).unwrap_or("") {
        "srt" | "vtt" | "ass" | "ssa" | "ttml" | "lrc" => "subtitle",
        "jpg" | "jpeg" | "png" | "webp" => "thumbnail",
        _ => "other",
    }
}

pub(crate) async fn process_task(
    app: &Arc<App>,
    id: &str,
    cancel: &CancellationToken,
) -> Result<()> {
    let mut task = app.db.task(id)?;
    if task.steps.is_empty() && task.status != "completed" {
        crate::workflow::plan(&mut task);
        app.save_task(&task)?;
    }
    if task.publication.is_some() {
        return publish(app, task, cancel).await;
    }
    let manifest = task
        .manifest
        .as_ref()
        .context("Local source manifest is missing")?;
    ensure!(manifest.version == 1, "Unsupported source manifest version");
    app.set_stage(id, "processing", "verifying_sources", None)?;
    for source in &manifest.files {
        if cancel.is_cancelled() {
            bail!("Operation interrupted");
        }
        crate::reject_links(&source.path)?;
        if task.conversion {
            app.owned_path(&source.path)?;
        } else {
            ensure!(
                source
                    .path
                    .canonicalize()?
                    .starts_with(app.config.data_dir.join("tasks").join(id).canonicalize()?),
                "Source manifest contains a file outside its task workspace"
            );
        }
        let (hash, size) = hash_file(&source.path)?;
        ensure!(
            hash == source.hash && size == source.size,
            "Saved source {} changed; local processing cannot continue",
            source
                .path
                .file_name()
                .unwrap_or_default()
                .to_string_lossy()
        );
    }
    if task.kind == "assets" {
        return prepare_assets(app, task, cancel).await;
    }
    let workspace = workspace(app, &task);
    crate::reject_links(&workspace)?;
    std::fs::create_dir_all(&workspace)?;
    let mut inputs = Vec::new();
    for source in manifest
        .files
        .iter()
        .filter(|s| s.kind == "video" || s.kind == "audio")
    {
        let probe = probe_file(app, &task, &source.path, cancel).await?;
        inputs.push(Input {
            path: source.path.clone(),
            kind: source.kind.clone(),
            language: source.language.clone(),
            probe,
        });
    }
    ensure!(!inputs.is_empty(), "Source manifest has no media inputs");
    let source_size = manifest
        .files
        .iter()
        .filter(|s| matches!(s.kind.as_str(), "video" | "audio"))
        .map(|s| s.size)
        .sum();
    let source_probe = json!({"streams": inputs.iter().flat_map(|input| input.probe["streams"].as_array().into_iter().flatten().cloned()).collect::<Vec<_>>(),"format":{"duration":manifest.duration.map(|d|d.to_string())}});
    let mut current = app.db.task(id)?;
    current.source_info = Some(crate::model::MediaInfo::from_probe(
        &source_probe,
        Some(source_size),
        None,
    ));
    if let Some(info) = current.source_info.as_mut() {
        info.container = task
            .source_info
            .as_ref()
            .and_then(|source| source.container.clone());
    }
    app.save_task(&current)?;
    let audio_mode = task.options.content_mode == "audio";
    let video = if audio_mode {
        None
    } else {
        inputs
            .iter()
            .position(|i| count_streams(&i.probe, "video") > 0)
    };
    ensure!(audio_mode || video.is_some(), "No video stream was saved");
    let mut audio = Vec::new();
    let separate = inputs.iter().any(|i| i.kind == "audio");
    for (index, input) in inputs.iter().enumerate() {
        if separate && input.kind != "audio" && task.options.audio_track != "all" {
            continue;
        }
        for stream in streams(&input.probe).filter(|s| text(s, "codec_type") == "audio") {
            audio.push((
                index,
                stream["index"].as_u64().unwrap_or(0),
                input
                    .language
                    .clone()
                    .or_else(|| stream["tags"]["language"].as_str().map(str::to_string)),
            ));
        }
    }
    if audio_mode {
        ensure!(!audio.is_empty(), "Saved media has no audio");
    }
    let duration = inputs
        .iter()
        .filter_map(|i| probe_duration(&i.probe))
        .chain(manifest.duration)
        .reduce(f64::max)
        .context("Media duration is unavailable")?;
    let ranges = keep_ranges(duration, &task.options, &manifest.sponsor_segments)?;
    let covered = ranges.iter().map(|(start, end)| end - start).sum::<f64>();
    let cuts = ranges.len() != 1 || ranges[0].0 > 0.001 || (ranges[0].1 - duration).abs() > 0.001;
    let thumbnail = prepare_thumbnail(&task, &workspace)?;
    if task.options.prepend_cover_enabled {
        ensure!(!audio_mode, "A prepend cover requires video output");
        ensure!(
            thumbnail.is_some(),
            "A prepend cover requires a saved thumbnail"
        );
    }
    let fps = video
        .and_then(|i| {
            streams(&inputs[i].probe)
                .find(|s| text(s, "codec_type") == "video")
                .and_then(|s| parse_fraction(text(s, "avg_frame_rate")))
        })
        .filter(|f| *f > 0.0)
        .unwrap_or(30.0);
    let cover_duration = if task.options.prepend_cover_enabled {
        task.options.prepend_cover_frames as f64 / fps
    } else {
        0.0
    };
    let intro = if cover_duration > 0.0 {
        let stream = streams(&inputs[video.unwrap()].probe)
            .find(|s| text(s, "codec_type") == "video")
            .unwrap();
        let width = stream["width"].as_u64().context("Video width is missing")?;
        let height = stream["height"]
            .as_u64()
            .context("Video height is missing")?;
        let path = workspace.join("cover-intro.mkv");
        let mut cover_args = vec![
            "-hide_banner".into(),
            "-loglevel".into(),
            "error".into(),
            "-nostdin".into(),
            "-y".into(),
            "-loop".into(),
            "1".into(),
            "-framerate".into(),
            fps.to_string(),
            "-i".into(),
            thumbnail.as_ref().unwrap().to_string_lossy().into(),
        ];
        for (input, stream, _) in &audio {
            let probe = streams(&inputs[*input].probe)
                .find(|s| s["index"].as_u64() == Some(*stream))
                .unwrap();
            let rate = text(probe, "sample_rate").parse::<u64>().unwrap_or(48000);
            let layout = text(probe, "channel_layout");
            let channels = probe["channels"].as_u64().unwrap_or(2);
            let layout = if layout.is_empty() {
                if channels == 1 { "mono" } else { "stereo" }
            } else {
                layout
            };
            cover_args.extend([
                "-f".into(),
                "lavfi".into(),
                "-i".into(),
                format!("anullsrc=r={rate}:cl={layout}:d={cover_duration}"),
            ]);
        }
        cover_args.extend(["-map".into(), "0:v:0".into()]);
        for track in 0..audio.len() {
            cover_args.extend(["-map".into(), format!("{}:a:0", track + 1)]);
        }
        cover_args.extend(["-vf".into(),format!("scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1"),"-t".into(),cover_duration.to_string(),"-r".into(),fps.to_string(),"-frames:v".into(),task.options.prepend_cover_frames.to_string(),"-c:v".into(),"ffv1".into(),"-pix_fmt".into(),"yuv420p".into(),"-c:a".into(),"pcm_s16le".into(),"-threads".into(),"2".into(),"-progress".into(),"pipe:1".into(),path.to_string_lossy().into()]);
        app.set_stage(id, "processing", "creating_cover_intro", None)?;
        run_ffmpeg(app, id, &cover_args, cover_duration, cancel).await?;
        Some(path)
    } else {
        None
    };
    let mut final_chapters = retime_chapters(&manifest.chapters, &ranges, cover_duration);
    if task.options.sponsorblock_mode == "mark" {
        final_chapters.extend(retime_chapters(
            &manifest.sponsor_segments,
            &ranges,
            cover_duration,
        ));
    }
    if cover_duration > 0.0 {
        final_chapters.push(Chapter {
            start: 0.0,
            end: cover_duration,
            title: "Cover".into(),
        });
    }
    final_chapters.sort_by(|a, b| a.start.total_cmp(&b.start));
    if task.options.split_chapters {
        ensure!(
            !final_chapters.is_empty(),
            "Chapter splitting was requested but this video has no chapters"
        );
    }
    let subtitles = prepare_subtitles(
        app,
        &task,
        &workspace,
        &ranges,
        cuts,
        cover_duration,
        cancel,
    )
    .await?;
    let extension = &manifest.output_extension;
    let filtering = cuts || intro.is_some();
    // Sparse subtitle inputs can block FFmpeg's filter scheduler; mux them after the finite media transform.
    let deferred_mux = filtering
        && (task.options.embed_subtitles
            || task.options.embed_thumbnail
            || task.options.embed_chapters
            || task.options.sponsorblock_mode == "mark");
    if task.options.embed_subtitles {
        ensure!(
            matches!(extension.as_str(), "mp4" | "mkv" | "mov"),
            "Embedded subtitles require MP4 or MKV output"
        );
    }
    if task.options.embed_thumbnail {
        ensure!(
            matches!(
                extension.as_str(),
                "mp4" | "m4a" | "mp3" | "mkv" | "mka" | "flac"
            ),
            "Embedded thumbnails are unsupported in {extension}; choose MP4, MKV, M4A, MP3 or FLAC"
        );
    }
    let output = workspace.join(format!("prepared.{extension}"));
    if output.exists() {
        std::fs::remove_file(&output)?;
    }
    let encoded_output = if deferred_mux {
        workspace.join(format!("prepared-base.{extension}"))
    } else {
        output.clone()
    };
    let mut args = vec![
        "-hide_banner".into(),
        "-loglevel".into(),
        "error".into(),
        "-nostdin".into(),
        "-y".into(),
        "-filter_complex_threads".into(),
        "1".into(),
    ];
    for input in &inputs {
        args.extend(["-i".into(), input.path.to_string_lossy().into()]);
    }
    let mut subtitle_indices = Vec::new();
    if task.options.embed_subtitles && !deferred_mux {
        for (path, language) in &subtitles {
            let index = inputs.len() + subtitle_indices.len();
            args.extend(["-i".into(), path.to_string_lossy().into()]);
            subtitle_indices.push((index, language.clone()));
        }
    }
    let mut next_input = inputs.len() + subtitle_indices.len();
    let chapter_index = if (task.options.embed_chapters || task.options.sponsorblock_mode == "mark")
        && !deferred_mux
    {
        let path = workspace.join("chapters.ffmetadata");
        write_sync(&path, ffmetadata(&final_chapters).as_bytes())?;
        args.extend([
            "-f".into(),
            "ffmetadata".into(),
            "-i".into(),
            path.to_string_lossy().into(),
        ]);
        let index = next_input;
        next_input += 1;
        Some(index)
    } else {
        None
    };
    let cover_index = if task.options.prepend_cover_enabled {
        args.extend([
            "-i".into(),
            intro.as_ref().unwrap().to_string_lossy().into(),
        ]);
        let index = next_input;
        next_input += 1;
        Some(index)
    } else {
        None
    };
    let embedded_cover_index = if task.options.embed_thumbnail
        && !deferred_mux
        && !matches!(extension.as_str(), "mkv" | "mka")
    {
        args.extend([
            "-i".into(),
            thumbnail
                .as_ref()
                .context("Embedding a cover requires a saved thumbnail")?
                .to_string_lossy()
                .into(),
        ]);
        Some(next_input)
    } else {
        None
    };
    if filtering {
        let mut filters = Vec::new();
        let mut segments = Vec::new();
        for (segment, (start, end)) in ranges.iter().enumerate() {
            let mut labels = String::new();
            if let Some(video) = video {
                let label = format!("v{segment}");
                filters.push(format!(
                    "[{video}:v:0]trim=start={start}:end={end},setpts=PTS-STARTPTS[{label}]"
                ));
                labels.push_str(&format!("[{label}]"));
            }
            for (track, (input, stream, _)) in audio.iter().enumerate() {
                let label = format!("a{segment}_{track}");
                filters.push(format!(
                    "[{input}:{stream}]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[{label}]"
                ));
                labels.push_str(&format!("[{label}]"));
            }
            segments.push(labels);
        }
        let video_label = if video.is_some() {
            Some("vmain".to_string())
        } else {
            None
        };
        let audio_labels = (0..audio.len())
            .map(|i| format!("amain{i}"))
            .collect::<Vec<_>>();
        let outputs = video_label
            .iter()
            .chain(&audio_labels)
            .map(|l| format!("[{l}]"))
            .collect::<String>();
        filters.push(format!(
            "{}concat=n={}:v={}:a={}{}",
            segments.join(""),
            ranges.len(),
            usize::from(video.is_some()),
            audio.len(),
            outputs
        ));
        if let Some(cover) = cover_index {
            filters.push(format!(
                "[{cover}:v:0]setsar=1,fps={fps},setpts=PTS-STARTPTS[vcover]"
            ));
            filters.push(format!("[vmain]setsar=1,fps={fps}[vnorm]"));
            let mut silent = String::new();
            let mut main = String::new();
            for track in 0..audio.len() {
                filters.push(format!(
                    "[{cover}:a:{track}]asetpts=PTS-STARTPTS[silent{track}]"
                ));
                silent.push_str(&format!("[silent{track}]"));
                main.push_str(&format!("[amain{track}]"));
            }
            let out = (0..audio.len())
                .map(|i| format!("[aout{i}]"))
                .collect::<String>();
            filters.push(format!(
                "[vcover]{silent}[vnorm]{main}concat=n=2:v=1:a={}[vout]{out}",
                audio.len()
            ));
            args.extend([
                "-filter_complex".into(),
                filters.join(";"),
                "-map".into(),
                "[vout]".into(),
            ]);
            for track in 0..audio.len() {
                args.extend(["-map".into(), format!("[aout{track}]")]);
            }
        } else {
            args.extend(["-filter_complex".into(), filters.join(";")]);
            for label in video_label.iter().chain(&audio_labels) {
                args.extend(["-map".into(), format!("[{label}]")]);
            }
        }
    } else {
        if let Some(video) = video {
            args.extend(["-map".into(), format!("{video}:v:0")]);
        }
        for (input, stream, _) in &audio {
            args.extend(["-map".into(), format!("{input}:{stream}")]);
        }
    }
    for (index, language) in &subtitle_indices {
        args.extend(["-map".into(), format!("{index}:s:0")]);
        if !language.is_empty() {
            args.extend([
                format!("-metadata:s:s:{}", index - inputs.len()),
                format!("language={language}"),
            ]);
        }
    }
    if let Some(index) = chapter_index {
        args.extend(["-map_chapters".into(), index.to_string()]);
        set_chapter_metadata(&mut args, &final_chapters);
    } else {
        args.extend([
            "-map_chapters".into(),
            if task.conversion {
                "0".into()
            } else {
                "-1".into()
            },
        ]);
    }
    args.extend([
        "-map_metadata".into(),
        if task.conversion {
            "0".into()
        } else {
            "-1".into()
        },
    ]);
    if task.options.embed_metadata {
        for (key, value) in [
            ("title", text(&manifest.metadata, "title")),
            ("artist", text(&manifest.metadata, "uploader")),
            ("date", text(&manifest.metadata, "upload_date")),
            ("comment", text(&manifest.metadata, "description")),
        ] {
            if !value.is_empty() {
                args.extend(["-metadata".into(), format!("{key}={value}")]);
            }
        }
    }
    for (index, (_, _, language)) in audio.iter().enumerate() {
        if let Some(language) = language {
            args.extend([
                format!("-metadata:s:a:{index}"),
                format!("language={language}"),
            ]);
        }
    }
    let encoder = if matches!(task.options.encoder.as_str(), "copy" | "original") {
        if filtering { "libx264" } else { "copy" }
    } else {
        task.options.encoder.as_str()
    };
    if video.is_some() {
        args.extend(["-c:v:0".into(), encoder.into()]);
        if encoder != "copy" {
            args.extend(video_encoder_args(encoder));
        }
        if filtering {
            args.extend(["-r".into(), fps.to_string()]);
        }
    }
    let audio_codec = (task.conversion && task.options.content_mode == "video")
        .then(|| {
            manifest
                .metadata
                .get("_conversionAudioEncoder")
                .and_then(Value::as_str)
        })
        .flatten()
        .unwrap_or_else(|| audio_codec(&task.options, filtering, extension));
    if !audio.is_empty() {
        args.extend(["-c:a".into(), audio_codec.into()]);
        if !matches!(audio_codec, "copy" | "pcm_s16le" | "flac" | "alac" | "aac") {
            args.extend(["-b:a".into(), "192k".into()]);
        }
    }
    if !subtitle_indices.is_empty() {
        args.extend([
            "-c:s".into(),
            if extension == "mp4" {
                "mov_text".into()
            } else if task.options.subtitle_format == "ass" {
                "ass".into()
            } else {
                "srt".into()
            },
        ]);
    }
    if task.options.embed_thumbnail && !deferred_mux {
        let path = thumbnail.as_ref().context("A thumbnail is required")?;
        if matches!(extension.as_str(), "mkv" | "mka") {
            args.extend([
                "-attach".into(),
                path.to_string_lossy().into(),
                "-metadata:s:t:0".into(),
                "mimetype=image/jpeg".into(),
                "-metadata:s:t:0".into(),
                "filename=cover.jpg".into(),
            ]);
        } else if let Some(index) = embedded_cover_index {
            let output_index = usize::from(video.is_some());
            args.extend([
                "-map".into(),
                format!("{index}:v:0"),
                format!("-c:v:{output_index}"),
                "mjpeg".into(),
                format!("-disposition:v:{output_index}"),
                "attached_pic".into(),
            ]);
        }
    }
    if matches!(extension.as_str(), "mp4" | "m4a") {
        args.extend(["-movflags".into(), "+faststart".into()]);
    }
    if filtering {
        args.extend(["-t".into(), (covered + cover_duration).to_string()]);
    }
    args.extend([
        "-threads".into(),
        "2".into(),
        "-progress".into(),
        "pipe:1".into(),
        "-nostats".into(),
        encoded_output.to_string_lossy().into(),
    ]);
    app.set_stage(
        id,
        "processing",
        if encoder == "copy" {
            "muxing"
        } else {
            "encoding"
        },
        None,
    )?;
    run_ffmpeg(app, id, &args, covered + cover_duration, cancel).await?;
    if deferred_mux {
        let mut args = vec![
            "-hide_banner".into(),
            "-loglevel".into(),
            "error".into(),
            "-nostdin".into(),
            "-y".into(),
            "-i".into(),
            encoded_output.to_string_lossy().into(),
        ];
        let mut subtitle_indices = Vec::new();
        let mut next_input = 1;
        if task.options.embed_subtitles {
            for (path, language) in &subtitles {
                args.extend(["-i".into(), path.to_string_lossy().into()]);
                subtitle_indices.push((next_input, language));
                next_input += 1;
            }
        }
        let chapters = if task.options.embed_chapters || task.options.sponsorblock_mode == "mark" {
            let path = workspace.join("chapters.ffmetadata");
            write_sync(&path, ffmetadata(&final_chapters).as_bytes())?;
            args.extend([
                "-f".into(),
                "ffmetadata".into(),
                "-i".into(),
                path.to_string_lossy().into(),
            ]);
            let index = next_input;
            next_input += 1;
            Some(index)
        } else {
            None
        };
        let cover = if task.options.embed_thumbnail && !matches!(extension.as_str(), "mkv" | "mka")
        {
            args.extend([
                "-i".into(),
                thumbnail
                    .as_ref()
                    .context("A thumbnail is required")?
                    .to_string_lossy()
                    .into(),
            ]);
            Some(next_input)
        } else {
            None
        };
        args.extend([
            "-map".into(),
            "0".into(),
            "-c".into(),
            "copy".into(),
            "-map_metadata".into(),
            "0".into(),
            "-map_chapters".into(),
            chapters.map(|i| i.to_string()).unwrap_or("-1".into()),
        ]);
        if chapters.is_some() {
            set_chapter_metadata(&mut args, &final_chapters);
        }
        for (track, (index, language)) in subtitle_indices.iter().enumerate() {
            args.extend([
                "-map".into(),
                format!("{index}:s:0"),
                format!("-metadata:s:s:{track}"),
                format!("language={language}"),
            ]);
        }
        if !subtitle_indices.is_empty() {
            args.extend([
                "-c:s".into(),
                if extension == "mp4" {
                    "mov_text".into()
                } else if task.options.subtitle_format == "ass" {
                    "ass".into()
                } else {
                    "srt".into()
                },
            ]);
        }
        if task.options.embed_thumbnail {
            let path = thumbnail.as_ref().context("A thumbnail is required")?;
            if matches!(extension.as_str(), "mkv" | "mka") {
                args.extend([
                    "-attach".into(),
                    path.to_string_lossy().into(),
                    "-metadata:s:t:0".into(),
                    "mimetype=image/jpeg".into(),
                    "-metadata:s:t:0".into(),
                    "filename=cover.jpg".into(),
                ]);
            } else if let Some(index) = cover {
                let index_out = usize::from(video.is_some());
                args.extend([
                    "-map".into(),
                    format!("{index}:v:0"),
                    format!("-c:v:{index_out}"),
                    "mjpeg".into(),
                    format!("-disposition:v:{index_out}"),
                    "attached_pic".into(),
                ]);
            }
        }
        if matches!(extension.as_str(), "mp4" | "m4a") {
            args.extend(["-movflags".into(), "+faststart".into()]);
        }
        args.extend([
            "-progress".into(),
            "pipe:1".into(),
            output.to_string_lossy().into(),
        ]);
        app.set_stage(id, "processing", "muxing_assets", None)?;
        run_ffmpeg(app, id, &args, covered + cover_duration, cancel).await?;
    }
    app.set_stage(id, "processing", "verifying_output", None)?;
    let probe = probe_file(app, &task, &output, cancel).await?;
    let result_info = crate::model::MediaInfo::from_probe(
        &probe,
        Some(std::fs::metadata(&output)?.len()),
        Some(extension),
    );
    let mut current = app.db.task(id)?;
    current.media_info = Some(result_info);
    app.save_task(&current)?;
    ensure!(
        count_streams(&probe, "video") == usize::from(video.is_some()),
        "Processed output has an unexpected number of video streams"
    );
    ensure!(
        count_streams(&probe, "audio") == audio.len(),
        "Processed output lost an audio stream"
    );
    if task.options.embed_subtitles {
        ensure!(
            count_streams(&probe, "subtitle") == subtitles.len(),
            "Processed output lost a subtitle track"
        );
    }
    let final_duration =
        probe_duration(&probe).context("Processed output has no valid duration")?;
    ensure!(final_duration > 0.0, "Processed output is empty");
    if filtering {
        ensure!(
            (final_duration - covered - cover_duration).abs() < 1.5,
            "Processed duration differs from the requested cut"
        );
    }
    let directory = if task.options.organize_task_folder {
        app.config.download_dir.join(sanitize_filename(&task.title))
    } else {
        app.config.download_dir.clone()
    };
    crate::reject_links(&directory)?;
    std::fs::create_dir_all(&directory)?;
    let target = if task.conversion {
        task.output_path
            .clone()
            .context("Conversion task has no output path")?
    } else {
        directory.join(output_filename(&task, extension))
    };
    let mut auxiliary = Vec::new();
    if !task.conversion {
        for source in manifest
            .files
            .iter()
            .filter(|s| matches!(s.kind.as_str(), "description" | "info" | "comments"))
        {
            let suffix = if source.kind == "info" {
                "info.json"
            } else if source.kind == "comments" {
                "comments.json"
            } else {
                "description.txt"
            };
            let destination = target.with_extension(suffix);
            auxiliary.push(make_auxiliary(&source.path, destination, &task.id)?);
        }
        if task.options.write_thumbnail
            && let Some(path) = &thumbnail
        {
            auxiliary.push(make_auxiliary(
                path,
                target.with_extension(path.extension().unwrap()),
                &task.id,
            )?);
        }
        for (path, language) in &subtitles {
            auxiliary.push(make_auxiliary(
                path,
                target.with_extension(format!(
                    "{language}.{}",
                    path.extension().unwrap().to_string_lossy()
                )),
                &task.id,
            )?);
        }
        if task.options.split_chapters {
            for (index, chapter) in final_chapters.iter().enumerate() {
                app.set_stage(
                    id,
                    "processing",
                    &format!("splitting_chapter_{}", index + 1),
                    None,
                )?;
                let path = workspace.join(format!("chapter-{index}.{extension}"));
                split_chapter(
                    app,
                    &task,
                    ChapterSplit {
                        source: &output,
                        target: &path,
                        chapter,
                        subtitles: &subtitles,
                        thumbnail: thumbnail.as_deref(),
                        video: video.is_some(),
                        audio: !audio.is_empty(),
                    },
                    cancel,
                )
                .await?;
                let mut chapter_title = sanitize_filename(&chapter.title);
                truncate_filename_bytes(&mut chapter_title, 80);
                let name = bounded_filename(&format!(
                    "{}.chapter-{:02}-{}.{}",
                    target.file_stem().unwrap().to_string_lossy(),
                    index + 1,
                    chapter_title,
                    extension
                ));
                auxiliary.push(make_auxiliary(&path, directory.join(name), &task.id)?);
            }
        }
    }
    let staged = make_auxiliary(&output, target.clone(), &task.id)?;
    let mut prepared = app.db.task(id)?;
    prepared.publication = Some(Publication {
        committed: false,
        temporary: staged.temporary,
        target: staged.target,
        hash: staged.hash,
        size: staged.size,
        duration: final_duration,
        auxiliary,
        backup: None,
    });
    prepared.stage("processing", "publishing", None);
    app.save_task(&prepared)?;
    publish(app, prepared, cancel).await
}

async fn prepare_assets(app: &Arc<App>, mut task: Task, cancel: &CancellationToken) -> Result<()> {
    let manifest = task
        .manifest
        .as_ref()
        .context("Asset source manifest is missing")?;
    let workspace = workspace(app, &task);
    crate::reject_links(&workspace)?;
    std::fs::create_dir_all(&workspace)?;
    let mut assets: Vec<(PathBuf, String, String)> = manifest
        .files
        .iter()
        .filter(|source| matches!(source.kind.as_str(), "info" | "description"))
        .map(|source| {
            (
                source.path.clone(),
                source.kind.clone(),
                if source.kind == "info" {
                    "info.json".into()
                } else {
                    "description.txt".into()
                },
            )
        })
        .collect();
    if let Some(thumbnail) = prepare_thumbnail(&task, &workspace)? {
        let suffix = thumbnail
            .extension()
            .context("Thumbnail has no extension")?
            .to_string_lossy()
            .into_owned();
        assets.push((thumbnail, "thumbnail".into(), suffix));
    }
    for (path, language) in prepare_subtitles(
        app,
        &task,
        &workspace,
        &[(0.0, manifest.duration.unwrap_or(0.0))],
        false,
        0.0,
        cancel,
    )
    .await?
    {
        let extension = path
            .extension()
            .context("Subtitle has no extension")?
            .to_string_lossy();
        let suffix = format!("{}.{}", sanitize_filename(&language), extension);
        assets.push((path, "subtitle".into(), suffix));
    }
    if assets.is_empty() {
        task = app.db.task(&task.id)?;
        task.warnings
            .push("The requested assets were not provided by this media source.".into());
        task.stage("completed", "completed", Some(100.0));
        return app.save_task(&task);
    }
    let directory = if task.options.organize_task_folder {
        app.config.download_dir.join(sanitize_filename(&task.title))
    } else {
        app.config.download_dir.clone()
    };
    crate::reject_links(&directory)?;
    std::fs::create_dir_all(&directory)?;
    let stem = format!(
        "{} [assets-{}]",
        sanitize_filename(&task.title),
        &task.id[..8]
    );
    let mut outputs = Vec::new();
    task.files.clear();
    for (path, kind, suffix) in assets {
        if cancel.is_cancelled() {
            bail!("Operation interrupted");
        }
        let target = directory.join(bounded_filename(&format!("{stem}.{suffix}")));
        let output = make_auxiliary(&path, target, &task.id)?;
        task.files.push(GeneratedFile {
            filename: output
                .target
                .file_name()
                .unwrap()
                .to_string_lossy()
                .into_owned(),
            kind,
            size: output.size,
        });
        outputs.push(output);
    }
    let primary = outputs.remove(0);
    task.publication = Some(Publication {
        committed: false,
        temporary: primary.temporary,
        target: primary.target,
        hash: primary.hash,
        size: primary.size,
        duration: 0.0,
        auxiliary: outputs,
        backup: None,
    });
    task.stage("processing", "publishing", None);
    app.save_task(&task)?;
    publish(app, task, cancel).await
}

struct ChapterSplit<'a> {
    source: &'a Path,
    target: &'a Path,
    chapter: &'a Chapter,
    subtitles: &'a [(PathBuf, String)],
    thumbnail: Option<&'a Path>,
    video: bool,
    audio: bool,
}
async fn split_chapter(
    app: &Arc<App>,
    task: &Task,
    split: ChapterSplit<'_>,
    cancel: &CancellationToken,
) -> Result<()> {
    let extension = task.manifest.as_ref().unwrap().output_extension.as_str();
    let duration = split.chapter.end - split.chapter.start;
    let stem = split.target.file_stem().unwrap().to_string_lossy();
    let base = split
        .target
        .with_file_name(format!("{stem}.base.{extension}"));
    // Decode the finite chapter before muxing sparse subtitles, so GOP preroll and long subtitle cues cannot extend it.
    let mut args = vec![
        "-hide_banner".into(),
        "-loglevel".into(),
        "error".into(),
        "-nostdin".into(),
        "-y".into(),
        "-i".into(),
        split.source.to_string_lossy().into(),
        "-ss".into(),
        split.chapter.start.to_string(),
        "-t".into(),
        duration.to_string(),
        "-map_metadata".into(),
        "0".into(),
        "-map_chapters".into(),
        "-1".into(),
    ];
    if split.video {
        let encoder = if matches!(task.options.encoder.as_str(), "copy" | "original") {
            "libx264"
        } else {
            task.options.encoder.as_str()
        };
        args.extend(["-map".into(), "0:v:0".into(), "-c:v".into(), encoder.into()]);
        args.extend(video_encoder_args(encoder));
    }
    if split.audio {
        args.extend([
            "-map".into(),
            "0:a".into(),
            "-c:a".into(),
            audio_codec(&task.options, true, extension).into(),
        ]);
    }
    args.extend([
        "-threads".into(),
        "2".into(),
        "-progress".into(),
        "pipe:1".into(),
        base.to_string_lossy().into(),
    ]);
    run_ffmpeg(app, &task.id, &args, duration, cancel).await?;
    let mut subtitles = Vec::new();
    if task.options.embed_subtitles {
        for (index, (source, language)) in split.subtitles.iter().enumerate() {
            let path = split
                .target
                .with_file_name(format!("{stem}.subtitle-{index}.srt"));
            subtitle_convert(app, task, source, &path, cancel).await?;
            let cues = retime_cues(
                &parse_srt(&std::fs::read_to_string(&path)?)?,
                &[(split.chapter.start, split.chapter.end)],
                0.0,
            );
            if !cues.is_empty() {
                write_sync(&path, format_srt(&cues).as_bytes())?;
                subtitles.push((path, language));
            }
        }
    }
    let mut args = vec![
        "-hide_banner".into(),
        "-loglevel".into(),
        "error".into(),
        "-nostdin".into(),
        "-y".into(),
        "-i".into(),
        base.to_string_lossy().into(),
    ];
    for (path, _) in &subtitles {
        args.extend(["-i".into(), path.to_string_lossy().into()]);
    }
    let cover = if task.options.embed_thumbnail && !matches!(extension, "mkv" | "mka") {
        args.extend([
            "-i".into(),
            split
                .thumbnail
                .context("A thumbnail is required")?
                .to_string_lossy()
                .into(),
        ]);
        Some(subtitles.len() + 1)
    } else {
        None
    };
    args.extend([
        "-map".into(),
        "0".into(),
        "-c".into(),
        "copy".into(),
        "-map_metadata".into(),
        "0".into(),
        "-map_chapters".into(),
        "-1".into(),
    ]);
    for (index, (_, language)) in subtitles.iter().enumerate() {
        args.extend([
            "-map".into(),
            format!("{}:s:0", index + 1),
            format!("-metadata:s:s:{index}"),
            format!("language={language}"),
        ]);
    }
    if !subtitles.is_empty() {
        args.extend([
            "-c:s".into(),
            if extension == "mp4" {
                "mov_text".into()
            } else if task.options.subtitle_format == "ass" {
                "ass".into()
            } else {
                "srt".into()
            },
        ]);
    }
    if task.options.embed_thumbnail {
        let path = split.thumbnail.context("A thumbnail is required")?;
        if matches!(extension, "mkv" | "mka") {
            args.extend([
                "-attach".into(),
                path.to_string_lossy().into(),
                "-metadata:s:t:0".into(),
                "mimetype=image/jpeg".into(),
                "-metadata:s:t:0".into(),
                "filename=cover.jpg".into(),
            ]);
        } else if let Some(index) = cover {
            let output_index = usize::from(split.video);
            args.extend([
                "-map".into(),
                format!("{index}:v:0"),
                format!("-c:v:{output_index}"),
                "mjpeg".into(),
                format!("-disposition:v:{output_index}"),
                "attached_pic".into(),
            ]);
        }
    }
    if matches!(extension, "mp4" | "m4a") {
        args.extend(["-movflags".into(), "+faststart".into()]);
    }
    args.extend([
        "-progress".into(),
        "pipe:1".into(),
        split.target.to_string_lossy().into(),
    ]);
    run_ffmpeg(app, &task.id, &args, duration, cancel).await?;
    let probe = probe_file(app, task, split.target, cancel).await?;
    let measured = probe_duration(&probe).context("Split chapter has no duration")?;
    ensure!(
        (measured - duration).abs() < 0.5,
        "Split chapter duration differs from its range"
    );
    Ok(())
}

fn make_auxiliary(source: &Path, target: PathBuf, task_id: &str) -> Result<AuxiliaryPublication> {
    let target = target.with_file_name(bounded_filename(
        &target
            .file_name()
            .context("Publication target has no filename")?
            .to_string_lossy(),
    ));
    crate::reject_links(source)?;
    crate::reject_links(&target)?;
    let directory = target
        .parent()
        .context("Publication target has no directory")?;
    std::fs::create_dir_all(directory)?;
    let mut staged = tempfile::Builder::new()
        .prefix(&format!(".huifa-{task_id}-"))
        .suffix(".publish")
        .rand_bytes(16)
        .tempfile_in(directory)?;
    std::io::copy(&mut std::fs::File::open(source)?, staged.as_file_mut())?;
    staged.as_file().sync_all()?;
    ensure!(
        !staged.path().with_extension("before-publication").exists(),
        "Publication backup already exists"
    );
    let (_, temporary) = staged.keep().map_err(|reason| reason.error)?;
    let (hash, size) = hash_file(&temporary)?;
    Ok(AuxiliaryPublication {
        temporary,
        target,
        hash,
        size,
    })
}
fn copy_sync(source: &Path, target: &Path) -> Result<()> {
    crate::reject_links(source)?;
    crate::reject_links(target)?;
    if let Some(parent) = target.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let mut input = std::fs::File::open(source)?;
    let mut output = std::fs::File::create(target)?;
    std::io::copy(&mut input, &mut output)?;
    output.sync_all()?;
    Ok(())
}

fn publication_files(
    publication: &Publication,
    task_id: &str,
) -> (Vec<AuxiliaryPublication>, Vec<PathBuf>) {
    let entries = std::iter::once(AuxiliaryPublication {
        temporary: publication.temporary.clone(),
        target: publication.target.clone(),
        hash: publication.hash.clone(),
        size: publication.size,
    })
    .chain(publication.auxiliary.clone())
    .collect::<Vec<_>>();
    let backups = entries
        .iter()
        .enumerate()
        .map(|(index, entry)| {
            if index == 0
                && let Some(path) = &publication.backup
            {
                return path.clone();
            }
            let name = entry
                .temporary
                .file_name()
                .unwrap_or_default()
                .to_string_lossy();
            if name
                .strip_prefix(&format!(".huifa-{task_id}-"))
                .and_then(|name| name.strip_suffix(".publish"))
                .is_some_and(|nonce| {
                    nonce.len() == 16 && nonce.chars().all(|c| c.is_ascii_alphanumeric())
                })
            {
                return entry.temporary.with_extension("before-publication");
            }
            // Existing journals retain their original backup paths for interrupted publication recovery.
            entry.target.with_file_name(format!(
                ".{}.{task_id}.before-publication",
                entry.target.file_name().unwrap().to_string_lossy()
            ))
        })
        .collect();
    (entries, backups)
}

pub(crate) async fn prepare_redownload(app: &App, task: &Task) -> Result<()> {
    let _publication_guard = app.publication_lock.lock().await;
    let mut task = app.db.task(&task.id)?;
    if task
        .publication
        .as_ref()
        .is_some_and(|publication| publication.committed)
    {
        return finish_publication(app, task, &app.db.media()?);
    }
    if task.status == "completed" {
        return Ok(());
    }
    let Some(publication) = &task.publication else {
        return Ok(());
    };
    let (entries, backups) = publication_files(publication, &task.id);
    let previous = app.db.media_for_task(&task.id)?;
    let result = (|| -> Result<()> {
        let mut installed = Vec::new();
        for (index, (entry, backup)) in entries.iter().zip(&backups).enumerate() {
            for path in [&entry.target, &entry.temporary, backup] {
                crate::reject_links(path)?;
                ensure!(
                    path.parent()
                        .context("Publication path has no directory")?
                        .canonicalize()?
                        .starts_with(&app.config.download_dir),
                    "Publication path is outside the owned downloads directory"
                );
            }
            if entry.temporary.exists() {
                ensure!(
                    hash_file(&entry.temporary)? == (entry.hash.clone(), entry.size),
                    "Prepared output changed; publication files were preserved"
                );
            }
            let target_matches = entry.target.exists()
                && hash_file(&entry.target)? == (entry.hash.clone(), entry.size);
            let old_owned = task.previous_outputs.contains(&entry.target)
                || previous.as_ref().is_some_and(|media| {
                    media.path == entry.target || media.assets.contains(&entry.target)
                })
                || task.conversion
                    && task.manifest.as_ref().is_some_and(|manifest| {
                        manifest
                            .files
                            .iter()
                            .any(|source| source.path == entry.target)
                    });
            if backup.exists() {
                ensure!(
                    old_owned,
                    "Publication backup has no previously managed output; files were preserved"
                );
                ensure!(
                    backup.is_file(),
                    "Original publication backup is not a regular file"
                );
                ensure!(
                    !entry.target.exists() || target_matches,
                    "Publication destination changed; original backup was preserved"
                );
                installed.push(index);
            } else if target_matches && !entry.temporary.exists() && !old_owned {
                installed.push(index);
            }
        }
        rollback_publication(&entries, &backups, &installed)?;
        for entry in &entries {
            if entry.temporary.exists() {
                ensure!(
                    hash_file(&entry.temporary)? == (entry.hash.clone(), entry.size),
                    "Prepared output changed during recovery"
                );
                std::fs::remove_file(&entry.temporary)?;
            }
        }
        Ok(())
    })();
    result.context("Cannot safely restart this download; saved files and the publication journal were preserved. Retry processing to recover the previous publication")?;
    task.publication = None;
    app.save_task(&task)
}

pub(crate) async fn prepare_task_deletion(app: &App, id: &str) -> Result<()> {
    let mut task = app.db.task(id)?;
    if task.status != "completed" {
        prepare_redownload(app, &task).await?;
        task = app.db.task(id)?;
    }
    let _publication_guard = app.publication_lock.lock().await;
    let Some(publication) = &task.publication else {
        return Ok(());
    };
    let (entries, backups) = publication_files(publication, id);
    for (entry, backup) in entries.iter().zip(&backups) {
        for path in [&entry.target, &entry.temporary, backup] {
            crate::reject_links(path)?;
            ensure!(
                path.parent()
                    .context("Publication path has no directory")?
                    .canonicalize()?
                    .starts_with(&app.config.download_dir),
                "Publication path is outside the owned downloads directory"
            );
        }
        if entry.temporary.exists() {
            ensure!(
                hash_file(&entry.temporary)? == (entry.hash.clone(), entry.size),
                "Prepared output changed; publication files were preserved"
            );
        }
        if backup.exists() {
            ensure!(
                entry.target.is_file()
                    && hash_file(&entry.target)? == (entry.hash.clone(), entry.size),
                "Publication destination changed; original backup was preserved"
            );
        }
    }
    for (entry, backup) in entries.iter().zip(&backups) {
        if entry.temporary.exists() {
            std::fs::remove_file(&entry.temporary)?;
        }
        if backup.exists() {
            std::fs::remove_file(backup)?;
        }
    }
    Ok(())
}

async fn publish(app: &Arc<App>, mut task: Task, cancel: &CancellationToken) -> Result<()> {
    let _publication_guard = tokio::select! {
        guard = app.publication_lock.lock() => guard,
        _ = cancel.cancelled() => bail!("Operation interrupted"),
    };
    let publication = task
        .publication
        .clone()
        .context("Publication journal is missing")?;
    if publication.committed {
        return finish_publication(app, task, &app.db.media()?);
    }
    let previous = app.db.media_for_task(&task.id)?;
    let (entries, backups) = publication_files(&publication, &task.id);
    let all_media = app.db.media()?;
    let mut destinations = std::collections::HashSet::new();
    for (entry, backup) in entries.iter().zip(&backups) {
        for path in [&entry.target, &entry.temporary, backup] {
            crate::reject_links(path)?;
            ensure!(
                path.parent()
                    .context("Publication path has no directory")?
                    .canonicalize()?
                    .starts_with(&app.config.download_dir),
                "Publication path is outside the owned downloads directory"
            );
        }
        let destination = entry.target.to_string_lossy().into_owned();
        #[cfg(windows)]
        let destination = destination.to_lowercase();
        ensure!(
            destinations.insert(destination),
            "Two generated files have the same publication filename"
        );
        ensure!(
            !all_media.iter().any(|media| media.task_id != task.id
                && (media.path == entry.target || media.assets.contains(&entry.target))),
            "Publication filename belongs to another task"
        );
        let matches =
            entry.target.exists() && hash_file(&entry.target)? == (entry.hash.clone(), entry.size);
        let owned = previous.as_ref().is_some_and(|media| {
            media.path == entry.target || media.assets.contains(&entry.target)
        }) || task.kind == "assets" && task.previous_outputs.contains(&entry.target);
        if matches {
            ensure!(
                owned || !entry.temporary.exists() || task.status == "completed",
                "Publication filename is already occupied by an unmanaged file"
            );
            continue;
        }
        ensure!(
            entry.temporary.is_file(),
            "Publication output is missing; saved sources remain available"
        );
        ensure!(
            hash_file(&entry.temporary)? == (entry.hash.clone(), entry.size),
            "Prepared output changed after validation"
        );
        if entry.target.exists() {
            ensure!(
                !backup.exists(),
                "Publication destination is occupied; original backup is preserved"
            );
            let conversion_source = task.conversion
                && task.manifest.as_ref().is_some_and(|manifest| {
                    manifest.files.iter().any(|source| {
                        source.path == entry.target
                            && hash_file(&entry.target)
                                .is_ok_and(|value| value == (source.hash.clone(), source.size))
                    })
                });
            ensure!(
                owned || conversion_source,
                "Publication filename is already occupied by an unmanaged file"
            );
        }
    }
    let mut installed = backups
        .iter()
        .enumerate()
        .filter_map(|(index, backup)| backup.exists().then_some(index))
        .collect::<Vec<_>>();
    let result = async {
        for (index, (entry, backup)) in entries.iter().zip(&backups).enumerate() {
            ensure!(!cancel.is_cancelled(), "Operation interrupted");
            if entry.target.exists()
                && hash_file(&entry.target)? == (entry.hash.clone(), entry.size)
            {
                continue;
            }
            if entry.target.exists() {
                std::fs::rename(&entry.target, backup)?;
            }
            // Record before rename so a failed installation also restores the old file.
            if !installed.contains(&index) {
                installed.push(index);
            }
            std::fs::rename(&entry.temporary, &entry.target)?;
        }
        sync_directory(publication.target.parent().unwrap())?;
        app.set_stage(&task.id, "processing", "validating_publication", None)?;
        if task.kind != "assets" {
            probe_file(app, &task, &publication.target, cancel).await?;
        }
        ensure!(!cancel.is_cancelled(), "Operation interrupted");
        Ok::<_, anyhow::Error>(())
    }
    .await;
    if let Err(error) = result {
        return match rollback_publication(&entries, &backups, &installed) {
            Ok(()) => Err(error),
            Err(rollback) => Err(error.context(format!(
                "Original files are preserved in publication backups; rollback failed: {rollback:#}"
            ))),
        };
    }
    if task.kind == "assets" {
        task = match app.db.task(&task.id) {
            Ok(task) => task,
            Err(error) => {
                rollback_publication(&entries, &backups, &installed)?;
                return Err(error);
            }
        };
        task.output_path = Some(publication.target);
        task.size =
            Some(publication.size + publication.auxiliary.iter().map(|a| a.size).sum::<u64>());
        task.error = None;
        task.publication.as_mut().unwrap().committed = true;
        task.stage("processing", "saving_results", None);
        if let Err(error) = app.db.save_task(&task) {
            rollback_publication(&entries, &backups, &installed)?;
            return Err(error);
        }
        return finish_publication(app, task, &all_media);
    }
    let mut assets = if task.conversion {
        previous
            .as_ref()
            .map(|m| m.assets.clone())
            .unwrap_or_default()
    } else {
        vec![]
    };
    assets.extend(publication.auxiliary.iter().map(|a| a.target.clone()));
    let media = Media {
        media_info: None,
        id: previous
            .as_ref()
            .map(|m| m.id.clone())
            .unwrap_or_else(|| uuid::Uuid::new_v4().to_string()),
        task_id: task.id.clone(),
        title: task.title.clone(),
        filename: publication
            .target
            .file_name()
            .unwrap()
            .to_string_lossy()
            .into(),
        size: publication.size,
        duration: publication.duration,
        path: publication.target.clone(),
        assets,
    };
    task = match app.db.task(&task.id) {
        Ok(task) => task,
        Err(error) => {
            rollback_publication(&entries, &backups, &installed)?;
            return Err(error);
        }
    };
    task.output_path = Some(publication.target.clone());
    task.size = Some(publication.size);
    if let Some(info) = task.media_info.as_mut() {
        info.size = Some(publication.size);
    }
    task.error = None;
    if let Some(previous) = previous {
        task.previous_outputs.push(previous.path);
        task.previous_outputs.extend(previous.assets);
    }
    task.previous_outputs.sort();
    task.previous_outputs.dedup();
    task.publication.as_mut().unwrap().committed = true;
    task.stage("processing", "saving_results", None);
    let media = match app.db.complete(&task, &media) {
        Ok(media) => media,
        Err(error) => {
            rollback_publication(&entries, &backups, &installed)?;
            return Err(error);
        }
    };
    app.emit("media.upsert", media.public());
    finish_publication(app, task, &all_media)
}

fn finish_publication(app: &App, mut task: Task, all_media: &[Media]) -> Result<()> {
    let publication = task
        .publication
        .as_ref()
        .context("Publication journal is missing")?;
    let (entries, backups) = publication_files(publication, &task.id);
    for (entry, backup) in entries.iter().zip(&backups) {
        for path in [&entry.target, &entry.temporary, backup] {
            crate::reject_links(path)?;
            ensure!(
                path.parent()
                    .context("Publication path has no directory")?
                    .canonicalize()?
                    .starts_with(&app.config.download_dir),
                "Publication path is outside the owned downloads directory"
            );
        }
        if backup.exists() {
            ensure!(
                entry.target.is_file()
                    && hash_file(&entry.target)? == (entry.hash.clone(), entry.size),
                "Publication destination changed; original backup was preserved"
            );
        }
    }
    let backups_cleaned = cleanup_backups(&backups);
    let current = if task.kind == "assets" {
        entries
            .iter()
            .map(|entry| entry.target.clone())
            .collect::<Vec<_>>()
    } else {
        let media = app
            .db
            .media_for_task(&task.id)?
            .context("Committed media does not exist")?;
        std::iter::once(media.path).chain(media.assets).collect()
    };
    task.previous_outputs =
        cleanup_replaced_paths(app, &task.id, &task.previous_outputs, &current, all_media);
    let warning =
        "Some previous output files could not be removed; they were preserved for a later cleanup.";
    task.warnings.retain(|value| value != warning);
    if !backups_cleaned || !task.previous_outputs.is_empty() {
        task.warnings.push(warning.into());
    }
    task.stage("completed", "completed", Some(100.0));
    app.save_task(&task)
}

fn rollback_publication(
    entries: &[AuxiliaryPublication],
    backups: &[PathBuf],
    installed: &[usize],
) -> Result<()> {
    for index in installed.iter().rev().copied() {
        let entry = &entries[index];
        if entry.target.exists() {
            ensure!(
                hash_file(&entry.target)? == (entry.hash.clone(), entry.size),
                "Published file changed; original backup was preserved"
            );
            if entry.temporary.exists() {
                std::fs::remove_file(&entry.target)?;
            } else {
                std::fs::rename(&entry.target, &entry.temporary)?;
            }
        }
        if backups[index].exists() {
            std::fs::rename(&backups[index], &entry.target)?;
        }
    }
    Ok(())
}

fn cleanup_backups(backups: &[PathBuf]) -> bool {
    let mut cleaned = true;
    for backup in backups {
        if backup.exists() && std::fs::remove_file(backup).is_err() {
            cleaned = false;
        }
    }
    cleaned
}

fn cleanup_replaced_paths(
    app: &App,
    task_id: &str,
    previous: &[PathBuf],
    current: &[PathBuf],
    other_media: &[Media],
) -> Vec<PathBuf> {
    let mut remaining = Vec::new();
    for path in previous {
        if current.contains(path)
            || other_media.iter().any(|media| {
                media.task_id != task_id && (media.path == *path || media.assets.contains(path))
            })
        {
            continue;
        }
        if !path.exists() {
            continue;
        }
        let Ok(owned) = app.owned_path(path) else {
            remaining.push(path.clone());
            continue;
        };
        if std::fs::remove_file(&owned).is_err() {
            remaining.push(path.clone());
        } else if let Some(parent) = owned.parent()
            && parent.file_name().is_some_and(|name| name == &task_id[..8])
        {
            let _ = std::fs::remove_dir(parent);
        }
    }
    remaining
}

fn sync_directory(path: &Path) -> Result<()> {
    #[cfg(unix)]
    {
        std::fs::File::open(path)?.sync_all()?;
    }
    #[cfg(windows)]
    {
        let _ = path;
    }
    Ok(())
}

pub(crate) async fn task_info(app: &App, id: &str) -> Result<Value> {
    let cancel = app.shutdown_token.child_token();
    let mut operation = Box::pin(task_info_inner(app, id, &cancel));
    tokio::select! {
        result = &mut operation => result,
        _ = tokio::time::sleep(Duration::from_secs(30)) => {
            cancel.cancel();
            let _ = operation.await;
            bail!("Reading saved media information timed out; retry when the file is available")
        }
    }
}

async fn task_info_inner(app: &App, id: &str, cancel: &CancellationToken) -> Result<Value> {
    let _info_guard = tokio::select! { guard = app.media_info_lock.lock() => guard, _ = cancel.cancelled() => bail!("Media information query cancelled") };
    let task = app.db.task(id)?;
    if task.kind != "media"
        || !matches!(task.status.as_str(), "completed" | "retained")
        || task.media_info.is_some()
    {
        return Ok(task.public());
    }
    let _publication_guard = tokio::select! { guard = app.publication_lock.lock() => guard, _ = cancel.cancelled() => bail!("Media information query cancelled") };
    let task = app.db.task(id)?;
    if task.kind != "media"
        || !matches!(task.status.as_str(), "completed" | "retained")
        || task.media_info.is_some()
    {
        return Ok(task.public());
    }
    let path = app.owned_path(
        task.output_path
            .as_ref()
            .context("Task has no saved media file")?,
    )?;
    let probe = probe_file(app, &task, &path, cancel).await?;
    let size = task.size.or_else(|| {
        probe["format"]["size"]
            .as_str()
            .and_then(|size| size.parse().ok())
    });
    let info = crate::model::MediaInfo::from_probe(
        &probe,
        size,
        path.extension().and_then(|s| s.to_str()),
    );
    let mut source_info = task.source_info.clone();
    if source_info.is_none()
        && let Some(manifest) = &task.manifest
    {
        let mut probes = Vec::new();
        let mut source_size = 0;
        for source in manifest
            .files
            .iter()
            .filter(|s| matches!(s.kind.as_str(), "video" | "audio"))
            .take(32)
        {
            let owned = source
                .path
                .canonicalize()
                .is_ok_and(|path| path.starts_with(app.config.data_dir.join("tasks").join(id)))
                || task.conversion && app.owned_path(&source.path).is_ok();
            if owned && let Ok(probe) = probe_file(app, &task, &source.path, cancel).await {
                probes.extend(probe["streams"].as_array().into_iter().flatten().cloned());
                source_size += source.size;
            }
        }
        if !probes.is_empty() {
            source_info = Some(crate::model::MediaInfo::from_probe(
                &json!({"streams":probes,"format":{"duration":manifest.duration.map(|d|d.to_string())}}),
                Some(source_size),
                None,
            ));
        }
    }
    let mut current = app.db.task(id)?;
    if matches!(current.status.as_str(), "completed" | "retained")
        && current.output_path == task.output_path
    {
        current.media_info = Some(info.clone());
        current.source_info = source_info;
        current.size = size;
        app.db.save_media_info(id, &path, &info)?;
        app.db.save_task(&current)?;
        if current.status != "retained" {
            app.emit("task.upsert", current.public());
        }
        if let Some(media) = app.db.media_for_task(id)? {
            app.emit("media.upsert", media.public());
        }
    }
    Ok(current.public())
}

async fn probe_file(
    app: &App,
    task: &Task,
    path: &Path,
    cancel: &CancellationToken,
) -> Result<Value> {
    let args = vec![
        "-v".into(),
        "error".into(),
        "-show_format".into(),
        "-show_streams".into(),
        "-of".into(),
        "json".into(),
        path.to_string_lossy().into(),
    ];
    let output = process::run(
        &app.task_tool(task, "ffprobe").await?,
        &args,
        None,
        cancel,
        None,
    )
    .await?;
    let value: Value = serde_json::from_str(&output.stdout)?;
    ensure!(
        value["streams"].as_array().is_some_and(|s| !s.is_empty()),
        "Media validation found no streams"
    );
    Ok(value)
}
fn streams(probe: &Value) -> impl Iterator<Item = &Value> {
    probe["streams"].as_array().into_iter().flatten()
}
fn count_streams(probe: &Value, kind: &str) -> usize {
    streams(probe)
        .filter(|s| {
            text(s, "codec_type") == kind
                && !s["disposition"]["attached_pic"]
                    .as_u64()
                    .is_some_and(|v| v != 0)
        })
        .count()
}
fn probe_duration(probe: &Value) -> Option<f64> {
    text(&probe["format"], "duration")
        .parse::<f64>()
        .ok()
        .filter(|v| v.is_finite() && *v > 0.0)
        .or_else(|| {
            streams(probe)
                .filter_map(|s| text(s, "duration").parse::<f64>().ok())
                .filter(|v| v.is_finite() && *v > 0.0)
                .reduce(f64::max)
        })
}
fn parse_fraction(s: &str) -> Option<f64> {
    let (a, b) = s.split_once('/')?;
    Some(a.parse::<f64>().ok()? / b.parse::<f64>().ok()?)
}

async fn run_ffmpeg(
    app: &Arc<App>,
    id: &str,
    args: &[String],
    duration: f64,
    cancel: &CancellationToken,
) -> Result<()> {
    let progress_app = app.clone();
    let task_id = id.to_string();
    let last = Arc::new(Mutex::new(Instant::now() - Duration::from_secs(1)));
    let callback: LineCallback = Arc::new(move |is_error, line| {
        if is_error {
            return;
        }
        if let Some(time) = line
            .strip_prefix("out_time_us=")
            .and_then(|s| s.parse::<f64>().ok())
        {
            let mut last = last.lock().unwrap();
            if last.elapsed() < Duration::from_millis(200) {
                return;
            }
            *last = Instant::now();
            if let Ok(mut task) = progress_app.db.task(&task_id) {
                if task.status != "processing" {
                    return;
                }
                task.stage_progress =
                    Some((time / 1_000_000.0 / duration * 100.0).clamp(0.0, 100.0));
                task.stage_indeterminate = false;
                task.progress = task.stage_progress.unwrap().min(99.0);
                let _ = progress_app.save_task(&task);
            }
        }
    });
    process::run(
        &app.task_tool(&app.db.task(id)?, "ffmpeg").await?,
        args,
        None,
        cancel,
        Some(callback),
    )
    .await?;
    Ok(())
}

fn video_encoder_args(encoder: &str) -> Vec<String> {
    match encoder {
        "libx264" => vec!["-preset", "medium", "-crf", "23", "-pix_fmt", "yuv420p"],
        "libx265" => vec![
            "-preset",
            "medium",
            "-crf",
            "27",
            "-pix_fmt",
            "yuv420p",
            "-x265-params",
            "pools=2:frame-threads=2",
        ],
        "libsvtav1" => vec!["-preset", "8", "-crf", "30", "-svtav1-params", "lp=2"],
        "libaom-av1" => vec!["-cpu-used", "6", "-crf", "30", "-b:v", "0"],
        "librav1e" => vec!["-speed", "6", "-qp", "100"],
        _ => vec!["-b:v", "5M"],
    }
    .into_iter()
    .map(str::to_string)
    .collect()
}
fn audio_codec<'a>(options: &'a DownloadOptions, filtering: bool, extension: &str) -> &'a str {
    if options.content_mode == "audio" {
        match options.audio_format.as_str() {
            "aac" | "m4a" => "aac",
            "alac" => "alac",
            "flac" => "flac",
            "mp3" => "libmp3lame",
            "opus" => "libopus",
            "vorbis" => "libvorbis",
            "wav" => "pcm_s16le",
            _ => {
                if filtering {
                    if extension == "webm" {
                        "libopus"
                    } else {
                        "aac"
                    }
                } else {
                    "copy"
                }
            }
        }
    } else if filtering {
        "aac"
    } else {
        "copy"
    }
}

fn prepare_thumbnail(task: &Task, workspace: &Path) -> Result<Option<PathBuf>> {
    let source = task
        .manifest
        .as_ref()
        .unwrap()
        .files
        .iter()
        .find(|f| f.kind == "thumbnail");
    let Some(source) = source else {
        return Ok(None);
    };
    let image = image::open(&source.path)?;
    let jpeg = task.options.convert_cover_jpeg
        || task.options.embed_thumbnail
        || task.options.prepend_cover_enabled;
    if jpeg {
        let path = workspace.join("cover.jpg");
        let mut output = std::fs::File::create(&path)?;
        image::codecs::jpeg::JpegEncoder::new_with_quality(
            &mut output,
            task.options.cover_jpeg_quality,
        )
        .encode_image(&image.to_rgb8())?;
        output.sync_all()?;
        Ok(Some(path))
    } else {
        Ok(Some(source.path.clone()))
    }
}

async fn prepare_subtitles(
    app: &Arc<App>,
    task: &Task,
    workspace: &Path,
    ranges: &[(f64, f64)],
    cuts: bool,
    offset: f64,
    cancel: &CancellationToken,
) -> Result<Vec<(PathBuf, String)>> {
    let mut result = Vec::new();
    for (index, source) in task
        .manifest
        .as_ref()
        .unwrap()
        .files
        .iter()
        .filter(|f| f.kind == "subtitle")
        .enumerate()
    {
        app.set_stage(
            &task.id,
            "processing",
            &format!("converting_subtitle_{}", index + 1),
            None,
        )?;
        let requested = if task.options.subtitle_format == "best" {
            source
                .path
                .extension()
                .and_then(|e| e.to_str())
                .unwrap_or("vtt")
        } else {
            &task.options.subtitle_format
        };
        let output = workspace.join(format!("subtitle-{index}.{requested}"));
        if cuts || offset > 0.0 {
            let srt = workspace.join(format!("subtitle-{index}.timing.srt"));
            subtitle_convert(app, task, &source.path, &srt, cancel).await?;
            let cues = parse_srt(&std::fs::read_to_string(&srt)?)?;
            let cues = retime_cues(&cues, ranges, offset);
            write_sync(&srt, format_srt(&cues).as_bytes())?;
            if requested == "srt" {
                copy_sync(&srt, &output)?;
            } else {
                subtitle_convert(app, task, &srt, &output, cancel).await?;
            }
        } else if source.path.extension().is_some_and(|e| e == requested) {
            copy_sync(&source.path, &output)?;
        } else {
            subtitle_convert(app, task, &source.path, &output, cancel).await?;
        }
        result.push((
            output,
            source
                .language
                .clone()
                .unwrap_or_else(|| format!("track-{index}")),
        ));
    }
    Ok(result)
}
async fn subtitle_convert(
    app: &App,
    task: &Task,
    input: &Path,
    output: &Path,
    cancel: &CancellationToken,
) -> Result<()> {
    let args = vec![
        "-hide_banner".into(),
        "-loglevel".into(),
        "error".into(),
        "-nostdin".into(),
        "-y".into(),
        "-i".into(),
        input.to_string_lossy().into(),
        output.to_string_lossy().into(),
    ];
    process::run(
        &app.task_tool(task, "ffmpeg").await?,
        &args,
        None,
        cancel,
        None,
    )
    .await?;
    Ok(())
}

pub(crate) fn keep_ranges(
    duration: f64,
    options: &DownloadOptions,
    segments: &[Chapter],
) -> Result<Vec<(f64, f64)>> {
    let start = parse_time(&options.section_start)?
        .unwrap_or(0.0)
        .min(duration);
    let end = parse_time(&options.section_end)?
        .unwrap_or(duration)
        .min(duration);
    ensure!(
        end > start,
        "Requested section lies outside the media duration"
    );
    if options.sponsorblock_mode != "remove" {
        return Ok(vec![(start, end)]);
    }
    let mut excluded = segments
        .iter()
        .filter_map(|s| {
            let a = s.start.max(start);
            let b = s.end.min(end);
            (b > a).then_some((a, b))
        })
        .collect::<Vec<_>>();
    excluded.sort_by(|a, b| a.0.total_cmp(&b.0));
    let mut keep = Vec::new();
    let mut cursor = start;
    for (a, b) in excluded {
        if a > cursor {
            keep.push((cursor, a));
        }
        cursor = cursor.max(b);
    }
    if cursor < end {
        keep.push((cursor, end));
    }
    ensure!(
        !keep.is_empty(),
        "SponsorBlock removal would remove the entire selected section"
    );
    Ok(keep)
}
fn retime_chapters(chapters: &[Chapter], ranges: &[(f64, f64)], offset: f64) -> Vec<Chapter> {
    let mut result = Vec::new();
    let mut elapsed = offset;
    for (a, b) in ranges {
        for chapter in chapters {
            let start = chapter.start.max(*a);
            let end = chapter.end.min(*b);
            if end > start {
                result.push(Chapter {
                    start: elapsed + start - a,
                    end: elapsed + end - a,
                    title: chapter.title.clone(),
                });
            }
        }
        elapsed += b - a;
    }
    result
}
fn ffmetadata(chapters: &[Chapter]) -> String {
    let mut output = ";FFMETADATA1\n".to_string();
    for chapter in chapters {
        let title = chapter
            .title
            .replace('\\', "\\\\")
            .replace('=', "\\=")
            .replace(';', "\\;")
            .replace('#', "\\#")
            .replace(['\n', '\r'], " ");
        output.push_str(&format!(
            "[CHAPTER]\nTIMEBASE=1/1000\nSTART={}\nEND={}\ntitle={}\n",
            (chapter.start * 1000.0).round() as u64,
            (chapter.end * 1000.0).round() as u64,
            title
        ));
    }
    output
}
fn set_chapter_metadata(args: &mut Vec<String>, chapters: &[Chapter]) {
    for (index, chapter) in chapters.iter().enumerate() {
        args.extend([
            format!("-metadata:c:{index}"),
            format!("title={}", chapter.title),
        ]);
    }
}

pub(crate) fn sanitize_filename(value: &str) -> String {
    let mut value = value
        .chars()
        .map(|c| {
            if c.is_control() || "<>:\"/\\|?*".contains(c) {
                '_'
            } else {
                c
            }
        })
        .take(120)
        .collect::<String>();
    truncate_filename_bytes(&mut value, 240);
    value = value.trim().trim_matches('.').trim_end().to_string();
    if value.is_empty() {
        value = "media".into();
    }
    let name = value.split('.').next().unwrap_or("").to_ascii_uppercase();
    if matches!(
        name.as_str(),
        "CON"
            | "PRN"
            | "AUX"
            | "NUL"
            | "COM1"
            | "COM2"
            | "COM3"
            | "COM4"
            | "COM5"
            | "COM6"
            | "COM7"
            | "COM8"
            | "COM9"
            | "LPT1"
            | "LPT2"
            | "LPT3"
            | "LPT4"
            | "LPT5"
            | "LPT6"
            | "LPT7"
            | "LPT8"
            | "LPT9"
    ) {
        value.insert(0, '_');
    }
    value
}

fn truncate_filename_bytes(value: &mut String, limit: usize) {
    if value.len() > limit {
        let mut end = limit;
        while !value.is_char_boundary(end) {
            end -= 1;
        }
        value.truncate(end);
    }
}

fn bounded_filename(value: &str) -> String {
    let mut value = value.to_owned();
    if value.len() > 240 {
        // Keep media IDs and multi-part attachment extensions when a long title must be shortened.
        let mut suffix = value
            .rfind(" [")
            .map(|index| value.split_off(index))
            .unwrap_or_else(|| {
                value
                    .find('.')
                    .map(|index| value.split_off(index))
                    .unwrap_or_default()
            });
        if suffix.len() > 240 {
            let extension = suffix
                .rfind('.')
                .map(|index| suffix.split_off(index))
                .unwrap_or_default();
            truncate_filename_bytes(&mut suffix, 240usize.saturating_sub(extension.len()));
            suffix.push_str(&extension);
        }
        truncate_filename_bytes(&mut value, 240usize.saturating_sub(suffix.len()));
        value.push_str(&suffix);
    }
    value
}

fn output_filename(task: &Task, extension: &str) -> String {
    let title = sanitize_filename(&task.title)
        .chars()
        .take(80)
        .collect::<String>();
    let source_id = task
        .manifest
        .as_ref()
        .map(|m| text(&m.metadata, "id"))
        .filter(|v| !v.is_empty())
        .unwrap_or(&task.id[..8]);
    let suffix_bytes = task
        .options
        .filename_template
        .replace("%(title)s", "")
        .replace("%(id)s", &sanitize_filename(source_id))
        .replace("%(ext)s", extension)
        .len();
    let mut title = title;
    truncate_filename_bytes(&mut title, 220usize.saturating_sub(suffix_bytes));
    let template = task
        .options
        .filename_template
        .replace("%(title)s", &title)
        .replace("%(id)s", &sanitize_filename(source_id))
        .replace("%(ext)s", extension);
    let name = sanitize_filename(&template);
    if name.ends_with(&format!(".{extension}")) {
        name
    } else {
        format!("{name}.{extension}")
    }
}

#[derive(Debug, Clone)]
struct Cue {
    start: f64,
    end: f64,
    text: String,
}
fn parse_srt(value: &str) -> Result<Vec<Cue>> {
    let normalized = value.replace("\r\n", "\n");
    let mut result = Vec::new();
    for block in normalized.split("\n\n").filter(|b| !b.trim().is_empty()) {
        let lines = block.lines().collect::<Vec<_>>();
        let Some(index) = lines.iter().position(|l| l.contains("-->")) else {
            continue;
        };
        let (start, end) = lines[index].split_once("-->").unwrap();
        let start = parse_srt_time(start.trim())?;
        let end = parse_srt_time(
            end.split_whitespace()
                .next()
                .context("Subtitle end is missing")?,
        )?;
        ensure!(end >= start, "Subtitle range is reversed");
        result.push(Cue {
            start,
            end,
            text: lines[index + 1..].join("\n"),
        });
    }
    Ok(result)
}
fn parse_srt_time(s: &str) -> Result<f64> {
    parse_time(&s.replace(',', "."))?.context("Subtitle timestamp is missing")
}
fn retime_cues(cues: &[Cue], ranges: &[(f64, f64)], offset: f64) -> Vec<Cue> {
    let mut output = Vec::new();
    let mut elapsed = offset;
    for (a, b) in ranges {
        for cue in cues {
            let start = cue.start.max(*a);
            let end = cue.end.min(*b);
            if end > start {
                output.push(Cue {
                    start: elapsed + start - a,
                    end: elapsed + end - a,
                    text: cue.text.clone(),
                });
            }
        }
        elapsed += b - a;
    }
    output.sort_by(|a, b| a.start.total_cmp(&b.start));
    output
}
fn format_srt(cues: &[Cue]) -> String {
    let mut output = String::new();
    for (index, cue) in cues.iter().enumerate() {
        output.push_str(&format!(
            "{}\n{} --> {}\n{}\n\n",
            index + 1,
            srt_time(cue.start),
            srt_time(cue.end),
            cue.text
        ));
    }
    output
}
fn srt_time(value: f64) -> String {
    let ms = (value * 1000.0).round() as u64;
    format!(
        "{:02}:{:02}:{:02},{:03}",
        ms / 3_600_000,
        ms / 60_000 % 60,
        ms / 1000 % 60,
        ms % 1000
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn legacy_publication_backup_names_remain_unchanged() {
        let task_id = "11111111-1111-4111-8111-111111111111";
        let target = PathBuf::from("output/video.mp4");
        let mut journal = Publication {
            committed: false,
            temporary: target.with_file_name(format!(".video.mp4.{task_id}.publish")),
            target: target.clone(),
            hash: String::new(),
            size: 0,
            duration: 0.0,
            auxiliary: vec![],
            backup: None,
        };
        assert_eq!(
            publication_files(&journal, task_id).1[0],
            target.with_file_name(format!(".video.mp4.{task_id}.before-publication"))
        );
        let conversion = target.with_file_name(format!(".video.mp4.{task_id}.before-conversion"));
        journal.backup = Some(conversion.clone());
        assert_eq!(publication_files(&journal, task_id).1[0], conversion);
    }

    #[test]
    fn unicode_filenames_fit_component_limits_and_keep_media_ids_and_extensions() {
        let mut task = crate::tests::paused_task(Default::default());
        task.title = "多字节😀视频标题".repeat(40);
        task.manifest = Some(SourceManifest {
            version: 1,
            directory: PathBuf::new(),
            files: vec![],
            metadata: json!({"id":"7695747024182590437"}),
            duration: None,
            chapters: vec![],
            sponsor_segments: vec![],
            output_extension: "mp4".into(),
        });
        let filename = output_filename(&task, "mp4");
        assert!(filename.len() <= 220, "{filename}");
        assert!(filename.ends_with(" [7695747024182590437].mp4"));
        let sidecar = PathBuf::from(&filename).with_extension("info.json");
        assert!(sidecar.file_name().unwrap().len() <= 240);
        assert!(sanitize_filename(&task.title).len() <= 240);
        for name in [
            format!(
                "{} [assets-12345678].info.json",
                sanitize_filename(&task.title)
            ),
            format!(
                "{}.chapter-01-{}.mkv",
                filename,
                sanitize_filename(&task.title)
            ),
            format!("{}.info.json", "😀".repeat(150)),
        ] {
            let bounded = bounded_filename(&name);
            assert!(bounded.len() <= 240, "{} bytes: {bounded}", bounded.len());
            assert_eq!(
                Path::new(&bounded).extension(),
                Path::new(&name).extension()
            );
        }
    }

    #[tokio::test]
    async fn long_unicode_publication_replaces_and_recovers_with_short_journal_files() -> Result<()>
    {
        let temp = tempfile::tempdir()?;
        let app = App::open(crate::AppConfig {
            data_dir: temp.path().join("data"),
            download_dir: temp.path().join("output"),
            tool_dir: None,
        })
        .await?;
        let mut task = crate::tests::paused_task(Default::default());
        task.kind = "assets".into();
        let target = app.config.download_dir.join(format!(
            "{} [7695747024182590437].info.json",
            "视".repeat(70)
        ));
        assert!(target.file_name().unwrap().len() <= 255);
        assert!(
            format!(
                ".{}.{}.publish",
                target.file_name().unwrap().to_string_lossy(),
                task.id
            )
            .len()
                > 255
        );
        write_sync(&target, b"old metadata")?;
        let source = app.config.data_dir.join("source.json");
        write_sync(&source, b"new metadata")?;
        let output = make_auxiliary(&source, target.clone(), &task.id)?;
        let other = make_auxiliary(&source, target.clone(), &task.id)?;
        assert_ne!(output.temporary, other.temporary);
        std::fs::remove_file(other.temporary)?;
        task.previous_outputs = vec![target.clone()];
        task.publication = Some(Publication {
            committed: false,
            temporary: output.temporary.clone(),
            target: target.clone(),
            hash: output.hash,
            size: output.size,
            duration: 0.0,
            auxiliary: vec![],
            backup: None,
        });
        app.db.insert_task(&task)?;
        let (_, backups) = publication_files(task.publication.as_ref().unwrap(), &task.id);
        assert!(backups[0].file_name().unwrap().len() < 100);
        assert!(output.temporary.file_name().unwrap().len() < 100);
        std::fs::rename(&target, &backups[0])?;
        std::fs::rename(&output.temporary, &target)?;
        prepare_redownload(&app, &task).await?;
        assert_eq!(std::fs::read(&target)?, b"old metadata");
        assert!(!backups[0].exists() && !output.temporary.exists());
        let output = make_auxiliary(&source, target.clone(), &task.id)?;
        task.publication = Some(Publication {
            committed: false,
            temporary: output.temporary.clone(),
            target: target.clone(),
            hash: output.hash,
            size: output.size,
            duration: 0.0,
            auxiliary: vec![],
            backup: None,
        });
        app.db.save_task(&task)?;
        publish(&app, task, &CancellationToken::new()).await?;
        assert_eq!(std::fs::read(&target)?, b"new metadata");
        assert!(!output.temporary.exists());
        app.shutdown().await?;
        Ok(())
    }

    #[test]
    fn conversion_preview_selects_copy_or_compatible_encoders() -> Result<()> {
        let h264 = json!({"streams":[{"codec_type":"video","codec_name":"h264"},{"codec_type":"audio","codec_name":"aac"}]});
        let plan = conversion_plan(&h264, "mp4", "auto", "")?;
        assert_eq!(plan["strategy"], "remux");
        assert_eq!(plan["encoder"], "copy");
        assert_eq!(plan["audioEncoder"], "copy");
        let webm = conversion_plan(&h264, "webm", "auto", "")?;
        assert_eq!(webm["strategy"], "transcode");
        assert_eq!(webm["encoder"], "libvpx-vp9");
        assert_eq!(webm["audioEncoder"], "libopus");
        let vp9 = json!({"streams":[{"codec_type":"video","codec_name":"vp9"},{"codec_type":"audio","codec_name":"opus"}]});
        assert_eq!(
            conversion_plan(&vp9, "mp4", "auto", "")?["encoder"],
            "libx264"
        );
        assert!(conversion_plan(&vp9, "mp4", "remux", "").is_err());
        assert!(conversion_plan(&h264, "webm", "transcode", "libx264").is_err());
        assert!(conversion_plan(&h264, "outside", "auto", "").is_err());
        Ok(())
    }

    #[tokio::test]
    async fn failed_replacement_restores_original_media_and_sidecars() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let app = App::open(crate::AppConfig {
            data_dir: temp.path().join("data"),
            download_dir: temp.path().join("output"),
            tool_dir: None,
        })
        .await?;
        let mut task = crate::tests::paused_task(Default::default());
        let target = app.config.download_dir.join("视频.mkv");
        let sidecar = target.with_extension("en.srt");
        write_sync(&target, b"old video")?;
        write_sync(&sidecar, b"old subtitle")?;
        task.stage("completed", "completed", Some(100.0));
        app.db.insert_task(&task)?;
        app.db.complete(
            &task,
            &Media {
                media_info: None,
                id: "old-media".into(),
                task_id: task.id.clone(),
                title: "视频".into(),
                filename: "视频.mkv".into(),
                size: 9,
                duration: 1.0,
                path: target.clone(),
                assets: vec![sidecar.clone()],
            },
        )?;
        let source = app.config.download_dir.join("new-source.bin");
        write_sync(&source, b"new invalid video")?;
        let main = make_auxiliary(&source, target.clone(), &task.id)?;
        write_sync(&source, b"new subtitle")?;
        let subtitle = make_auxiliary(&source, sidecar.clone(), &task.id)?;
        task.publication = Some(Publication {
            committed: false,
            temporary: main.temporary.clone(),
            target: target.clone(),
            hash: main.hash,
            size: main.size,
            duration: 1.0,
            auxiliary: vec![subtitle.clone()],
            backup: None,
        });
        task.stage("processing", "publishing", None);
        app.db.save_task(&task)?;
        // Missing frozen FFprobe makes final validation fail after both files were installed.
        assert!(
            publish(&app, task.clone(), &CancellationToken::new())
                .await
                .is_err()
        );
        assert_eq!(std::fs::read(&target)?, b"old video");
        assert_eq!(std::fs::read(&sidecar)?, b"old subtitle");
        assert_eq!(std::fs::read(&main.temporary)?, b"new invalid video");
        assert_eq!(std::fs::read(&subtitle.temporary)?, b"new subtitle");
        assert_eq!(app.db.media_for_task(&task.id)?.unwrap().id, "old-media");
        app.shutdown().await?;
        Ok(())
    }

    #[tokio::test]
    async fn deleting_failed_asset_publication_preserves_unmanaged_destinations() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let app = App::open(crate::AppConfig {
            data_dir: temp.path().join("data"),
            download_dir: temp.path().join("output"),
            tool_dir: None,
        })
        .await?;
        let mut task = crate::tests::paused_task(Default::default());
        task.kind = "assets".into();
        let target = app.config.download_dir.join("user-notes.txt");
        let source = app.config.download_dir.join("source.bin");
        write_sync(&target, b"user notes")?;
        write_sync(&source, b"downloaded notes")?;
        let output = make_auxiliary(&source, target.clone(), &task.id)?;
        task.publication = Some(Publication {
            committed: false,
            temporary: output.temporary.clone(),
            target: target.clone(),
            hash: output.hash,
            size: output.size,
            duration: 0.0,
            auxiliary: vec![],
            backup: None,
        });
        task.stage("processing_failed", "publishing", None);
        app.db.insert_task(&task)?;
        assert!(
            publish(&app, task.clone(), &CancellationToken::new())
                .await
                .is_err()
        );
        app.command("tasks.delete", json!({"id":task.id,"deleteFiles":true}))
            .await?;
        assert_eq!(std::fs::read(target)?, b"user notes");
        assert!(!output.temporary.exists());
        assert!(app.db.task(&task.id).is_err());
        app.shutdown().await?;
        Ok(())
    }

    #[tokio::test]
    async fn deleting_interrupted_publication_cleans_its_journal_and_restores_kept_files()
    -> Result<()> {
        for delete_files in [false, true] {
            let temp = tempfile::tempdir()?;
            let app = App::open(crate::AppConfig {
                data_dir: temp.path().join("data"),
                download_dir: temp.path().join("output"),
                tool_dir: None,
            })
            .await?;
            let mut task = crate::tests::paused_task(Default::default());
            task.kind = "assets".into();
            let target = app.config.download_dir.join("notes.txt");
            let subtitle = app.config.download_dir.join("notes.en.srt");
            let source = app.config.download_dir.join("source.bin");
            write_sync(&target, b"old notes")?;
            write_sync(&source, b"new notes")?;
            let main = make_auxiliary(&source, target.clone(), &task.id)?;
            let sidecar = make_auxiliary(&source, subtitle.clone(), &task.id)?;
            task.previous_outputs = vec![target.clone()];
            task.publication = Some(Publication {
                committed: false,
                temporary: main.temporary.clone(),
                target: target.clone(),
                hash: main.hash,
                size: main.size,
                duration: 0.0,
                auxiliary: vec![sidecar.clone()],
                backup: None,
            });
            app.db.insert_task(&task)?;
            let (_, backups) = publication_files(task.publication.as_ref().unwrap(), &task.id);
            std::fs::rename(&target, &backups[0])?;
            std::fs::rename(&main.temporary, &target)?;
            app.command(
                "tasks.delete",
                json!({"id":task.id,"deleteFiles":delete_files}),
            )
            .await?;
            assert_eq!(target.exists(), !delete_files);
            if !delete_files {
                assert_eq!(std::fs::read(target)?, b"old notes");
            }
            assert!(!subtitle.exists());
            assert!(!main.temporary.exists());
            assert!(!sidecar.temporary.exists());
            assert!(backups.iter().all(|path| !path.exists()));
            assert!(app.db.task(&task.id).is_err());
            app.shutdown().await?;
        }
        Ok(())
    }

    #[tokio::test]
    async fn deletion_cleans_artifacts_left_after_a_committed_publication() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let app = App::open(crate::AppConfig {
            data_dir: temp.path().join("data"),
            download_dir: temp.path().join("output"),
            tool_dir: None,
        })
        .await?;
        let mut task = crate::tests::paused_task(Default::default());
        task.kind = "assets".into();
        task.stage("completed", "completed", Some(100.0));
        let target = app.config.download_dir.join("notes.txt");
        write_sync(&target, b"new notes")?;
        let main = make_auxiliary(&target, target.clone(), &task.id)?;
        task.publication = Some(Publication {
            committed: false,
            temporary: main.temporary.clone(),
            target: target.clone(),
            hash: main.hash,
            size: main.size,
            duration: 0.0,
            auxiliary: vec![],
            backup: None,
        });
        app.db.insert_task(&task)?;
        let (_, backups) = publication_files(task.publication.as_ref().unwrap(), &task.id);
        write_sync(&backups[0], b"old notes")?;
        app.command("tasks.delete", json!({"id":task.id,"deleteFiles":false}))
            .await?;
        assert_eq!(std::fs::read(target)?, b"new notes");
        assert!(!main.temporary.exists());
        assert!(!backups[0].exists());
        assert!(app.db.task(&task.id).is_err());
        app.shutdown().await?;
        Ok(())
    }

    #[tokio::test]
    async fn deletion_preserves_a_journal_that_cannot_be_recovered_safely() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let app = App::open(crate::AppConfig {
            data_dir: temp.path().join("data"),
            download_dir: temp.path().join("output"),
            tool_dir: None,
        })
        .await?;
        let mut task = crate::tests::paused_task(Default::default());
        task.kind = "assets".into();
        let target = app.config.download_dir.join("notes.txt");
        let source = app.config.download_dir.join("source.bin");
        write_sync(&target, b"old notes")?;
        write_sync(&source, b"new notes")?;
        let main = make_auxiliary(&source, target.clone(), &task.id)?;
        task.previous_outputs = vec![target.clone()];
        task.publication = Some(Publication {
            committed: false,
            temporary: main.temporary.clone(),
            target: target.clone(),
            hash: main.hash,
            size: main.size,
            duration: 0.0,
            auxiliary: vec![],
            backup: None,
        });
        app.db.insert_task(&task)?;
        let (_, backups) = publication_files(task.publication.as_ref().unwrap(), &task.id);
        std::fs::rename(&target, &backups[0])?;
        write_sync(&target, b"external edits")?;
        assert!(
            app.command("tasks.delete", json!({"id":task.id,"deleteFiles":true}))
                .await
                .is_err()
        );
        assert_eq!(std::fs::read(target)?, b"external edits");
        assert_eq!(std::fs::read(&backups[0])?, b"old notes");
        assert!(main.temporary.exists());
        assert!(app.db.task(&task.id)?.publication.is_some());
        app.shutdown().await?;
        Ok(())
    }

    #[tokio::test]
    async fn publication_preflight_does_not_overwrite_unmanaged_sidecars() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let app = App::open(crate::AppConfig {
            data_dir: temp.path().join("data"),
            download_dir: temp.path().join("output"),
            tool_dir: None,
        })
        .await?;
        let mut task = crate::tests::paused_task(Default::default());
        task.kind = "assets".into();
        let target = app.config.download_dir.join("notes.txt");
        let sidecar = app.config.download_dir.join("cover.jpg");
        let source = app.config.download_dir.join("source.bin");
        write_sync(&source, b"new notes")?;
        let main = make_auxiliary(&source, target.clone(), &task.id)?;
        let cover = make_auxiliary(&source, sidecar.clone(), &task.id)?;
        write_sync(&sidecar, b"user-owned image")?;
        task.publication = Some(Publication {
            committed: false,
            temporary: main.temporary,
            target: target.clone(),
            hash: main.hash,
            size: main.size,
            duration: 0.0,
            auxiliary: vec![cover],
            backup: None,
        });
        app.db.insert_task(&task)?;
        assert!(
            publish(&app, task, &CancellationToken::new())
                .await
                .unwrap_err()
                .to_string()
                .contains("unmanaged")
        );
        assert!(!target.exists());
        assert_eq!(std::fs::read(sidecar)?, b"user-owned image");
        app.shutdown().await?;
        Ok(())
    }

    #[tokio::test]
    async fn committed_publication_recovers_cleanup_without_revalidating_or_rolling_back()
    -> Result<()> {
        let temp = tempfile::tempdir()?;
        let config = crate::AppConfig {
            data_dir: temp.path().join("data"),
            download_dir: temp.path().join("output"),
            tool_dir: None,
        };
        let app = App::open(config.clone()).await?;
        let mut task = crate::tests::paused_task(Default::default());
        let target = app.config.download_dir.join("new.mp4");
        let obsolete = app.config.download_dir.join("old.mkv");
        write_sync(&target, b"committed media")?;
        write_sync(&obsolete, b"old media")?;
        let (hash, size) = hash_file(&target)?;
        task.publication = Some(Publication {
            committed: true,
            temporary: target.with_extension("publish"),
            target: target.clone(),
            hash,
            size,
            duration: 1.0,
            auxiliary: vec![],
            backup: None,
        });
        task.manifest = Some(SourceManifest {
            version: 1,
            directory: app.config.download_dir.clone(),
            files: vec![],
            metadata: json!({}),
            duration: Some(1.0),
            chapters: vec![],
            sponsor_segments: vec![],
            output_extension: "mp4".into(),
        });
        task.previous_outputs = vec![obsolete.clone()];
        task.stage("processing", "saving_results", None);
        app.db.insert_task(&task)?;
        app.db.complete(
            &task,
            &Media {
                media_info: None,
                id: "committed-media".into(),
                task_id: task.id.clone(),
                title: "Committed".into(),
                filename: "new.mp4".into(),
                size,
                duration: 1.0,
                path: target.clone(),
                assets: vec![],
            },
        )?;
        let (_, backups) = publication_files(task.publication.as_ref().unwrap(), &task.id);
        write_sync(&backups[0], b"old destination")?;
        app.shutdown().await?;
        drop(app);
        let app = App::open(config).await?;
        tokio::time::timeout(Duration::from_secs(5), async {
            loop {
                let current = app.db.task(&task.id)?;
                ensure!(current.status != "processing_failed", "{:?}", current.error);
                if current.status == "completed" {
                    return Ok::<_, anyhow::Error>(());
                }
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await??;
        assert_eq!(std::fs::read(&target)?, b"committed media");
        assert!(!obsolete.exists());
        assert!(!backups[0].exists());
        assert!(app.db.task(&task.id)?.previous_outputs.is_empty());
        assert_eq!(app.db.media_for_task(&task.id)?.unwrap().path, target);
        // Cancellation after the result commit must finish cleanup rather than restoring the backup.
        let mut interrupted = app.db.task(&task.id)?;
        write_sync(&obsolete, b"another obsolete output")?;
        write_sync(&backups[0], b"another old destination")?;
        interrupted.previous_outputs = vec![obsolete.clone()];
        interrupted.stage("paused", "saving_results", None);
        app.db.save_task(&interrupted)?;
        app.command("tasks.cancel", json!({"id":task.id})).await?;
        prepare_redownload(&app, &interrupted).await?;
        assert_eq!(app.db.task(&task.id)?.status, "completed");
        assert_eq!(std::fs::read(&target)?, b"committed media");
        assert!(!obsolete.exists());
        assert!(!backups[0].exists());
        // A keep-files deletion after a committed interruption must retain the new result.
        interrupted = app.db.task(&task.id)?;
        write_sync(&backups[0], b"last old destination")?;
        interrupted.stage("paused", "saving_results", None);
        app.db.save_task(&interrupted)?;
        app.command("tasks.delete", json!({"id":task.id,"deleteFiles":false}))
            .await?;
        assert_eq!(std::fs::read(&target)?, b"committed media");
        assert!(!backups[0].exists());
        assert_eq!(app.db.task(&task.id)?.status, "retained");
        app.shutdown().await?;
        Ok(())
    }

    #[tokio::test]
    async fn committed_cleanup_failure_warns_and_preserves_paths_for_a_later_retry() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let app = App::open(crate::AppConfig {
            data_dir: temp.path().join("data"),
            download_dir: temp.path().join("output"),
            tool_dir: None,
        })
        .await?;
        let mut task = crate::tests::paused_task(Default::default());
        task.kind = "assets".into();
        let obsolete = app.config.download_dir.join("obsolete.txt");
        std::fs::create_dir(&obsolete)?;
        write_sync(&obsolete.join("external.txt"), b"external replacement")?;
        let target = app.config.download_dir.join("new.txt");
        write_sync(&target, b"committed notes")?;
        let (hash, size) = hash_file(&target)?;
        task.publication = Some(Publication {
            committed: true,
            temporary: target.with_extension("publish"),
            target: target.clone(),
            hash,
            size,
            duration: 0.0,
            auxiliary: vec![],
            backup: None,
        });
        task.previous_outputs = vec![obsolete.clone()];
        task.stage("paused", "saving_results", None);
        app.db.insert_task(&task)?;
        prepare_redownload(&app, &task).await?;
        let saved = app.db.task(&task.id)?;
        assert_eq!(saved.status, "completed");
        assert_eq!(saved.previous_outputs, vec![obsolete.clone()]);
        assert!(
            saved
                .warnings
                .iter()
                .any(|warning| warning.contains("previous output files"))
        );
        assert_eq!(std::fs::read(&target)?, b"committed notes");
        assert!(obsolete.join("external.txt").exists());
        std::fs::remove_file(obsolete.join("external.txt"))?;
        std::fs::remove_dir(&obsolete)?;
        write_sync(&obsolete, b"obsolete notes")?;
        prepare_redownload(&app, &saved).await?;
        let cleaned = app.db.task(&task.id)?;
        assert!(cleaned.previous_outputs.is_empty());
        assert!(cleaned.warnings.is_empty());
        assert!(!obsolete.exists());
        app.shutdown().await?;
        Ok(())
    }

    #[tokio::test]
    async fn completed_publication_is_observable_only_after_obsolete_files_are_removed()
    -> Result<()> {
        let temp = tempfile::tempdir()?;
        let app = App::open(crate::AppConfig {
            data_dir: temp.path().join("data"),
            download_dir: temp.path().join("output"),
            tool_dir: None,
        })
        .await?;
        let mut task = crate::tests::paused_task(Default::default());
        task.kind = "assets".into();
        for index in 0..512 {
            let path = app
                .config
                .download_dir
                .join(format!("obsolete-{index}.txt"));
            write_sync(&path, b"old notes")?;
            task.previous_outputs.push(path);
        }
        let source = app.config.download_dir.join("source.txt");
        let target = app.config.download_dir.join("current.txt");
        write_sync(&source, b"new notes")?;
        let output = make_auxiliary(&source, target.clone(), &task.id)?;
        task.publication = Some(Publication {
            committed: false,
            temporary: output.temporary,
            target,
            hash: output.hash,
            size: output.size,
            duration: 0.0,
            auxiliary: vec![],
            backup: None,
        });
        app.db.insert_task(&task)?;
        let observer_app = app.clone();
        let observer_id = task.id.clone();
        let obsolete = task.previous_outputs.last().unwrap().clone();
        let (ready, started) = std::sync::mpsc::channel();
        let observer = std::thread::spawn(move || -> Result<()> {
            ready.send(())?;
            let deadline = Instant::now() + Duration::from_secs(30);
            loop {
                if observer_app.db.task(&observer_id)?.status == "completed" {
                    ensure!(
                        !obsolete.exists(),
                        "Obsolete managed output remained after successful replacement"
                    );
                    return Ok(());
                }
                ensure!(Instant::now() < deadline, "Publication did not complete");
                std::thread::yield_now();
            }
        });
        started.recv()?;
        publish(&app, task, &CancellationToken::new()).await?;
        observer.join().unwrap()?;
        app.shutdown().await?;
        Ok(())
    }

    #[tokio::test]
    async fn repeated_asset_download_replaces_only_its_managed_files() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let app = App::open(crate::AppConfig {
            data_dir: temp.path().join("data"),
            download_dir: temp.path().join("output"),
            tool_dir: None,
        })
        .await?;
        let mut task = crate::tests::paused_task(Default::default());
        task.kind = "assets".into();
        let target = app.config.download_dir.join("notes.txt");
        let obsolete = app.config.download_dir.join("obsolete.en.srt");
        write_sync(&target, b"old notes")?;
        write_sync(&obsolete, b"obsolete captions")?;
        task.previous_outputs = vec![target.clone(), obsolete.clone()];
        let source = app.config.download_dir.join("source.bin");
        write_sync(&source, b"updated notes")?;
        let output = make_auxiliary(&source, target.clone(), &task.id)?;
        task.publication = Some(Publication {
            committed: false,
            temporary: output.temporary,
            target: target.clone(),
            hash: output.hash,
            size: output.size,
            duration: 0.0,
            auxiliary: vec![],
            backup: None,
        });
        app.db.insert_task(&task)?;
        publish(&app, task.clone(), &CancellationToken::new()).await?;
        assert_eq!(std::fs::read(&target)?, b"updated notes");
        assert!(!obsolete.exists());
        assert!(app.db.media()?.is_empty());
        publish(&app, app.db.task(&task.id)?, &CancellationToken::new()).await?;
        assert_eq!(std::fs::read(target)?, b"updated notes");
        app.shutdown().await?;
        Ok(())
    }

    #[tokio::test]
    async fn redownload_recovers_interrupted_publication_and_preserves_changed_destinations()
    -> Result<()> {
        let temp = tempfile::tempdir()?;
        let app = App::open(crate::AppConfig {
            data_dir: temp.path().join("data"),
            download_dir: temp.path().join("output"),
            tool_dir: None,
        })
        .await?;
        let mut task = crate::tests::paused_task(Default::default());
        task.kind = "assets".into();
        task.status = "cancelled".into();
        let target = app.config.download_dir.join("notes.txt");
        let subtitle = app.config.download_dir.join("notes.en.srt");
        let source = app.config.download_dir.join("source.bin");
        write_sync(&target, b"old notes")?;
        write_sync(&subtitle, b"old captions")?;
        write_sync(&source, b"new notes")?;
        let main = make_auxiliary(&source, target.clone(), &task.id)?;
        write_sync(&source, b"new captions")?;
        let sidecar = make_auxiliary(&source, subtitle.clone(), &task.id)?;
        task.previous_outputs = vec![target.clone(), subtitle.clone()];
        task.publication = Some(Publication {
            committed: false,
            temporary: main.temporary.clone(),
            target: target.clone(),
            hash: main.hash,
            size: main.size,
            duration: 0.0,
            auxiliary: vec![sidecar.clone()],
            backup: None,
        });
        app.db.insert_task(&task)?;
        let (_, backups) = publication_files(task.publication.as_ref().unwrap(), &task.id);
        // Simulate a crash between installing the main file and its subtitle.
        std::fs::rename(&target, &backups[0])?;
        std::fs::rename(&main.temporary, &target)?;
        prepare_redownload(&app, &task).await?;
        assert_eq!(std::fs::read(&target)?, b"old notes");
        assert_eq!(std::fs::read(&subtitle)?, b"old captions");
        assert!(!backups[0].exists());
        assert!(!sidecar.temporary.exists());
        assert!(app.db.task(&task.id)?.publication.is_none());

        // An externally edited destination must block reset instead of losing its recovery journal.
        write_sync(&source, b"new notes")?;
        let main = make_auxiliary(&source, target.clone(), &task.id)?;
        task.publication.as_mut().unwrap().temporary = main.temporary;
        app.db.save_task(&task)?;
        let (_, backups) = publication_files(task.publication.as_ref().unwrap(), &task.id);
        std::fs::rename(&target, &backups[0])?;
        write_sync(&target, b"external changes")?;
        let error = prepare_redownload(&app, &task)
            .await
            .unwrap_err()
            .to_string();
        assert!(error.contains("Retry processing"), "{error}");
        assert_eq!(std::fs::read(&target)?, b"external changes");
        assert_eq!(std::fs::read(&backups[0])?, b"old notes");
        assert!(app.db.task(&task.id)?.publication.is_some());
        app.shutdown().await?;
        Ok(())
    }
    #[tokio::test]
    async fn asset_publication_is_persistent_idempotent_and_not_media() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let config = crate::AppConfig {
            data_dir: temp.path().join("data"),
            download_dir: temp.path().join("output"),
            tool_dir: None,
        };
        let app = App::open(config.clone()).await?;
        let mut task = crate::tests::paused_task(Default::default());
        task.kind = "assets".into();
        task.title = "Podcast".into();
        task.options.organize_task_folder = true;
        let directory = app
            .config
            .data_dir
            .join("tasks")
            .join(&task.id)
            .join("sources");
        std::fs::create_dir_all(&directory)?;
        let path = directory.join("description.txt");
        write_sync(&path, b"Episode notes")?;
        let (hash, size) = hash_file(&path)?;
        task.manifest = Some(SourceManifest {
            version: 1,
            directory,
            files: vec![SourceFile {
                path,
                hash,
                size,
                kind: "description".into(),
                format_id: String::new(),
                language: None,
            }],
            metadata: json!({"title":"Podcast"}),
            duration: None,
            chapters: vec![],
            sponsor_segments: vec![],
            output_extension: String::new(),
        });
        app.db.insert_task(&task)?;
        process_task(&app, &task.id, &CancellationToken::new()).await?;
        let published = app.db.task(&task.id)?;
        assert_eq!(
            published.output_path.as_ref().unwrap().parent().unwrap(),
            app.config.download_dir.join("Podcast")
        );
        assert_eq!(published.status, "completed");
        assert!(app.db.media()?.is_empty());
        assert_eq!(published.public()["files"][0]["kind"], "description");
        assert_eq!(published.public()["files"][0]["size"], 13);
        assert!(!published.public().to_string().contains("Episode notes"));
        assert!(
            !published
                .public()
                .to_string()
                .contains(temp.path().to_string_lossy().as_ref())
        );
        assert_eq!(
            std::fs::read(published.output_path.as_ref().unwrap())?,
            b"Episode notes"
        );
        process_task(&app, &task.id, &CancellationToken::new()).await?;
        app.shutdown().await?;
        let reopened = App::open(config).await?;
        assert_eq!(reopened.db.task(&task.id)?.files[0].size, 13);
        assert!(reopened.db.media()?.is_empty());
        reopened.shutdown().await?;
        Ok(())
    }

    #[tokio::test]
    async fn conversion_rejects_paths_and_reports_missing_saved_inputs() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let app = App::open(crate::AppConfig {
            data_dir: temp.path().join("data"),
            download_dir: temp.path().join("output"),
            tool_dir: None,
        })
        .await?;
        let mut task = crate::tests::paused_task(Default::default());
        task.stage("completed", "completed", Some(100.0));
        let path = app.config.download_dir.join("source.mp4");
        write_sync(&path, b"original")?;
        task.output_path = Some(path.clone());
        app.db.insert_task(&task)?;
        app.db.complete(
            &task,
            &Media {
                media_info: None,
                id: "media".into(),
                task_id: task.id.clone(),
                title: "Source".into(),
                filename: "source.mp4".into(),
                size: 8,
                duration: 1.0,
                path: path.clone(),
                assets: vec![],
            },
        )?;
        for (changes, expected) in [
            (json!({"path":"outside"}), "Unsupported conversion option"),
            (json!({"embedSubtitles":true}), "No saved subtitles"),
            (json!({"embedThumbnail":true}), "No saved thumbnail"),
            (
                json!({"embedMetadata":true}),
                "Saved metadata is unavailable",
            ),
            (json!({"embedChapters":true}), "No saved chapters"),
        ] {
            let error = app
                .research_convert_media_options("media", &changes)
                .await
                .unwrap_err()
                .to_string();
            assert!(error.contains(expected), "{error}");
        }
        assert_eq!(app.db.tasks()?.len(), 1);
        assert_eq!(std::fs::read(path)?, b"original");
        let mut serialized = serde_json::to_value(&task)?;
        serialized.as_object_mut().unwrap().remove("kind");
        serialized.as_object_mut().unwrap().remove("files");
        let legacy: Task = serde_json::from_value(serialized)?;
        assert_eq!(legacy.kind, "media");
        assert!(legacy.files.is_empty());
        app.shutdown().await?;
        Ok(())
    }

    #[tokio::test]
    #[ignore = "requires bundled FFmpeg/FFprobe runtimes"]
    async fn all_audio_tracks_maps_combined_and_separate_inputs_without_extra_video() -> Result<()>
    {
        let temp = tempfile::tempdir()?;
        let tool_dir = std::env::var_os("HUIFA_TEST_TOOLS")
            .map(PathBuf::from)
            .unwrap_or_else(|| {
                PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../runtime-tools")
            });
        let app = App::open(crate::AppConfig {
            data_dir: temp.path().join("data"),
            download_dir: temp.path().join("output"),
            tool_dir: Some(tool_dir),
        })
        .await?;
        let mut task = crate::tests::paused_task(app.freeze_tools().await?);
        task.options.audio_track = "all".into();
        task.options.write_thumbnail = false;
        task.options.write_info_json = false;
        let directory = app
            .config
            .data_dir
            .join("tasks")
            .join(&task.id)
            .join("sources");
        std::fs::create_dir_all(&directory)?;
        let mut files = Vec::new();
        for (index, language) in ["en", "zh", "ja"].into_iter().enumerate() {
            let path = directory.join(format!("source-{index}.mkv"));
            let mut args: Vec<String> = ["-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
                .into_iter()
                .map(str::to_string)
                .collect();
            if index < 2 {
                args.extend([
                    "-f".into(),
                    "lavfi".into(),
                    "-i".into(),
                    "testsrc2=size=160x90:rate=12".into(),
                ]);
            }
            args.extend([
                "-f".into(),
                "lavfi".into(),
                "-i".into(),
                format!("sine=frequency={}:sample_rate=44100", 440 + index * 220),
                "-t".into(),
                "1".into(),
                "-c:v".into(),
                "libx264".into(),
                "-preset".into(),
                "ultrafast".into(),
                "-c:a".into(),
                "aac".into(),
                "-threads".into(),
                "1".into(),
                path.to_string_lossy().into_owned(),
            ]);
            process::run(
                &app.tool("ffmpeg")?,
                &args,
                None,
                &CancellationToken::new(),
                None,
            )
            .await?;
            let (hash, size) = hash_file(&path)?;
            files.push(SourceFile {
                path,
                hash,
                size,
                kind: if index < 2 { "video" } else { "audio" }.into(),
                format_id: index.to_string(),
                language: Some(language.into()),
            });
        }
        task.manifest = Some(SourceManifest {
            version: 1,
            directory,
            files,
            metadata: json!({"id":"all-audio"}),
            duration: Some(1.0),
            chapters: vec![],
            sponsor_segments: vec![],
            output_extension: "mkv".into(),
        });
        app.db.insert_task(&task)?;
        process_task(&app, &task.id, &CancellationToken::new()).await?;
        let completed = app.db.task(&task.id)?;
        let probe = probe_file(
            &app,
            &completed,
            completed.output_path.as_ref().unwrap(),
            &CancellationToken::new(),
        )
        .await?;
        assert_eq!(count_streams(&probe, "video"), 1);
        assert_eq!(count_streams(&probe, "audio"), 3);
        let languages = streams(&probe)
            .filter(|stream| text(stream, "codec_type") == "audio")
            .map(|stream| text(&stream["tags"], "language").to_string())
            .collect::<Vec<_>>();
        assert_eq!(languages, ["en", "zh", "ja"]);
        app.shutdown().await?;
        Ok(())
    }

    #[tokio::test]
    #[ignore = "requires bundled FFmpeg/FFprobe runtimes"]
    async fn conversion_options_extract_audio_and_embed_saved_assets_without_replacing_source()
    -> Result<()> {
        let temp = tempfile::tempdir()?;
        let tool_dir = std::env::var_os("HUIFA_TEST_TOOLS")
            .map(PathBuf::from)
            .unwrap_or_else(|| {
                PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../runtime-tools")
            });
        let app = App::open(crate::AppConfig {
            data_dir: temp.path().join("data"),
            download_dir: temp.path().join("output"),
            tool_dir: Some(tool_dir),
        })
        .await?;
        let path = app.config.download_dir.join("original.mp4");
        let args: Vec<String> = [
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
            "2",
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
        .chain([path.to_string_lossy().into_owned()])
        .collect();
        process::run(
            &app.tool("ffmpeg")?,
            &args,
            None,
            &CancellationToken::new(),
            None,
        )
        .await?;
        let (original_hash, size) = hash_file(&path)?;
        let subtitle = app.config.download_dir.join("original.en.srt");
        write_sync(
            &subtitle,
            b"1\n00:00:00,100 --> 00:00:01,500\nPodcast subtitle\n\n",
        )?;
        let thumbnail = app.config.download_dir.join("original.jpg");
        image::RgbImage::from_pixel(16, 16, image::Rgb([50, 20, 80])).save(&thumbnail)?;
        let mut task = crate::tests::paused_task(app.freeze_tools().await?);
        task.output_path = Some(path.clone());
        task.title = "Podcast".into();
        task.manifest = Some(SourceManifest {
            version: 1,
            directory: app.config.download_dir.clone(),
            files: vec![],
            metadata: json!({"title":"Podcast","uploader":"Author"}),
            duration: Some(2.0),
            chapters: vec![Chapter {
                start: 0.0,
                end: 2.0,
                title: "Episode".into(),
            }],
            sponsor_segments: vec![],
            output_extension: "mp4".into(),
        });
        task.stage("completed", "completed", Some(100.0));
        app.db.insert_task(&task)?;
        app.db.complete(
            &task,
            &Media {
                media_info: None,
                id: "original-media".into(),
                task_id: task.id.clone(),
                title: task.title.clone(),
                filename: "original.mp4".into(),
                size,
                duration: 2.0,
                path: path.clone(),
                assets: vec![subtitle, thumbnail],
            },
        )?;
        let original_info = task_info(&app, &task.id).await?;
        assert_eq!(original_info["mediaInfo"]["width"], 160);
        assert_eq!(original_info["mediaInfo"]["height"], 90);
        assert_eq!(original_info["size"], size);
        assert!(
            app.db
                .media_for_task(&task.id)?
                .unwrap()
                .media_info
                .is_some()
        );
        // Persisted summaries do not depend on re-reading a file for every detail refresh.
        assert_eq!(task_info(&app, &task.id).await?, original_info);
        for options in [
            json!({"contentMode":"audio","audioFormat":"mp3"}),
            json!({"container":"mkv","encoder":"libx264","embedSubtitles":true,"embedThumbnail":true,"embedMetadata":true,"embedChapters":true,"subtitleFormat":"ass"}),
        ] {
            let queued = app
                .research_convert_media_options("original-media", &options)
                .await?;
            let id = queued["id"].as_str().unwrap();
            let completed = tokio::time::timeout(Duration::from_secs(90), async {
                loop {
                    let current = app.db.task(id)?;
                    ensure!(
                        !matches!(current.status.as_str(), "failed" | "processing_failed"),
                        "{:?}",
                        current.error
                    );
                    if current.status == "completed" {
                        return Ok::<_, anyhow::Error>(current);
                    }
                    tokio::time::sleep(Duration::from_millis(100)).await;
                }
            })
            .await??;
            assert_ne!(completed.id, task.id);
            assert!(completed.source_info.is_some());
            assert!(completed.media_info.is_some());
            assert!(completed.size.is_some_and(|size| size > 0));
            let probe = probe_file(
                &app,
                &completed,
                completed.output_path.as_ref().unwrap(),
                &CancellationToken::new(),
            )
            .await?;
            if options["contentMode"] == "audio" {
                assert_eq!(count_streams(&probe, "video"), 0);
                assert_eq!(
                    text(
                        streams(&probe)
                            .find(|s| text(s, "codec_type") == "audio")
                            .unwrap(),
                        "codec_name"
                    ),
                    "mp3"
                );
            } else {
                assert_eq!(count_streams(&probe, "subtitle"), 1);
                assert!(
                    streams(&probe).any(|s| text(s, "codec_type") == "attachment"
                        || s["disposition"]["attached_pic"] == 1)
                );
                assert_eq!(probe["format"]["tags"]["title"], "Podcast");
            }
            assert_eq!(hash_file(&path)?.0, original_hash);
            assert_eq!(app.db.task(&task.id)?.status, "completed");
        }
        let mut source_id = task.id.clone();
        for (container, expected_strategy, video_codec, audio_codec) in [
            ("mkv", "remux", "h264", "aac"),
            ("webm", "transcode", "vp9", "opus"),
            ("mp4", "transcode", "h264", "aac"),
        ] {
            let changes = json!({"container":container});
            let preview = preview_conversion(&app, &source_id, &changes, "auto").await?;
            assert_eq!(preview["strategy"], expected_strategy);
            let queued =
                queue_conversion_options_with_strategy(&app, &source_id, &changes, "auto").await?;
            let id = queued["id"].as_str().unwrap();
            let completed = tokio::time::timeout(Duration::from_secs(90), async {
                loop {
                    let current = app.db.task(id)?;
                    ensure!(
                        !matches!(current.status.as_str(), "failed" | "processing_failed"),
                        "{:?}",
                        current.error
                    );
                    if current.status == "completed" {
                        return Ok::<_, anyhow::Error>(current);
                    }
                    tokio::time::sleep(Duration::from_millis(100)).await;
                }
            })
            .await??;
            let probe = probe_file(
                &app,
                &completed,
                completed.output_path.as_ref().unwrap(),
                &CancellationToken::new(),
            )
            .await?;
            assert_eq!(
                text(
                    streams(&probe)
                        .find(|stream| text(stream, "codec_type") == "video")
                        .unwrap(),
                    "codec_name"
                ),
                video_codec
            );
            assert_eq!(
                text(
                    streams(&probe)
                        .find(|stream| text(stream, "codec_type") == "audio")
                        .unwrap(),
                    "codec_name"
                ),
                audio_codec
            );
            assert_eq!(hash_file(&path)?.0, original_hash);
            source_id = completed.id;
        }
        assert_eq!(app.db.media()?.len(), 6);

        // Re-download publication migrates this task out of an old short-ID directory only after success.
        let legacy = app.config.download_dir.join("Podcast").join(&task.id[..8]);
        std::fs::create_dir_all(&legacy)?;
        let old_path = legacy.join("original.mp4");
        let old_subtitle = legacy.join("original.en.srt");
        copy_sync(&path, &old_path)?;
        write_sync(&old_subtitle, b"obsolete subtitle")?;
        let previous = app.db.media_for_task(&task.id)?.unwrap();
        app.db.complete(
            &task,
            &Media {
                media_info: None,
                path: old_path.clone(),
                assets: vec![old_subtitle.clone()],
                ..previous
            },
        )?;
        let target = legacy.parent().unwrap().join("original.mp4");
        let prepared = make_auxiliary(&path, target.clone(), &task.id)?;
        let mut replacement = app.db.task(&task.id)?;
        replacement.stage("processing", "publishing", None);
        replacement.publication = Some(Publication {
            committed: false,
            temporary: prepared.temporary,
            target: target.clone(),
            hash: prepared.hash,
            size: prepared.size,
            duration: 2.0,
            auxiliary: vec![],
            backup: None,
        });
        app.db.save_task(&replacement)?;
        publish(&app, replacement.clone(), &CancellationToken::new()).await?;
        assert!(target.is_file());
        assert!(!old_path.exists());
        assert!(!old_subtitle.exists());
        assert!(!legacy.exists());
        assert_eq!(
            app.db.media_for_task(&task.id)?.unwrap().id,
            "original-media"
        );
        // Repeating an already committed journal is idempotent.
        publish(&app, app.db.task(&task.id)?, &CancellationToken::new()).await?;
        app.shutdown().await?;
        Ok(())
    }
    #[test]
    fn cuts_remove_overlapping_sponsors_and_retime_captions() {
        let options=DownloadOptions::parse(json!({"sectionStart":"2","sectionEnd":"10","sponsorblockMode":"remove","sponsorblockCategories":["sponsor"]})).unwrap();
        let ranges = keep_ranges(
            20.0,
            &options,
            &[
                Chapter {
                    start: 3.0,
                    end: 5.0,
                    title: "s".into(),
                },
                Chapter {
                    start: 4.0,
                    end: 6.0,
                    title: "s".into(),
                },
            ],
        )
        .unwrap();
        assert_eq!(ranges, vec![(2.0, 3.0), (6.0, 10.0)]);
        let cues = parse_srt("1\n00:00:02,500 --> 00:00:07,000\ntext\n\n").unwrap();
        let cues = retime_cues(&cues, &ranges, 0.1);
        assert_eq!(cues.len(), 2);
        assert!((cues[1].start - 1.1).abs() < 0.001);
        assert!((cues[1].end - 2.1).abs() < 0.001);
    }
    #[test]
    fn filenames_cannot_escape_or_use_windows_devices() {
        assert_eq!(sanitize_filename("../CON"), "_CON");
        assert_eq!(sanitize_filename("CON"), "_CON");
        assert!(!sanitize_filename("../../foo\\bar").contains('/'));
    }
}

use crate::{
    App, DownloadOptions,
    download::{hash_file, write_sync},
    model::{
        AuxiliaryPublication, Chapter, Media, Publication, SourceFile, SourceManifest, Task,
        parse_time, text,
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
        task.status == "completed",
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

pub(crate) async fn process_task(
    app: &Arc<App>,
    id: &str,
    cancel: &CancellationToken,
) -> Result<()> {
    let task = app.db.task(id)?;
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
        if separate && input.kind != "audio" {
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
    let audio_codec = audio_codec(&task.options, filtering, extension);
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
        app.config
            .download_dir
            .join(sanitize_filename(&task.title))
            .join(&task.id[..8])
    } else {
        app.config.download_dir.clone()
    };
    crate::reject_links(&directory)?;
    std::fs::create_dir_all(&directory)?;
    let mut target = if task.conversion {
        task.output_path
            .clone()
            .context("Conversion task has no output path")?
    } else {
        directory.join(output_filename(&task, extension))
    };
    if !task.conversion && target.exists() {
        target = target.with_file_name(format!(
            "{} [{}].{}",
            target.file_stem().unwrap().to_string_lossy(),
            &task.id[..8],
            extension
        ));
    }
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
            auxiliary.push(make_auxiliary(&source.path, destination)?);
        }
        if task.options.write_thumbnail
            && let Some(path) = &thumbnail
        {
            auxiliary.push(make_auxiliary(
                path,
                target.with_extension(path.extension().unwrap()),
            )?);
        }
        for (path, language) in &subtitles {
            auxiliary.push(make_auxiliary(
                path,
                target.with_extension(format!(
                    "{language}.{}",
                    path.extension().unwrap().to_string_lossy()
                )),
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
                let name = format!(
                    "{}.chapter-{:02}-{}.{}",
                    target.file_stem().unwrap().to_string_lossy(),
                    index + 1,
                    sanitize_filename(&chapter.title),
                    extension
                );
                auxiliary.push(make_auxiliary(&path, directory.join(name))?);
            }
        }
    }
    let temporary = target.with_file_name(format!(
        ".{}.{}.publish",
        target.file_name().unwrap().to_string_lossy(),
        task.id
    ));
    copy_sync(&output, &temporary)?;
    let (hash, size) = hash_file(&temporary)?;
    let mut prepared = app.db.task(id)?;
    prepared.publication = Some(Publication {
        temporary,
        target: target.clone(),
        hash,
        size,
        duration: final_duration,
        auxiliary,
        backup: task.conversion.then(|| {
            target.with_file_name(format!(
                ".{}.{}.before-conversion",
                target.file_name().unwrap().to_string_lossy(),
                task.id
            ))
        }),
    });
    prepared.stage("processing", "publishing", None);
    app.save_task(&prepared)?;
    publish(app, prepared, cancel).await
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

fn make_auxiliary(source: &Path, target: PathBuf) -> Result<AuxiliaryPublication> {
    let temporary = target.with_file_name(format!(
        ".{}.publish",
        target.file_name().unwrap().to_string_lossy()
    ));
    copy_sync(source, &temporary)?;
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

async fn publish(app: &Arc<App>, mut task: Task, cancel: &CancellationToken) -> Result<()> {
    let publication = task
        .publication
        .clone()
        .context("Publication journal is missing")?;
    for path in std::iter::once(&publication.target)
        .chain(std::iter::once(&publication.temporary))
        .chain(publication.backup.iter())
        .chain(
            publication
                .auxiliary
                .iter()
                .flat_map(|a| [&a.target, &a.temporary]),
        )
    {
        crate::reject_links(path)?;
        ensure!(
            path.parent()
                .context("Publication path has no directory")?
                .canonicalize()?
                .starts_with(&app.config.download_dir),
            "Publication path is outside the owned downloads directory"
        );
    }
    if cancel.is_cancelled() {
        bail!("Operation interrupted");
    }
    let target_matches = publication.target.exists()
        && hash_file(&publication.target)? == (publication.hash.clone(), publication.size);
    if !target_matches {
        ensure!(
            publication.temporary.exists(),
            "Publication output is missing; saved sources remain available"
        );
        ensure!(
            hash_file(&publication.temporary)? == (publication.hash.clone(), publication.size),
            "Prepared output changed after validation"
        );
        if publication.target.exists() {
            let backup = publication
                .backup
                .as_ref()
                .context("Publication filename is already occupied")?;
            if !backup.exists() {
                let original = task
                    .manifest
                    .as_ref()
                    .and_then(|m| m.files.iter().find(|s| s.path == publication.target))
                    .context("Conversion source is missing from its manifest")?;
                ensure!(
                    hash_file(&publication.target)? == (original.hash.clone(), original.size),
                    "Conversion source changed before publication"
                );
                std::fs::rename(&publication.target, backup)?;
            } else {
                bail!("Conversion destination is occupied; original backup is preserved");
            }
        }
        std::fs::rename(&publication.temporary, &publication.target)?;
        sync_directory(publication.target.parent().unwrap())?;
    }
    for auxiliary in &publication.auxiliary {
        if auxiliary.target.exists()
            && hash_file(&auxiliary.target)? == (auxiliary.hash.clone(), auxiliary.size)
        {
            continue;
        }
        ensure!(
            !auxiliary.target.exists(),
            "An output sidecar filename is occupied"
        );
        ensure!(
            hash_file(&auxiliary.temporary)? == (auxiliary.hash.clone(), auxiliary.size),
            "Prepared output sidecar changed"
        );
        std::fs::rename(&auxiliary.temporary, &auxiliary.target)?;
    }
    sync_directory(publication.target.parent().unwrap())?;
    app.set_stage(&task.id, "processing", "validating_publication", None)?;
    let _ = probe_file(app, &task, &publication.target, cancel).await?;
    let previous = app.db.media_for_task(&task.id)?;
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
    task = app.db.task(&task.id)?;
    task.output_path = Some(publication.target.clone());
    task.error = None;
    task.stage("completed", "completed", Some(100.0));
    let media = app.db.complete(&task, &media)?;
    app.emit("task.upsert", task.public());
    app.emit("media.upsert", media.public());
    if let Some(backup) = publication.backup
        && backup.exists()
    {
        std::fs::remove_file(backup)?;
    }
    Ok(())
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

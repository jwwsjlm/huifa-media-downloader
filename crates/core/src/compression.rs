use crate::{
    App,
    model::{CompressionOptions, MediaInfo, Task, text},
    process::{self, LineCallback},
};
use anyhow::{Context, Result, bail, ensure};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::{HashMap, VecDeque},
    io::Read,
    path::{Path, PathBuf},
    sync::{Arc, Mutex, OnceLock},
    time::{Duration, Instant, SystemTime},
};
use tokio_util::sync::CancellationToken;

const ENCODERS: [(&str, &str); 6] = [
    ("libx265", "CPU · HEVC"),
    ("hevc_nvenc", "NVIDIA · HEVC"),
    ("hevc_qsv", "Intel · HEVC"),
    ("hevc_amf", "AMD · HEVC"),
    ("hevc_videotoolbox", "Apple · HEVC"),
    ("hevc_vaapi", "VAAPI · HEVC"),
];

#[derive(Clone, PartialEq, Eq)]
struct SourceKey {
    path: PathBuf,
    size: u64,
    modified: SystemTime,
    created: Option<SystemTime>,
    probe_tool: PathBuf,
    probe_identity: String,
}

struct Source {
    key: SourceKey,
    probe: Value,
    video: Value,
    duration: f64,
    fps: Option<String>,
    variable_fps: bool,
    video_bytes: u64,
    fixed_bytes: u64,
    blocking: Vec<String>,
}

#[derive(Clone)]
struct Plan {
    encoder: String,
    bitrate: u64,
    width: u32,
    height: u32,
    resize: bool,
    fps: Option<String>,
    pixel_format: String,
}

#[derive(Clone)]
struct Capability {
    encoder: String,
    available: bool,
    reason: Option<String>,
    checked_at: Instant,
}

type CapabilityKey = (
    SourceKey,
    PathBuf,
    String,
    u32,
    u32,
    Option<String>,
    u64,
    String,
);
static SOURCES: OnceLock<Mutex<VecDeque<Arc<Source>>>> = OnceLock::new();
static CAPABILITIES: OnceLock<Mutex<VecDeque<(CapabilityKey, Capability)>>> = OnceLock::new();
static ANALYSIS_LOCK: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());
type RuntimeSnapshots = VecDeque<(String, HashMap<String, PathBuf>)>;
static RUNTIME_SNAPSHOTS: tokio::sync::Mutex<RuntimeSnapshots> =
    tokio::sync::Mutex::const_new(VecDeque::new());

async fn current_tools(app: &App, cancel: &CancellationToken) -> Result<HashMap<String, PathBuf>> {
    let mut cache = tokio::select! {guard=RUNTIME_SNAPSHOTS.lock()=>guard,_=cancel.cancelled()=>bail!("Compression analysis cancelled")};
    let _updating = tokio::select! {guard=app.tool_update_lock.lock()=>guard,_=cancel.cancelled()=>bail!("Compression analysis cancelled")};
    let mut paths = Vec::new();
    for name in ["yt-dlp", "ffmpeg", "ffprobe"] {
        paths.push(app.tool(name)?.canonicalize()?);
    }
    if let Ok(path) = app.tool("deno").or_else(|_| app.tool("qjs")) {
        paths.push(path.canonicalize()?);
    }
    if cfg!(windows) {
        let parent = app
            .tool("ffmpeg")?
            .parent()
            .context("FFmpeg runtime directory is missing")?
            .to_path_buf();
        let entries = std::fs::read_dir(parent)?.collect::<std::io::Result<Vec<_>>>()?;
        ensure!(
            entries.len() <= 128,
            "FFmpeg runtime directory contains too many files"
        );
        paths.extend(
            entries
                .into_iter()
                .map(|entry| entry.path())
                .filter(|path| {
                    path.extension()
                        .is_some_and(|extension| extension.eq_ignore_ascii_case("dll"))
                }),
        );
    }
    paths.sort();
    paths.dedup();
    let mut identity = Sha256::new();
    identity.update(
        app.config
            .data_dir
            .canonicalize()?
            .to_string_lossy()
            .as_bytes(),
    );
    for path in paths {
        let metadata = std::fs::metadata(&path)?;
        identity.update(
            format!(
                "{path:?}:{}:{:?}:{:?}",
                metadata.len(),
                metadata.modified()?,
                metadata.created().ok()
            )
            .as_bytes(),
        );
    }
    let identity = hex::encode(identity.finalize());
    cache.retain(|(_, tools)| {
        tools.values().all(|path| {
            path.is_file()
                && path
                    .parent()
                    .is_some_and(|parent| parent.join("manifest.json").is_file())
        })
    });
    if let Some((_, tools)) = cache.iter().find(|(existing, _)| *existing == identity) {
        return Ok(tools.clone());
    }
    let owned = app
        .myself
        .get()
        .and_then(std::sync::Weak::upgrade)
        .context("Application is closed")?;
    let tools = tokio::task::spawn_blocking(move || crate::tools::freeze(&owned)).await??;
    ensure!(!cancel.is_cancelled(), "Compression analysis cancelled");
    cache.push_back((identity, tools.clone()));
    while cache.len() > 16 {
        cache.pop_front();
    }
    Ok(tools)
}

fn tool_identity(app: &App, task: &Task, name: &str) -> Result<(PathBuf, String)> {
    let path = task
        .tools
        .get(name)
        .with_context(|| format!("Task has no frozen {name} runtime"))?;
    crate::reject_links(path)?;
    let path = path.canonicalize()?;
    let directory = path
        .parent()
        .context("Frozen runtime directory is missing")?;
    ensure!(
        directory.parent()
            == Some(
                app.config
                    .data_dir
                    .join("tools/frozen")
                    .canonicalize()?
                    .as_path()
            ),
        "Task runtime is outside its immutable tool cache"
    );
    let mut entries = std::fs::read_dir(directory)?
        .map(|entry| entry.map(|entry| entry.path()))
        .collect::<std::io::Result<Vec<_>>>()?;
    ensure!(
        entries.len() <= 128,
        "Frozen runtime contains too many files"
    );
    entries.sort();
    let mut digest = Sha256::new();
    for entry in entries {
        crate::reject_links(&entry)?;
        let metadata = std::fs::metadata(&entry)?;
        ensure!(
            metadata.is_file(),
            "Frozen runtime contains a non-file entry"
        );
        digest.update(
            format!(
                "{entry:?}:{:?}:{:?}:{:?}",
                metadata.len(),
                metadata.modified()?,
                metadata.created().ok()
            )
            .as_bytes(),
        );
        if entry.file_name().and_then(|name| name.to_str()) == Some("manifest.json") {
            ensure!(
                metadata.len() <= 8192,
                "Frozen runtime manifest is too large"
            );
            digest.update(std::fs::read(entry)?);
        }
    }
    Ok((path, hex::encode(digest.finalize())))
}

pub(crate) struct Prepared {
    pub path: PathBuf,
    pub duration: f64,
    pub media_info: MediaInfo,
}

impl CompressionOptions {
    fn validate(&self, require_rate: bool) -> Result<()> {
        ensure!(
            !(self.target_bytes.is_some() && self.video_bitrate_bps.is_some()),
            "Choose either a target size or a video bitrate"
        );
        ensure!(
            !require_rate || self.target_bytes.is_some() || self.video_bitrate_bps.is_some(),
            "Choose a target size or a video bitrate"
        );
        ensure!(self.target_bytes != Some(0), "Target size must be positive");
        ensure!(
            self.video_bitrate_bps
                .is_none_or(|value| (1_000..=2_000_000_000).contains(&value)),
            "Video bitrate must be between 1000 and 2000000000 bits per second"
        );
        for value in [self.max_width, self.max_height].into_iter().flatten() {
            ensure!(
                (16..=65535).contains(&value),
                "Invalid compression dimensions"
            );
        }
        if let Some(fps) = &self.fps {
            let rate = fraction(fps).context("Invalid compression frame rate")?;
            ensure!(
                rate > 0.0 && rate <= 240.0,
                "Compression frame rate must be 0–240 fps"
            );
        }
        ensure!(
            ENCODERS.iter().any(|(name, _)| *name == self.encoder),
            "Unsupported compression encoder"
        );
        Ok(())
    }
}

fn fraction(value: &str) -> Option<f64> {
    if value.len() > 32 || value.is_empty() {
        return None;
    }
    let (a, b) = value.split_once('/').unwrap_or((value, "1"));
    if !a.bytes().all(|byte| byte.is_ascii_digit()) || !b.bytes().all(|byte| byte.is_ascii_digit())
    {
        return None;
    }
    let result = a.parse::<u64>().ok()? as f64 / b.parse::<u64>().ok()? as f64;
    (result.is_finite() && result > 0.0).then_some(result)
}

fn streams(probe: &Value) -> impl Iterator<Item = &Value> {
    probe["streams"].as_array().into_iter().flatten()
}

fn picture(stream: &Value) -> bool {
    stream["disposition"]["attached_pic"].as_u64().unwrap_or(0) != 0
}

fn main_video(probe: &Value) -> Result<Value> {
    let videos = streams(probe)
        .filter(|stream| text(stream, "codec_type") == "video" && !picture(stream))
        .collect::<Vec<_>>();
    ensure!(
        videos.len() == 1,
        "Compression requires one main video stream; multiple video views are not supported"
    );
    Ok(videos[0].clone())
}

fn duration(probe: &Value) -> Result<f64> {
    text(&probe["format"], "duration")
        .parse::<f64>()
        .ok()
        .filter(|value| value.is_finite() && *value > 0.0)
        .context("The saved video has no valid duration")
}

fn pixel_format(pixel: &str) -> Option<(&'static str, u32, u32, u32)> {
    Some(match pixel {
        "yuv420p" | "yuvj420p" | "nv12" | "nv21" => ("yuv420p", 8, 2, 2),
        "yuv420p10le" | "yuv420p10be" | "p010le" | "p010be" => ("yuv420p10le", 10, 2, 2),
        "yuv420p12le" | "yuv420p12be" | "p012le" | "p012be" => ("yuv420p12le", 12, 2, 2),
        "yuv422p" | "yuvj422p" | "nv16" => ("yuv422p", 8, 2, 1),
        "yuv422p10le" | "yuv422p10be" | "p210le" | "p210be" => ("yuv422p10le", 10, 2, 1),
        "yuv422p12le" | "yuv422p12be" | "p212le" | "p212be" => ("yuv422p12le", 12, 2, 1),
        "yuv444p" | "yuvj444p" | "nv24" | "nv42" => ("yuv444p", 8, 1, 1),
        "yuv444p10le" | "yuv444p10be" | "p410le" | "p410be" => ("yuv444p10le", 10, 1, 1),
        "yuv444p12le" | "yuv444p12be" | "p412le" | "p412be" => ("yuv444p12le", 12, 1, 1),
        _ => return None,
    })
}

fn depth(pixel: &str) -> u32 {
    pixel_format(pixel).map_or(0, |(_, bits, _, _)| bits)
}

fn hdr(video: &Value) -> &'static str {
    match text(video, "color_transfer") {
        "smpte2084" => "hdr10",
        "arib-std-b67" => "hlg",
        _ => "sdr",
    }
}

fn dimensions(video: &Value) -> Result<(u32, u32)> {
    let width = video["width"]
        .as_u64()
        .filter(|value| *value <= 65535)
        .context("Video width is unavailable")? as u32;
    let height = video["height"]
        .as_u64()
        .filter(|value| *value <= 65535)
        .context("Video height is unavailable")? as u32;
    ensure!(width > 0 && height > 0, "Video dimensions are invalid");
    let sar = text(video, "sample_aspect_ratio").replace(':', "/");
    let display_width = (width as f64 * fraction(&sar).unwrap_or(1.0)).round() as u32;
    let rotation = video["side_data_list"]
        .as_array()
        .into_iter()
        .flatten()
        .find_map(|data| data["rotation"].as_f64())
        .or_else(|| text(&video["tags"], "rotate").parse().ok())
        .unwrap_or(0.0);
    ensure!(
        (rotation / 90.0 - (rotation / 90.0).round()).abs() < 0.001,
        "Unsupported non-right-angle video rotation"
    );
    Ok(if (rotation / 90.0).round() as i64 % 2 != 0 {
        (height, display_width)
    } else {
        (display_width, height)
    })
}

fn side_data<'a>(probe: &'a Value, video: &'a Value) -> impl Iterator<Item = &'a Value> {
    video["side_data_list"]
        .as_array()
        .into_iter()
        .flatten()
        .chain(
            probe["frames"]
                .as_array()
                .into_iter()
                .flatten()
                .flat_map(|frame| frame["side_data_list"].as_array().into_iter().flatten()),
        )
}

async fn probe(app: &App, task: &Task, path: &Path, cancel: &CancellationToken) -> Result<Value> {
    let executable = app.task_tool(task, "ffprobe").await?;
    let args = vec![
        "-v".into(),
        "error".into(),
        "-show_format".into(),
        "-show_streams".into(),
        "-show_chapters".into(),
        "-show_data_hash".into(),
        "sha256".into(),
        "-of".into(),
        "json".into(),
        path.to_string_lossy().into(),
    ];
    let output = process::run_limited_logged(
        &executable,
        &args,
        cancel,
        16 * 1024 * 1024,
        Some(Duration::from_secs(30)),
        Some(app.raw_task_log(&task.id)?),
    )
    .await?;
    let mut probe: Value = serde_json::from_str(&output.stdout)?;
    let video = main_video(&probe)?;
    let mut frame_probe = Frames::default();
    let frames = Arc::new(Mutex::new(Frames {
        timestamp_tolerance: fraction(text(&video, "time_base")).unwrap_or(0.000001) * 2.01,
        ..Frames::default()
    }));
    let observed = frames.clone();
    let callback: LineCallback = Arc::new(move |error, line| {
        if error {
            observed.lock().unwrap().decode_error = true;
        } else {
            observed.lock().unwrap().observe(line);
        }
    });
    let args = vec![
        "-v".into(),
        "error".into(),
        "-select_streams".into(),
        video["index"].to_string(),
        "-show_frames".into(),
        "-show_entries".into(),
        "frame=interlaced_frame,width,height,pix_fmt,best_effort_timestamp_time,duration_time,color_transfer,color_primaries,color_space,color_range,chroma_location:frame_side_data=side_data_type,red_x,red_y,green_x,green_y,blue_x,blue_y,white_point_x,white_point_y,max_luminance,min_luminance,max_content,max_average".into(),
        "-of".into(),
        "compact=p=0".into(),
        path.to_string_lossy().into(),
    ];
    process::run_streamed(&executable, &args, cancel, callback).await?;
    std::mem::swap(&mut frame_probe, &mut frames.lock().unwrap());
    ensure!(
        !frame_probe.decode_error,
        "The source contains decoding errors; repair it before compression"
    );
    ensure!(
        frame_probe.count > 0,
        "The video contains no decodable frames"
    );
    let index = probe["streams"]
        .as_array()
        .context("Media stream list is missing")?
        .iter()
        .position(|stream| stream["index"] == video["index"])
        .context("Main video stream is missing")?;
    for (key, value) in &frame_probe.colors {
        let existing = text(&probe["streams"][index], key);
        if matches!(existing, "" | "unknown" | "unspecified") {
            probe["streams"][index][key] = json!(value);
        } else if existing != value {
            frame_probe.unsupported = true;
        }
    }
    probe["frames"] = json!([{"side_data_list":frame_probe.side_data}]);
    probe["compressionFrames"] = json!({"count":frame_probe.count,"dynamicHdr":frame_probe.dynamic_hdr,"unsupported":frame_probe.unsupported,"variable":frame_probe.variable,"firstTimestamp":frame_probe.first_timestamp,"lastTimestamp":frame_probe.last_timestamp,"lastDuration":frame_probe.last_duration});
    Ok(probe)
}

#[derive(Default)]
struct Frames {
    count: u64,
    decode_error: bool,
    dynamic_hdr: bool,
    unsupported: bool,
    variable: bool,
    first_timestamp: Option<f64>,
    last_timestamp: Option<f64>,
    last_delta: Option<f64>,
    last_duration: Option<f64>,
    timestamp_tolerance: f64,
    format: Option<(String, String, String)>,
    side_data: Vec<Value>,
    colors: HashMap<String, String>,
}

impl Frames {
    fn observe(&mut self, line: &str) {
        let lower = line.to_ascii_lowercase();
        self.dynamic_hdr |= ["dovi", "dolby vision", "smpte2094", "smpte 2094", "hdr10+"]
            .iter()
            .any(|word| lower.contains(word));
        let fields = line
            .split('|')
            .filter_map(|field| field.split_once('='))
            .collect::<HashMap<_, _>>();
        if let (Some(width), Some(height), Some(pixel)) = (
            fields.get("width"),
            fields.get("height"),
            fields.get("pix_fmt"),
        ) {
            self.count += 1;
            let format = (width.to_string(), height.to_string(), pixel.to_string());
            self.unsupported |= self
                .format
                .as_ref()
                .is_some_and(|previous| *previous != format)
                || fields
                    .get("interlaced_frame")
                    .is_some_and(|value| *value != "0");
            self.format = Some(format);
            self.last_duration = fields
                .get("duration_time")
                .and_then(|value| value.parse::<f64>().ok())
                .filter(|value| value.is_finite() && *value > 0.0);
            for key in [
                "color_primaries",
                "color_transfer",
                "color_space",
                "color_range",
                "chroma_location",
            ] {
                if let Some(value) = fields
                    .get(key)
                    .filter(|value| !matches!(**value, "" | "unknown" | "unspecified"))
                {
                    if let Some(previous) = self.colors.get(key) {
                        self.unsupported |= previous != value;
                    } else {
                        self.colors.insert(key.into(), value.to_string());
                    }
                }
            }
            if let Some(timestamp) = fields
                .get("best_effort_timestamp_time")
                .and_then(|value| value.parse::<f64>().ok())
                .filter(|value| value.is_finite())
            {
                self.first_timestamp.get_or_insert(timestamp);
                if let Some(previous) = self.last_timestamp {
                    let delta = timestamp - previous;
                    if let Some(last_delta) = self.last_delta {
                        self.variable |=
                            (delta - last_delta).abs() > self.timestamp_tolerance.max(0.000001);
                    }
                    self.last_delta = Some(delta);
                }
                self.last_timestamp = Some(timestamp);
            }
        }
        for section in line.split("side_data_type=").skip(1) {
            let mut parts = section.split('|');
            let kind = parts.next().unwrap_or("");
            if !matches!(
                kind,
                "Mastering display metadata" | "Content light level metadata"
            ) {
                continue;
            }
            let mut data = serde_json::Map::new();
            data.insert("side_data_type".into(), json!(kind));
            for (key, value) in parts.filter_map(|part| part.split_once('=')) {
                let key = key.rsplit(':').next().unwrap_or(key);
                if !matches!(
                    key,
                    "red_x"
                        | "red_y"
                        | "green_x"
                        | "green_y"
                        | "blue_x"
                        | "blue_y"
                        | "white_point_x"
                        | "white_point_y"
                        | "max_luminance"
                        | "min_luminance"
                        | "max_content"
                        | "max_average"
                ) {
                    continue;
                }
                data.insert(
                    key.into(),
                    if matches!(key, "max_content" | "max_average") {
                        value
                            .parse::<u64>()
                            .map_or_else(|_| json!(value), |value| json!(value))
                    } else {
                        json!(value)
                    },
                );
            }
            let data = Value::Object(data);
            if !self.side_data.contains(&data) {
                if self
                    .side_data
                    .iter()
                    .any(|previous| text(previous, "side_data_type") == kind)
                {
                    self.unsupported = true;
                }
                if self.side_data.len() >= 8 {
                    self.unsupported = true;
                } else {
                    self.side_data.push(data);
                }
            }
        }
    }
}

fn blocking_reasons(probe: &Value, video: &Value) -> Vec<String> {
    let mut reasons = Vec::new();
    let descriptors = video["side_data_list"].to_string().to_ascii_lowercase();
    if probe["compressionFrames"]["dynamicHdr"].as_bool() == Some(true)
        || ["dovi", "dolby vision", "smpte2094", "smpte 2094", "hdr10+"]
            .iter()
            .any(|word| descriptors.contains(word))
        || matches!(
            text(video, "codec_tag_string"),
            "dvhe" | "dvh1" | "dva1" | "dvav"
        )
    {
        reasons.push(
            "Dolby Vision or dynamic HDR metadata cannot currently be preserved by compression"
                .into(),
        );
    }
    if streams(probe).any(|stream| {
        !matches!(
            text(stream, "codec_type"),
            "video" | "audio" | "subtitle" | "attachment"
        ) || picture(stream)
    }) {
        reasons.push(
            "This video contains a data stream or attached picture that cannot be preserved in MKV"
                .into(),
        );
    }
    if pixel_format(text(video, "pix_fmt")).is_none() {
        reasons.push("This video pixel format, bit depth or alpha channel cannot be preserved by HEVC compression".into());
    }
    if streams(probe).any(|stream| {
        text(stream, "codec_type") == "subtitle"
            && !matches!(
                text(stream, "codec_name"),
                "subrip"
                    | "ass"
                    | "ssa"
                    | "webvtt"
                    | "hdmv_pgs_subtitle"
                    | "dvd_subtitle"
                    | "dvb_subtitle"
            )
    }) {
        reasons
            .push("A subtitle codec cannot be copied into MKV without changing its format".into());
    }
    if probe["compressionFrames"]["unsupported"].as_bool() == Some(true)
        || !matches!(text(video, "field_order"), "" | "unknown" | "progressive")
    {
        reasons.push(
            "Interlaced video or changing frame properties are not supported by compression".into(),
        );
    }
    reasons
}

async fn source(
    app: &App,
    id: &str,
    cancel: &CancellationToken,
    cached: bool,
) -> Result<(Task, Arc<Source>)> {
    let _analysis = tokio::select! { guard=ANALYSIS_LOCK.lock()=>guard, _=cancel.cancelled()=>bail!("Compression analysis cancelled") };
    let mut task = app.db.task(id)?;
    ensure!(
        matches!(task.status.as_str(), "completed" | "retained") && task.kind == "media",
        "Only completed local videos can be compressed"
    );
    task.tools = current_tools(app, cancel).await?;
    let media = app
        .db
        .media_for_task(id)?
        .context("Completed media does not exist")?;
    let path = app.owned_path(&media.path)?;
    let metadata = std::fs::metadata(&path)?;
    let (probe_tool, probe_identity) = tool_identity(app, &task, "ffprobe")?;
    let key = SourceKey {
        path,
        size: metadata.len(),
        modified: metadata.modified()?,
        created: metadata.created().ok(),
        probe_tool,
        probe_identity,
    };
    if cached
        && let Some(source) = SOURCES
            .get_or_init(Default::default)
            .lock()
            .unwrap()
            .iter()
            .find(|source| source.key == key)
            .cloned()
    {
        return Ok((task, source));
    }
    let probe = probe(app, &task, &key.path, cancel).await?;
    let video = main_video(&probe)?;
    dimensions(&video)?;
    let duration = duration(&probe)?;
    let blocking = blocking_reasons(&probe, &video);
    let main_index = video["index"]
        .as_u64()
        .context("Video stream index is missing")?;
    let stats = Arc::new(Mutex::new((HashMap::<u64, u64>::new(), None::<f64>, false)));
    let observed = stats.clone();
    let duration_tolerance = fraction(text(&video, "time_base")).unwrap_or(0.000001) * 2.01;
    let callback: LineCallback = Arc::new(move |is_error, line| {
        if is_error {
            return;
        }
        let fields = line
            .split('|')
            .filter_map(|field| field.split_once('='))
            .collect::<HashMap<_, _>>();
        if let (Some(index), Some(size)) = (
            fields
                .get("stream_index")
                .and_then(|value| value.parse::<u64>().ok()),
            fields
                .get("size")
                .and_then(|value| value.parse::<u64>().ok()),
        ) {
            let mut stats = observed.lock().unwrap();
            *stats.0.entry(index).or_default() += size;
            if index == main_index
                && let Some(value) = fields
                    .get("duration_time")
                    .and_then(|value| value.parse::<f64>().ok())
                    .filter(|value| *value > 0.0)
            {
                if stats
                    .1
                    .is_some_and(|previous| (previous - value).abs() > duration_tolerance)
                {
                    stats.2 = true;
                }
                stats.1 = Some(value);
            }
        }
    });
    let args = vec![
        "-v".into(),
        "error".into(),
        "-show_packets".into(),
        "-show_entries".into(),
        "packet=stream_index,size,duration_time".into(),
        "-of".into(),
        "compact=p=0".into(),
        key.path.to_string_lossy().into(),
    ];
    process::run_streamed(&key.probe_tool, &args, cancel, callback).await?;
    let stats = stats.lock().unwrap();
    let video_bytes = stats.0.get(&main_index).copied().unwrap_or(0);
    ensure!(video_bytes > 0, "The main video stream contains no packets");
    let fixed_bytes = stats
        .0
        .iter()
        .filter(|(index, _)| **index != main_index)
        .map(|(_, size)| size)
        .sum::<u64>()
        + streams(&probe)
            .filter(|stream| text(stream, "codec_type") == "attachment")
            .filter_map(|stream| stream["extradata_size"].as_u64())
            .sum::<u64>();
    let fps = [text(&video, "avg_frame_rate"), text(&video, "r_frame_rate")]
        .into_iter()
        .find(|value| fraction(value).is_some())
        .map(str::to_string);
    let variable_fps = probe["compressionFrames"]["variable"].as_bool() == Some(true)
        || stats.2
        || fraction(text(&video, "avg_frame_rate"))
            .zip(fraction(text(&video, "r_frame_rate")))
            .is_some_and(|(a, b)| (a - b).abs() > 0.01);
    drop(stats);
    let current = std::fs::metadata(&key.path)?;
    ensure!(
        current.len() == key.size
            && current.modified()? == key.modified
            && current.created().ok() == key.created,
        "The source changed during compression analysis; analyze it again"
    );
    let source = Arc::new(Source {
        key,
        probe,
        video,
        duration,
        fps,
        variable_fps,
        video_bytes,
        fixed_bytes,
        blocking,
    });
    let mut sources = SOURCES.get_or_init(Default::default).lock().unwrap();
    sources.retain(|existing| existing.key.path != source.key.path);
    sources.push_back(source.clone());
    while sources.len() > 16 {
        sources.pop_front();
    }
    Ok((task, source))
}

fn reserve(target: u64) -> u64 {
    (target / 50).max(64 * 1024)
}

fn video_duration(source: &Source) -> Option<f64> {
    if let Some(value) = text(&source.video, "duration")
        .parse::<f64>()
        .ok()
        .filter(|value| value.is_finite() && *value > 0.0)
    {
        return Some(value);
    }
    let frames = &source.probe["compressionFrames"];
    let first = frames["firstTimestamp"].as_f64()?;
    let last = frames["lastTimestamp"].as_f64()?;
    let tail = frames["lastDuration"].as_f64().or_else(|| {
        source
            .fps
            .as_deref()
            .and_then(fraction)
            .map(|fps| 1.0 / fps)
    })?;
    let duration = last - first + tail;
    (duration.is_finite() && duration > 0.0).then_some(duration)
}

fn video_bitrate(source: &Source) -> (u64, bool) {
    if let Some(rate) = text(&source.video, "bit_rate")
        .parse::<u64>()
        .ok()
        .filter(|rate| *rate > 0)
    {
        return (rate, false);
    }
    let duration = video_duration(source);
    (
        (source.video_bytes as f64 * 8.0 / duration.unwrap_or(source.duration)).round() as u64,
        duration.is_none(),
    )
}

fn bitrate(target: u64, fixed: u64, duration: f64) -> Result<u64> {
    let remaining = target
        .checked_sub(fixed)
        .and_then(|value| value.checked_sub(reserve(target)))
        .context(
            "The target is too small to preserve the existing audio, subtitles and attachments",
        )?;
    let bitrate = (remaining as f64 * 8.0 / duration).floor() as u64;
    ensure!(
        bitrate >= 1_000,
        "The target leaves too little space for video; choose a larger size"
    );
    Ok(bitrate)
}

fn plan(source: &Source, options: &CompressionOptions) -> Result<Plan> {
    options.validate(true)?;
    if let Some(target) = options.target_bytes {
        ensure!(
            target <= source.key.size,
            "Target size cannot exceed the actual source file size"
        );
    }
    let (width, height) = dimensions(&source.video)?;
    ensure!(
        options.max_width.is_none_or(|value| value <= width)
            && options.max_height.is_none_or(|value| value <= height),
        "Compression cannot increase the source resolution"
    );
    let factor = options
        .max_width
        .map_or(1.0, |value| value as f64 / width as f64)
        .min(
            options
                .max_height
                .map_or(1.0, |value| value as f64 / height as f64),
        );
    let resize = factor < 1.0;
    let (pixel_format, _, align_x, align_y) = pixel_format(text(&source.video, "pix_fmt"))
        .context("The source pixel format cannot be preserved by compression")?;
    let (width, height) = if resize {
        (
            (width as f64 * factor).floor() as u32 / align_x * align_x,
            (height as f64 * factor).floor() as u32 / align_y * align_y,
        )
    } else {
        (width, height)
    };
    ensure!(
        width >= 16 && height >= 16,
        "The requested resolution is too small"
    );
    if let Some(fps) = &options.fps {
        ensure!(
            fraction(fps).unwrap()
                <= source
                    .fps
                    .as_deref()
                    .and_then(fraction)
                    .context("The source frame rate is unavailable")?
                    + 0.00001,
            "Compression cannot increase the source frame rate"
        );
    }
    Ok(Plan {
        encoder: options.encoder.clone(),
        bitrate: match options.target_bytes {
            Some(target) => bitrate(
                target,
                source.fixed_bytes,
                video_duration(source).unwrap_or(source.duration),
            )?,
            None => options.video_bitrate_bps.unwrap(),
        },
        width,
        height,
        resize,
        fps: options.fps.clone(),
        pixel_format: pixel_format.into(),
    })
}

pub(crate) async fn preview(
    app: &App,
    id: &str,
    options: Option<&CompressionOptions>,
    cancel: &CancellationToken,
) -> Result<Value> {
    if let Some(options) = options {
        options.validate(false)?;
    }
    let (task, source) = source(app, id, cancel, true).await?;
    let planning = options
        .filter(|options| options.target_bytes.is_some() || options.video_bitrate_bps.is_some())
        .map(|options| plan(&source, options))
        .transpose()?;
    let (display_width, display_height) = dimensions(&source.video)?;
    let fallback = Plan {
        encoder: "libx265".into(),
        bitrate: video_bitrate(&source).0,
        width: display_width,
        height: display_height,
        resize: false,
        fps: None,
        pixel_format: pixel_format(text(&source.video, "pix_fmt"))
            .map_or(text(&source.video, "pix_fmt"), |(format, _, _, _)| format)
            .into(),
    };
    let capabilities = capabilities(
        app,
        &task,
        &source,
        planning.as_ref().unwrap_or(&fallback),
        cancel,
    )
    .await?;
    let mut blocking = source.blocking.clone();
    if let Some(plan) = &planning
        && !capabilities
            .iter()
            .any(|capability| capability.encoder == plan.encoder && capability.available)
    {
        blocking.push("The selected encoder cannot preserve this video's format and features; choose an available encoder".into());
    }
    let count = |kind| {
        streams(&source.probe)
            .filter(|stream| text(stream, "codec_type") == kind)
            .count()
    };
    Ok(
        json!({"source":{"sizeBytes":source.key.size,"duration":source.duration,"width":source.video["width"],"height":source.video["height"],"displayWidth":display_width,"displayHeight":display_height,"fps":source.fps,"variableFrameRate":source.variable_fps,"videoBitrateBps":video_bitrate(&source).0,"videoBitrateEstimated":video_bitrate(&source).1,"hdr":hdr(&source.video),"bitDepth":depth(text(&source.video,"pix_fmt")),"audioTracks":count("audio"),"subtitleTracks":count("subtitle"),"attachmentTracks":count("attachment")},"plan":planning.as_ref().map(|plan|json!({"encoder":plan.encoder,"videoBitrateBps":plan.bitrate,"estimatedSizeBytes":(plan.bitrate as f64*video_duration(&source).unwrap_or(source.duration)/8.0).ceil() as u64+source.fixed_bytes+reserve(options.and_then(|options|options.target_bytes).unwrap_or(source.key.size)),"width":plan.width,"height":plan.height,"fps":plan.fps.as_ref().or(source.fps.as_ref()),"targetBytes":options.and_then(|options|options.target_bytes)})),"capabilities":capabilities.iter().map(|capability|json!({"encoder":capability.encoder,"label":ENCODERS.iter().find(|(name,_)|*name==capability.encoder).map(|(_,label)|*label).unwrap_or("HEVC"),"available":capability.available,"reason":capability.reason})).collect::<Vec<_>>(),"blockingReasons":blocking,"warnings":["Video compression is lossy. Existing audio, subtitle tracks, attachments and chapters are copied into MKV."]}),
    )
}

pub(crate) async fn queue(
    app: &App,
    id: &str,
    options: CompressionOptions,
    cancel: &CancellationToken,
) -> Result<Value> {
    options.validate(true)?;
    let preview = preview(app, id, Some(&options), cancel).await?;
    ensure!(
        preview["blockingReasons"]
            .as_array()
            .is_some_and(Vec::is_empty),
        "{}",
        preview["blockingReasons"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .collect::<Vec<_>>()
            .join("; ")
    );
    ensure!(!cancel.is_cancelled(), "Compression cancelled");
    crate::processing::queue_compression(app, id, &options, cancel).await
}

pub(crate) async fn retry_cpu(app: &App, id: &str) -> Result<Value> {
    let _adding = app.task_add_lock.lock().await;
    let mut task = app.db.task(id)?;
    ensure!(
        matches!(
            task.status.as_str(),
            "failed" | "processing_failed" | "paused" | "cancelled"
        ),
        "Only stopped compression tasks can be retried on CPU"
    );
    ensure!(
        task.publication.is_none(),
        "The encoded output is already prepared; resume publication before changing its encoder"
    );
    let options = task
        .compression
        .as_mut()
        .context("This is not a compression task")?;
    ensure!(
        options.encoder != "libx265",
        "This task already uses CPU compression"
    );
    options.encoder = "libx265".into();
    task.options.encoder = "libx265".into();
    task.error = None;
    app.save_task(&task)?;
    drop(_adding);
    app.resume(id, true).await
}

fn threads() -> usize {
    std::thread::available_parallelism().map_or(1, |value| {
        let cores = value.get();
        if cores >= 4 { cores - 1 } else { cores }
    })
}

fn filters(plan: &Plan) -> Option<String> {
    let mut filters = Vec::new();
    if plan.resize {
        filters.push(format!(
            "scale={}:{}:flags=lanczos,setsar=1",
            plan.width, plan.height
        ));
    }
    if let Some(fps) = &plan.fps {
        filters.push(format!("fps={fps}"));
    }
    (!filters.is_empty()).then(|| filters.join(","))
}

fn encoder_args(encoder: &str, plan: &Plan) -> Vec<String> {
    let pixel = if encoder == "libx265" {
        plan.pixel_format.as_str()
    } else if depth(&plan.pixel_format) > 8 {
        "p010le"
    } else {
        "nv12"
    };
    let mut filter = Vec::new();
    if !plan.resize {
        filter.push("scale=iw:ih:flags=lanczos".into());
    }
    if let Some(filters) = filters(plan) {
        filter.push(filters);
    }
    filter.push(format!("format={pixel}"));
    if encoder == "hevc_vaapi" {
        filter.push("hwupload".into());
    }
    let mut args = vec![
        "-pix_fmt:v:0".into(),
        format!(
            "+{}",
            if encoder == "hevc_vaapi" {
                "vaapi"
            } else {
                pixel
            }
        ),
        "-vf:v:0".into(),
        filter.join(","),
    ];
    if encoder == "hevc_videotoolbox" {
        args.extend(["-allow_sw".into(), "0".into()]);
    }
    args
}

fn input_args(encoder: &str, path: &Path) -> Vec<String> {
    let mut args = vec![
        "-hide_banner".into(),
        "-loglevel".into(),
        "error".into(),
        "-nostdin".into(),
        "-y".into(),
        "-threads".into(),
        threads().to_string(),
        "-filter_threads".into(),
        threads().to_string(),
        "-copyts".into(),
    ];
    if encoder == "hevc_vaapi" {
        args.extend(["-vaapi_device".into(), "/dev/dri/renderD128".into()]);
    }
    args.extend(["-i".into(), path.to_string_lossy().into()]);
    args
}

fn color_args(source: &Source) -> Vec<String> {
    let mut args = Vec::new();
    for (key, flag) in [
        ("color_primaries", "-color_primaries"),
        ("color_transfer", "-color_trc"),
        ("color_space", "-colorspace"),
        ("color_range", "-color_range"),
        ("chroma_location", "-chroma_sample_location"),
    ] {
        let value = text(&source.video, key);
        if !matches!(value, "" | "unknown" | "unspecified") {
            args.extend([flag.into(), value.into()]);
        }
    }
    args
}

fn hdr_params(source: &Source) -> Vec<String> {
    let mut params = Vec::new();
    for data in side_data(&source.probe, &source.video) {
        let number = |key: &str, scale: f64| {
            data[key]
                .as_str()
                .and_then(|value| {
                    let (a, b) = value.split_once('/').unwrap_or((value, "1"));
                    Some(a.parse::<f64>().ok()? / b.parse::<f64>().ok()?)
                })
                .map(|value| (value * scale).round() as u64)
        };
        if text(data, "side_data_type") == "Mastering display metadata" {
            let values = [
                "green_x",
                "green_y",
                "blue_x",
                "blue_y",
                "red_x",
                "red_y",
                "white_point_x",
                "white_point_y",
            ]
            .map(|key| number(key, 50000.0));
            if let (
                [
                    Some(gx),
                    Some(gy),
                    Some(bx),
                    Some(by),
                    Some(rx),
                    Some(ry),
                    Some(wx),
                    Some(wy),
                ],
                Some(max),
                Some(min),
            ) = (
                values,
                number("max_luminance", 10000.0),
                number("min_luminance", 10000.0),
            ) {
                params.push(format!(
                    "master-display=G({gx},{gy})B({bx},{by})R({rx},{ry})WP({wx},{wy})L({max},{min})"
                ));
            }
        }
        if text(data, "side_data_type") == "Content light level metadata"
            && let (Some(max), Some(avg)) =
                (data["max_content"].as_u64(), data["max_average"].as_u64())
        {
            params.push(format!("max-cll={max},{avg}"));
        }
    }
    params.sort();
    params.dedup();
    params
}

fn x265_colors(source: &Source) -> Vec<String> {
    [
        ("color_primaries", "colorprim"),
        ("color_transfer", "transfer"),
        ("color_space", "colormatrix"),
    ]
    .into_iter()
    .filter_map(|(key, parameter)| {
        let value = text(&source.video, key);
        (!matches!(value, "" | "unknown" | "unspecified")).then(|| format!("{parameter}={value}"))
    })
    .collect()
}

async fn capabilities(
    app: &App,
    task: &Task,
    source: &Source,
    plan: &Plan,
    cancel: &CancellationToken,
) -> Result<Vec<Capability>> {
    let (executable, identity) = tool_identity(app, task, "ffmpeg")?;
    let mut verified = false;
    let mut result = Vec::new();
    for (encoder, _) in ENCODERS {
        let key = (
            source.key.clone(),
            executable.clone(),
            identity.clone(),
            plan.width,
            plan.height,
            plan.fps.clone(),
            plan.bitrate,
            encoder.to_string(),
        );
        if let Some((_, capability)) = CAPABILITIES
            .get_or_init(Default::default)
            .lock()
            .unwrap()
            .iter()
            .find(|(existing, capability)| {
                *existing == key && capability.checked_at.elapsed() < Duration::from_secs(30)
            })
            .cloned()
        {
            result.push(capability);
            continue;
        }
        let platform = match encoder {
            "hevc_videotoolbox" => cfg!(target_os = "macos"),
            "hevc_amf" | "hevc_qsv" => cfg!(windows) || cfg!(target_os = "linux"),
            "hevc_nvenc" => !cfg!(target_os = "macos"),
            "hevc_vaapi" => cfg!(target_os = "linux"),
            _ => true,
        };
        let reason = if !source.blocking.is_empty() {
            Some(source.blocking.join("; "))
        } else if !platform {
            Some("This encoder is not available on this platform".into())
        } else if encoder != "libx265"
            && (!hdr_params(source).is_empty()
                || !matches!(
                    plan.pixel_format.as_str(),
                    "yuv420p" | "yuvj420p" | "yuv420p10le" | "p010le"
                ))
        {
            Some("This GPU path cannot preserve the source pixel format or HDR mastering metadata; use CPU".into())
        } else {
            if !verified {
                app.task_tool(task, "ffmpeg").await?;
                verified = true;
            }
            let workspace = tempfile::Builder::new()
                .prefix(".compression-probe-")
                .tempdir_in(&app.config.data_dir)?;
            let output = workspace.path().join("probe.mkv");
            let mut args = input_args(encoder, &source.key.path);
            args.extend([
                "-map".into(),
                format!("0:{}", source.video["index"]),
                "-frames:v".into(),
                "1".into(),
                "-an".into(),
                "-sn".into(),
                "-c:v".into(),
                encoder.into(),
                "-b:v".into(),
                plan.bitrate.to_string(),
            ]);
            args.extend(encoder_args(encoder, plan));
            if encoder == "libx265" {
                let mut params = vec![format!(
                    "pools={}:frame-threads=1:log-level=error",
                    threads()
                )];
                params.extend(hdr_params(source));
                params.extend(x265_colors(source));
                args.extend(["-x265-params".into(), params.join(":")]);
            }
            args.extend(color_args(source));
            args.push(output.to_string_lossy().into());
            let probe_cancel = cancel.child_token();
            let process = process::run(&executable, &args, None, &probe_cancel, None);
            tokio::pin!(process);
            match tokio::select! {result=&mut process=>result,_=tokio::time::sleep(Duration::from_secs(5))=>{probe_cancel.cancel();let _=process.await;Err(anyhow::anyhow!("Encoder capability probe timed out"))}}
            {
                Ok(_) => match probe(app, task, &output, cancel).await.and_then(|probe| {
                    let video = main_video(&probe)?;
                    ensure!(
                        text(&video, "codec_name") == "hevc"
                            && pixel_format(text(&video, "pix_fmt"))
                                == pixel_format(&plan.pixel_format),
                        "Encoder changed source pixel format"
                    );
                    Ok(())
                }) {
                    Ok(()) => None,
                    Err(_) => Some("The encoder cannot preserve the source pixel format".into()),
                },
                Err(error) => Some(capability_failure(&error)),
            }
        };
        ensure!(!cancel.is_cancelled(), "Compression analysis cancelled");
        let capability = Capability {
            encoder: encoder.into(),
            available: reason.is_none(),
            reason,
            checked_at: Instant::now(),
        };
        let mut cache = CAPABILITIES.get_or_init(Default::default).lock().unwrap();
        cache.push_back((key, capability.clone()));
        while cache.len() > 64 {
            cache.pop_front();
        }
        result.push(capability);
    }
    Ok(result)
}

fn capability_failure(error: &anyhow::Error) -> String {
    let diagnostic = format!("{error:#}").to_ascii_lowercase();
    if diagnostic.contains("required nvenc api version")
        || diagnostic.contains("minimum required nvidia driver")
    {
        "The installed NVIDIA driver does not support this FFmpeg NVENC API; update the driver or use CPU".into()
    } else if diagnostic.contains("timed out") {
        "Encoder capability probe timed out; try again or use CPU".into()
    } else {
        "The encoder could not initialize for this source format and dimensions".into()
    }
}

async fn run_encode(
    app: &Arc<App>,
    task: &Task,
    args: &[String],
    duration: f64,
    cancel: &CancellationToken,
) -> Result<()> {
    let progress_app = app.clone();
    let id = task.id.clone();
    let last = Arc::new(Mutex::new(
        std::time::Instant::now() - Duration::from_secs(1),
    ));
    let callback: LineCallback = Arc::new(move |error, line| {
        if error {
            return;
        }
        if let Some(time) = line
            .strip_prefix("out_time_us=")
            .and_then(|value| value.parse::<f64>().ok())
        {
            let mut last = last.lock().unwrap();
            if last.elapsed() < Duration::from_millis(200) {
                return;
            }
            *last = std::time::Instant::now();
            if let Ok(mut current) = progress_app.db.task(&id)
                && current.status == "processing"
            {
                current.stage_progress =
                    Some((time / 1_000_000.0 / duration * 100.0).clamp(0.0, 100.0));
                current.progress = current.stage_progress.unwrap().min(99.0);
                current.stage_indeterminate = false;
                let _ = progress_app.save_task(&current);
            }
        }
    });
    process::run_logged(
        &app.task_tool(task, "ffmpeg").await?,
        args,
        Some(&crate::processing::workspace(app, task)),
        cancel,
        Some(callback),
        app.raw_task_log(&task.id)?,
    )
    .await?;
    Ok(())
}

pub(crate) async fn encode(
    app: &Arc<App>,
    task: &Task,
    cancel: &CancellationToken,
) -> Result<Prepared> {
    let options = task
        .compression
        .as_ref()
        .context("Compression options are missing")?;
    options.validate(true)?;
    let manifest = task
        .manifest
        .as_ref()
        .context("Compression source is missing")?;
    let input = manifest
        .files
        .first()
        .context("Compression source is missing")?;
    let path = app.owned_path(&input.path)?;
    let source_probe = probe(app, task, &path, cancel).await?;
    let video = main_video(&source_probe)?;
    let fps = [text(&video, "avg_frame_rate"), text(&video, "r_frame_rate")]
        .into_iter()
        .find(|value| fraction(value).is_some())
        .map(str::to_string);
    let source = Source {
        key: SourceKey {
            path: path.clone(),
            size: input.size,
            modified: std::fs::metadata(&path)?.modified()?,
            created: std::fs::metadata(&path)?.created().ok(),
            probe_tool: app.task_tool(task, "ffprobe").await?,
            probe_identity: tool_identity(app, task, "ffprobe")?.1,
        },
        duration: duration(&source_probe)?,
        fps,
        variable_fps: false,
        video_bytes: 0,
        fixed_bytes: 0,
        blocking: blocking_reasons(&source_probe, &video),
        video,
        probe: source_probe,
    };
    ensure!(source.blocking.is_empty(), "{}", source.blocking.join("; "));
    let mut plan = plan(&source, options)?;
    let workspace = crate::processing::workspace(app, task);
    crate::reject_links(&workspace)?;
    std::fs::create_dir_all(&workspace)?;
    let fixed = workspace.join("compression-fixed.mkv");
    let video_index = source.video["index"].as_u64().unwrap();
    let fixed_streams = streams(&source.probe)
        .filter(|stream| stream["index"].as_u64() != Some(video_index))
        .collect::<Vec<_>>();
    let mut base = input_args(&plan.encoder, &path);
    let fixed_bytes = if fixed_streams.is_empty() {
        0
    } else {
        let mut args = base.clone();
        for stream in &fixed_streams {
            args.extend(["-map".into(), format!("0:{}", stream["index"])]);
        }
        args.extend([
            "-c".into(),
            "copy".into(),
            "-map_metadata".into(),
            "0".into(),
            "-map_chapters".into(),
            "0".into(),
            fixed.to_string_lossy().into(),
        ]);
        app.set_stage(&task.id, "processing", "compression_preparing", None)?;
        run_encode(app, task, &args, source.duration, cancel).await?;
        std::fs::metadata(&fixed)?.len()
    };
    if let Some(target) = options.target_bytes {
        plan.bitrate = bitrate(
            target,
            fixed_bytes,
            video_duration(&source).unwrap_or(source.duration),
        )?;
    }
    let output = workspace.join("compressed.mkv");
    base.extend(["-map".into(), format!("0:{video_index}")]);
    for correction in 0..3 {
        let passes = if plan.encoder == "libx265" && options.target_bytes.is_some() {
            2
        } else {
            1
        };
        for pass in 1..=passes {
            let analysis = passes == 2 && pass == 1;
            let mut args = base.clone();
            if !analysis {
                for stream in &fixed_streams {
                    args.extend(["-map".into(), format!("0:{}", stream["index"])]);
                }
                args.extend([
                    "-c".into(),
                    "copy".into(),
                    "-map_metadata".into(),
                    "0".into(),
                    "-map_chapters".into(),
                    "0".into(),
                ]);
                for (output_index, stream) in std::iter::once(&source.video)
                    .chain(fixed_streams.iter().copied())
                    .enumerate()
                {
                    let flags = stream["disposition"]
                        .as_object()
                        .into_iter()
                        .flatten()
                        .filter(|(_, value)| value.as_u64() == Some(1))
                        .map(|(key, _)| key.as_str())
                        .collect::<Vec<_>>();
                    args.extend([
                        format!("-disposition:{output_index}"),
                        if flags.is_empty() {
                            "0".into()
                        } else {
                            flags.join("+")
                        },
                        format!("-map_metadata:s:{output_index}"),
                        format!("0:s:{}", stream["index"]),
                    ]);
                }
            }
            args.extend([
                "-c:v:0".into(),
                plan.encoder.clone(),
                "-b:v:0".into(),
                plan.bitrate.to_string(),
                "-threads".into(),
                threads().to_string(),
            ]);
            args.extend(encoder_args(&plan.encoder, &plan));
            args.extend(["-fps_mode:v:0".into(), "passthrough".into()]);
            args.extend(color_args(&source));
            if plan.encoder == "libx265" {
                let mut params = vec![format!("pools={}", threads()), "frame-threads=1".into()];
                if passes == 2 {
                    params.push(format!("pass={pass}"));
                    params.push("stats=compression-stats".into());
                }
                params.extend(hdr_params(&source));
                params.extend(x265_colors(&source));
                args.extend([
                    "-preset".into(),
                    "medium".into(),
                    "-x265-params".into(),
                    params.join(":"),
                ]);
            }
            args.extend(["-progress".into(), "pipe:1".into(), "-nostats".into()]);
            if analysis {
                args.extend([
                    "-an".into(),
                    "-sn".into(),
                    "-f".into(),
                    "null".into(),
                    "-".into(),
                ]);
            } else {
                args.push(output.to_string_lossy().into());
            }
            app.set_stage(
                &task.id,
                "processing",
                if analysis {
                    "compression_analyzing"
                } else {
                    "compression_encoding"
                },
                None,
            )?;
            let result = run_encode(app, task, &args, source.duration, cancel).await;
            if plan.encoder != "libx265" {
                result.context("GPU compression failed; retry this task on CPU")?;
            } else {
                result?;
            }
        }
        let size = std::fs::metadata(&output)?.len();
        if options.target_bytes.is_none_or(|target| size <= target) {
            break;
        }
        ensure!(
            correction < 2,
            "Compressed output still exceeds the target size; choose a larger target"
        );
        let target = options.target_bytes.unwrap();
        let allowed = target
            .checked_sub(fixed_bytes + reserve(target))
            .context("The target is too small to preserve fixed streams")?;
        let actual = size.saturating_sub(fixed_bytes).max(1);
        plan.bitrate = ((plan.bitrate as f64 * allowed as f64 / actual as f64 * 0.97).floor()
            as u64)
            .max(1000);
    }
    app.set_stage(&task.id, "processing", "verifying_output", None)?;
    let output_probe = probe(app, task, &output, cancel).await?;
    let output_duration = duration(&output_probe)?;
    verify_preservation(&source, &plan, &output_probe)?;
    verify_packet_copies(
        app,
        task,
        &source.probe,
        &path,
        &output_probe,
        &output,
        cancel,
    )
    .await?;
    let (hash, size) = hash_path(&path, cancel).await?;
    ensure!(
        size == input.size && hash == input.hash,
        "Source changed during compression; the original file was preserved"
    );
    let size = std::fs::metadata(&output)?.len();
    ensure!(
        size > 0 && options.target_bytes.is_none_or(|target| size <= target),
        "Compressed output exceeds the target size"
    );
    Ok(Prepared {
        path: output,
        duration: output_duration,
        media_info: MediaInfo::from_probe(&output_probe, Some(size), Some("mkv")),
    })
}

pub(crate) async fn hash_path(path: &Path, cancel: &CancellationToken) -> Result<(String, u64)> {
    let owned = path.to_path_buf();
    let cancellation = cancel.clone();
    tokio::task::spawn_blocking(move || {
        let mut file = std::fs::File::open(owned)?;
        let mut hash = Sha256::new();
        let mut size = 0;
        let mut buffer = vec![0; 1024 * 1024];
        loop {
            ensure!(!cancellation.is_cancelled(), "Compression cancelled");
            let count = file.read(&mut buffer)?;
            if count == 0 {
                break;
            }
            hash.update(&buffer[..count]);
            size += count as u64;
        }
        Ok((hex::encode(hash.finalize()), size))
    })
    .await?
}

fn preserved_tags(expected: &Value, actual: &Value) -> bool {
    let tags = actual
        .as_object()
        .into_iter()
        .flatten()
        .map(|(key, value)| (key.to_ascii_lowercase(), value))
        .collect::<HashMap<_, _>>();
    expected
        .as_object()
        .into_iter()
        .flatten()
        .all(|(key, value)| {
            let key = key.to_ascii_lowercase();
            key == "language" && value.as_str() == Some("und") && !tags.contains_key(&key)
                || matches!(
                    key.as_str(),
                    "encoder"
                        | "duration"
                        | "bps"
                        | "number_of_bytes"
                        | "number_of_frames"
                        | "_statistics_writing_app"
                        | "_statistics_writing_date_utc"
                        | "_statistics_tags"
                )
                || tags.get(&key).is_some_and(|actual| *actual == value)
        })
}

fn verify_preservation(source: &Source, plan: &Plan, output: &Value) -> Result<()> {
    let encoded = main_video(output)?;
    ensure!(
        text(&encoded, "codec_name") == "hevc",
        "Compression did not produce HEVC video"
    );
    ensure!(
        pixel_format(text(&encoded, "pix_fmt")) == pixel_format(text(&source.video, "pix_fmt")),
        "Compression changed video bit depth or chroma sampling"
    );
    ensure!(
        dimensions(&encoded)? == (plan.width, plan.height),
        "Compression changed the requested display dimensions or aspect ratio"
    );
    for key in [
        "color_primaries",
        "color_transfer",
        "color_space",
        "color_range",
        "chroma_location",
    ] {
        let expected = text(&source.video, key);
        if !matches!(expected, "" | "unknown" | "unspecified") {
            ensure!(
                text(&encoded, key) == expected,
                "Compression changed the source {key}"
            );
        }
    }
    let output_source = Source {
        key: source.key.clone(),
        probe: output.clone(),
        video: encoded.clone(),
        duration: duration(output)?,
        fps: None,
        variable_fps: false,
        video_bytes: 0,
        fixed_bytes: 0,
        blocking: vec![],
    };
    ensure!(
        hdr_params(source) == hdr_params(&output_source),
        "Compression changed HDR mastering metadata"
    );
    ensure!(
        blocking_reasons(output, &encoded).is_empty(),
        "Compression produced unsupported frame properties"
    );
    let expected_rate = plan
        .fps
        .as_deref()
        .or(source.fps.as_deref())
        .and_then(fraction);
    let tolerance = expected_rate.map_or(0.2, |fps| 2.0 / fps + 0.1).max(0.1);
    ensure!(
        (duration(output)? - source.duration).abs() <= tolerance,
        "Compressed duration differs from the source"
    );
    let original_frames = &source.probe["compressionFrames"];
    let encoded_frames = &output["compressionFrames"];
    if plan.fps.is_none() {
        ensure!(
            original_frames["count"] == encoded_frames["count"],
            "Compression lost or added video frames"
        );
        for key in ["firstTimestamp", "lastTimestamp"] {
            if let (Some(expected), Some(actual)) =
                (original_frames[key].as_f64(), encoded_frames[key].as_f64())
            {
                ensure!(
                    (expected - actual).abs() <= tolerance,
                    "Compression changed the source frame timeline"
                );
            }
        }
    } else if let Some(rate) = expected_rate {
        let actual = fraction(text(&encoded, "avg_frame_rate"))
            .context("Compressed frame rate is unavailable")?;
        ensure!(
            (rate - actual).abs() < 0.001,
            "Compression changed the requested frame rate"
        );
        if let (Some(first), Some(last), Some(count)) = (
            original_frames["firstTimestamp"].as_f64(),
            original_frames["lastTimestamp"].as_f64(),
            encoded_frames["count"].as_u64(),
        ) {
            ensure!(
                (count as f64 - (last - first) * rate).abs() <= 3.0,
                "Compression truncated the video frame timeline"
            );
        }
    }
    let expected = streams(&source.probe)
        .filter(|stream| text(stream, "codec_type") != "video")
        .collect::<Vec<_>>();
    let actual = streams(output)
        .filter(|stream| text(stream, "codec_type") != "video")
        .collect::<Vec<_>>();
    ensure!(
        expected.len() == actual.len(),
        "Compression lost a source stream"
    );
    for (expected, actual) in expected.into_iter().zip(actual) {
        for key in [
            "codec_type",
            "codec_name",
            "sample_rate",
            "channels",
            "channel_layout",
            "extradata_hash",
        ] {
            if !expected[key].is_null() {
                ensure!(
                    expected[key] == actual[key],
                    "Compression changed the source stream {key}"
                );
            }
        }
        ensure!(
            preserved_tags(&expected["tags"], &actual["tags"]),
            "Compression changed source stream tags"
        );
        ensure!(
            expected["disposition"] == actual["disposition"],
            "Compression changed source stream dispositions"
        );
    }
    ensure!(
        preserved_tags(&source.video["tags"], &encoded["tags"]),
        "Compression changed video stream tags"
    );
    ensure!(
        source.video["disposition"] == encoded["disposition"],
        "Compression changed video stream dispositions"
    );
    ensure!(
        preserved_tags(&source.probe["format"]["tags"], &output["format"]["tags"]),
        "Compression changed source metadata"
    );
    let chapters = source.probe["chapters"]
        .as_array()
        .into_iter()
        .flatten()
        .collect::<Vec<_>>();
    let output_chapters = output["chapters"]
        .as_array()
        .into_iter()
        .flatten()
        .collect::<Vec<_>>();
    ensure!(
        chapters.len() == output_chapters.len(),
        "Compression lost a chapter"
    );
    for (index, (expected, actual)) in chapters.into_iter().zip(output_chapters).enumerate() {
        ensure!(
            preserved_tags(&expected["tags"], &actual["tags"]),
            "Compression changed chapter titles or tags"
        );
        for key in ["start_time", "end_time"] {
            let expected = text(expected, key).parse::<f64>()?;
            let actual = text(actual, key).parse::<f64>()?;
            ensure!(
                (expected - actual).abs() <= 0.002,
                "Compression changed chapter {} {key}: expected {expected:.6}s, actual {actual:.6}s (difference {:+.6}s)",
                index + 1,
                actual - expected
            );
        }
    }
    Ok(())
}

async fn packet_hashes(
    app: &App,
    task: &Task,
    path: &Path,
    cancel: &CancellationToken,
) -> Result<HashMap<u64, String>> {
    let hashes = Arc::new(Mutex::new(HashMap::<u64, Sha256>::new()));
    let captured = hashes.clone();
    let callback: LineCallback = Arc::new(move |error, line| {
        if error {
            return;
        }
        let fields = line
            .split('|')
            .filter_map(|field| field.split_once('='))
            .collect::<HashMap<_, _>>();
        if let (Some(index), Some(hash)) = (
            fields
                .get("stream_index")
                .and_then(|value| value.parse::<u64>().ok()),
            fields.get("data_hash"),
        ) {
            let mut hashes = captured.lock().unwrap();
            let hash_state = hashes.entry(index).or_default();
            hash_state.update(hash.as_bytes());
            hash_state.update(b"\n");
        }
    });
    let args = vec![
        "-v".into(),
        "error".into(),
        "-show_packets".into(),
        "-show_data_hash".into(),
        "sha256".into(),
        "-show_entries".into(),
        "packet=stream_index,data_hash".into(),
        "-of".into(),
        "compact=p=0".into(),
        path.to_string_lossy().into(),
    ];
    process::run_streamed(
        &app.task_tool(task, "ffprobe").await?,
        &args,
        cancel,
        callback,
    )
    .await?;
    Ok(hashes
        .lock()
        .unwrap()
        .iter()
        .map(|(index, hash)| (*index, hex::encode(hash.clone().finalize())))
        .collect())
}

async fn verify_packet_copies(
    app: &App,
    task: &Task,
    source_probe: &Value,
    source: &Path,
    output_probe: &Value,
    output: &Path,
    cancel: &CancellationToken,
) -> Result<()> {
    let before = packet_hashes(app, task, source, cancel).await?;
    let after = packet_hashes(app, task, output, cancel).await?;
    for (expected, actual) in streams(source_probe)
        .filter(|stream| matches!(text(stream, "codec_type"), "audio" | "subtitle"))
        .zip(
            streams(output_probe)
                .filter(|stream| matches!(text(stream, "codec_type"), "audio" | "subtitle")),
        )
    {
        let expected = expected["index"]
            .as_u64()
            .context("Source stream index is missing")?;
        let actual = actual["index"]
            .as_u64()
            .context("Output stream index is missing")?;
        ensure!(
            before.get(&expected) == after.get(&actual),
            "Compression changed copied audio or subtitle content"
        );
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture() -> Source {
        let probe = json!({"format":{"duration":"10.000000","tags":{"title":"Original"}},"streams":[
            {"index":0,"codec_type":"video","codec_name":"h264","width":640,"height":360,"sample_aspect_ratio":"1:1","pix_fmt":"yuv420p10le","avg_frame_rate":"30000/1001","r_frame_rate":"30000/1001","field_order":"progressive","color_transfer":"smpte2084","color_primaries":"bt2020","color_space":"bt2020nc","color_range":"tv","chroma_location":"left","disposition":{"default":1,"forced":0},"tags":{"language":"eng","title":"Main"}},
            {"index":1,"codec_type":"audio","codec_name":"aac","sample_rate":"48000","channels":2,"channel_layout":"stereo","extradata_hash":"SHA256:audio","disposition":{"default":1,"forced":0},"tags":{"language":"eng","title":"Original audio"}},
            {"index":2,"codec_type":"subtitle","codec_name":"subrip","disposition":{"default":0,"forced":1},"tags":{"language":"zho","title":"Chinese"}},
            {"index":3,"codec_type":"attachment","codec_name":"ttf","extradata_hash":"SHA256:font","disposition":{"default":0,"forced":0},"tags":{"filename":"subtitle.ttf","mimetype":"application/x-truetype-font"}}
        ],"chapters":[{"start_time":"0.000000","end_time":"10.000000","tags":{"title":"One"}}],"compressionFrames":{"count":300,"firstTimestamp":0.0,"lastTimestamp":9.966666,"dynamicHdr":false,"unsupported":false}});
        Source {
            key: SourceKey {
                path: PathBuf::from("original.mkv"),
                size: 10_000_000,
                modified: SystemTime::UNIX_EPOCH,
                created: None,
                probe_tool: PathBuf::from("ffprobe"),
                probe_identity: String::new(),
            },
            video: probe["streams"][0].clone(),
            probe,
            duration: 10.0,
            fps: Some("30000/1001".into()),
            variable_fps: false,
            video_bytes: 9_000_000,
            fixed_bytes: 800_000,
            blocking: vec![],
        }
    }

    fn encoded(source: &Source) -> Value {
        let mut output = source.probe.clone();
        output["streams"][0]["codec_name"] = json!("hevc");
        output
    }

    fn direct_options() -> CompressionOptions {
        CompressionOptions {
            video_bitrate_bps: Some(1_000_000),
            ..Default::default()
        }
    }

    #[test]
    fn rate_modes_and_rational_frame_rates_are_validated() {
        assert!(CompressionOptions::default().validate(true).is_err());
        assert!(
            CompressionOptions {
                target_bytes: Some(1_000_000),
                ..direct_options()
            }
            .validate(true)
            .is_err()
        );
        for fps in ["0", "0/1", "1/0", "NaN", "30/1:evil", "241", "-1"] {
            assert!(
                CompressionOptions {
                    fps: Some(fps.into()),
                    ..direct_options()
                }
                .validate(true)
                .is_err(),
                "{fps}"
            );
        }
        assert!(
            CompressionOptions {
                fps: Some("24000/1001".into()),
                ..direct_options()
            }
            .validate(true)
            .is_ok()
        );
    }

    #[test]
    fn target_budget_preserves_fixed_streams_and_source_limits() -> Result<()> {
        let source = fixture();
        let options = CompressionOptions {
            target_bytes: Some(5_000_000),
            ..Default::default()
        };
        let budget = plan(&source, &options)?;
        assert!(
            budget.bitrate as f64 * source.duration / 8.0 + source.fixed_bytes as f64
                <= 5_000_000.0 - reserve(5_000_000) as f64
        );
        assert!(
            plan(
                &source,
                &CompressionOptions {
                    target_bytes: Some(source.fixed_bytes),
                    ..Default::default()
                }
            )
            .is_err()
        );
        assert!(
            plan(
                &source,
                &CompressionOptions {
                    target_bytes: Some(source.key.size + 1),
                    ..Default::default()
                }
            )
            .is_err()
        );
        Ok(())
    }

    #[test]
    fn resolution_and_frame_rate_can_only_decrease() -> Result<()> {
        let source = fixture();
        let plan = plan(
            &source,
            &CompressionOptions {
                max_width: Some(321),
                fps: Some("24000/1001".into()),
                ..direct_options()
            },
        )?;
        assert_eq!((plan.width, plan.height), (320, 180));
        assert!(
            super::plan(
                &source,
                &CompressionOptions {
                    max_width: Some(1280),
                    ..direct_options()
                }
            )
            .is_err()
        );
        assert!(
            super::plan(
                &source,
                &CompressionOptions {
                    fps: Some("30".into()),
                    ..direct_options()
                }
            )
            .is_err()
        );
        Ok(())
    }

    #[test]
    fn pixel_formats_preserve_chroma_and_do_not_confuse_nv12_with_12_bits() {
        assert_eq!(depth("nv12"), 8);
        assert_eq!(depth("p010le"), 10);
        assert_eq!(depth("yuv422p12le"), 12);
        assert!(pixel_format("yuv420p16le").is_none());
        assert!(pixel_format("yuva420p10le").is_none());
        assert_ne!(pixel_format("yuv422p10le"), pixel_format("yuv420p10le"));
    }

    #[test]
    fn full_frame_scan_detects_late_dynamic_hdr_and_changing_static_hdr() {
        let mut frames = Frames::default();
        for index in 0..100 {
            frames.observe(&format!("best_effort_timestamp_time={}|width=640|height=360|pix_fmt=yuv420p|interlaced_frame=0",index as f64/30.0));
        }
        frames.observe(
            "side_datum/dynamic:side_data_type=HDR Dynamic Metadata SMPTE2094-40 (HDR10+)",
        );
        assert!(frames.dynamic_hdr);
        frames.observe("side_datum/master:side_data_type=Mastering display metadata|red_x=1/2");
        frames.observe("side_datum/master:side_data_type=Mastering display metadata|red_x=1/3");
        assert!(frames.unsupported);
    }

    #[test]
    fn frame_level_color_data_identifies_hdr_and_rejects_changes() {
        let mut frames = Frames::default();
        frames.observe("best_effort_timestamp_time=0|width=640|height=360|pix_fmt=yuv420p10le|interlaced_frame=0|color_transfer=smpte2084|color_primaries=bt2020|color_space=bt2020nc");
        assert_eq!(
            frames.colors.get("color_transfer").map(String::as_str),
            Some("smpte2084")
        );
        assert!(!frames.unsupported);
        frames.observe("best_effort_timestamp_time=0.04|width=640|height=360|pix_fmt=yuv420p10le|interlaced_frame=0|color_transfer=bt709");
        assert!(frames.unsupported);
    }

    #[test]
    fn namespaced_compact_hdr_fields_retain_mastering_and_light_metadata() {
        let mut frames = Frames::default();
        frames.observe("side_datum/mastering_display_metadata:side_data_type=Mastering display metadata|side_datum/mastering_display_metadata:red_x=34000/50000|side_datum/mastering_display_metadata:red_y=16000/50000|side_datum/mastering_display_metadata:green_x=13250/50000|side_datum/mastering_display_metadata:green_y=34500/50000|side_datum/mastering_display_metadata:blue_x=7500/50000|side_datum/mastering_display_metadata:blue_y=3000/50000|side_datum/mastering_display_metadata:white_point_x=15635/50000|side_datum/mastering_display_metadata:white_point_y=16450/50000|side_datum/mastering_display_metadata:min_luminance=1/10000|side_datum/mastering_display_metadata:max_luminance=10000000/10000|side_datum/content_light_level_metadata:side_data_type=Content light level metadata|side_datum/content_light_level_metadata:max_content=1000|side_datum/content_light_level_metadata:max_average=400");
        let mut source = fixture();
        source.probe["frames"] = json!([{"side_data_list":frames.side_data}]);
        assert_eq!(
            hdr_params(&source),
            vec![
                "master-display=G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1)",
                "max-cll=1000,400"
            ]
        );
        assert!(x265_colors(&source).contains(&"transfer=smpte2084".into()));
    }

    #[test]
    fn video_bitrate_uses_video_duration_when_audio_runs_longer() {
        let mut source = fixture();
        source.duration = 20.0;
        source.video_bytes = 1_000_000;
        source.probe["compressionFrames"]["lastTimestamp"] = json!(9.966666666666667);
        source.probe["compressionFrames"]["lastDuration"] = json!(1.0 / 30.0);
        assert_eq!(video_bitrate(&source), (800_000, false));
        source.video["bit_rate"] = json!("900000");
        assert_eq!(video_bitrate(&source), (900_000, false));
    }

    #[test]
    fn matroska_omitted_undefined_language_retains_its_meaning() {
        assert!(preserved_tags(
            &json!({"language":"und","handler_name":"VideoHandler"}),
            &json!({"HANDLER_NAME":"VideoHandler"})
        ));
        assert!(!preserved_tags(&json!({"language":"eng"}), &json!({})));
    }

    #[test]
    fn unavailable_driver_reason_is_specific_and_does_not_expose_raw_diagnostics() {
        let error = anyhow::anyhow!(
            "secret=C:/private/file: Driver does not support the required nvenc API version. Required:13.1 Found:13.0"
        );
        let reason = capability_failure(&error);
        assert!(reason.contains("NVIDIA driver"));
        assert!(!reason.contains("private"));
    }

    #[test]
    fn timestamp_quantization_does_not_mark_cfr_mkv_as_variable() {
        let mut frames = Frames {
            timestamp_tolerance: 0.00201,
            ..Default::default()
        };
        for timestamp in [0.0, 0.033, 0.067, 0.100, 0.133, 0.167] {
            frames.observe(&format!("best_effort_timestamp_time={timestamp}|width=640|height=360|pix_fmt=yuv420p|interlaced_frame=0"));
        }
        assert!(!frames.variable);
        frames.observe("best_effort_timestamp_time=0.267|width=640|height=360|pix_fmt=yuv420p|interlaced_frame=0");
        assert!(frames.variable);
    }

    #[test]
    fn publication_verification_rejects_lost_streams_metadata_and_frames() -> Result<()> {
        let source = fixture();
        let plan = plan(&source, &direct_options())?;
        let original = encoded(&source);
        verify_preservation(&source, &plan, &original)?;
        for (path, value) in [
            ("/streams/0/pix_fmt", json!("yuv420p")),
            ("/streams/0/color_transfer", json!("bt709")),
            ("/streams/1/tags/language", json!("fra")),
            ("/streams/2/disposition/forced", json!(0)),
            ("/streams/3/extradata_hash", json!("SHA256:changed-font")),
            ("/chapters/0/tags/title", json!("Wrong")),
            ("/chapters/0/end_time", json!("9.9")),
            ("/compressionFrames/count", json!(299)),
            ("/compressionFrames/lastTimestamp", json!(9.0)),
        ] {
            let mut changed = original.clone();
            *changed.pointer_mut(path).unwrap() = value;
            assert!(
                verify_preservation(&source, &plan, &changed).is_err(),
                "{path}"
            );
        }
        let mut missing = original;
        missing["streams"].as_array_mut().unwrap().remove(2);
        assert!(verify_preservation(&source, &plan, &missing).is_err());
        Ok(())
    }

    #[test]
    fn chapter_timestamp_shifts_report_the_exact_difference() -> Result<()> {
        let source = fixture();
        let plan = plan(&source, &direct_options())?;
        let mut output = encoded(&source);
        output["chapters"][0]["start_time"] = json!("0.021000");
        let error = verify_preservation(&source, &plan, &output)
            .unwrap_err()
            .to_string();
        assert!(error.contains("chapter 1 start_time"));
        assert!(error.contains("expected 0.000000s, actual 0.021000s"));
        assert!(error.contains("+0.021000s"));
        Ok(())
    }

    #[test]
    fn vaapi_and_cpu_share_requested_filters_and_strict_pixel_format() -> Result<()> {
        let source = fixture();
        let plan = plan(
            &source,
            &CompressionOptions {
                max_width: Some(320),
                fps: Some("24000/1001".into()),
                ..direct_options()
            },
        )?;
        let args = encoder_args("hevc_vaapi", &plan);
        assert!(args.iter().any(|arg| arg == "+vaapi"));
        assert!(args.iter().any(|arg| arg.contains("scale=320:180")
            && arg.contains("fps=24000/1001")
            && arg.ends_with("hwupload")));
        assert!(
            encoder_args("libx265", &plan)
                .iter()
                .any(|arg| arg == "+yuv420p10le")
        );
        Ok(())
    }

    #[tokio::test]
    async fn source_hashing_obeys_cancellation() -> Result<()> {
        let file = tempfile::NamedTempFile::new()?;
        std::fs::write(file.path(), b"original")?;
        let cancel = CancellationToken::new();
        let (hash, size) = hash_path(file.path(), &cancel).await?;
        assert_eq!(size, 8);
        assert_eq!(hash, hex::encode(Sha256::digest(b"original")));
        cancel.cancel();
        assert!(hash_path(file.path(), &cancel).await.is_err());
        Ok(())
    }

    #[tokio::test]
    async fn preview_runtime_cache_recovers_after_unused_snapshot_cleanup() -> Result<()> {
        let root = tempfile::tempdir()?;
        let runtime = root.path().join("runtime");
        std::fs::create_dir_all(&runtime)?;
        for name in ["yt-dlp", "ffmpeg", "ffprobe", "deno"] {
            std::fs::write(
                runtime.join(format!("{name}{}", std::env::consts::EXE_SUFFIX)),
                b"runtime fixture",
            )?;
        }
        let app = App::open(crate::AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("output"),
            tool_dir: Some(runtime),
        })
        .await?;
        let cancel = CancellationToken::new();
        let before = current_tools(&app, &cancel).await?;
        for path in crate::tools::unused_cache(&app)? {
            ensure!(
                path.canonicalize()?
                    .starts_with(app.config.data_dir.canonicalize()?),
                "Cleanup escaped test data"
            );
            std::fs::remove_dir_all(path)?;
        }
        assert!(before.values().all(|path| !path.exists()));
        let after = current_tools(&app, &cancel).await?;
        assert!(after.values().all(|path| path.is_file()));
        app.shutdown().await?;
        Ok(())
    }
}

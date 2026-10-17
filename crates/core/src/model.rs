use anyhow::{Result, bail, ensure};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::path::PathBuf;

#[derive(Debug, Clone)]
pub struct AppConfig {
    pub data_dir: PathBuf,
    pub download_dir: PathBuf,
    pub tool_dir: Option<PathBuf>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Event {
    pub kind: String,
    pub data: Value,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default, rename_all = "camelCase", deny_unknown_fields)]
pub struct DownloadOptions {
    pub quality: String,
    #[serde(alias = "content_mode")]
    pub content_mode: String,
    #[serde(alias = "audio_format")]
    pub audio_format: String,
    #[serde(alias = "audio_track")]
    pub audio_track: String,
    pub container: String,
    #[serde(alias = "video_fps")]
    pub video_fps: String,
    #[serde(alias = "source_video_codec")]
    pub source_video_codec: String,
    #[serde(alias = "vr_mode")]
    pub vr_mode: String,
    #[serde(alias = "compatibility_target")]
    pub compatibility_target: String,
    #[serde(alias = "collection_mode")]
    pub collection_mode: String,
    #[serde(alias = "collection_order")]
    pub collection_order: String,
    #[serde(alias = "first_n")]
    pub first_n: usize,
    #[serde(alias = "playlist_items")]
    pub playlist_items: String,
    #[serde(alias = "date_after")]
    pub date_after: String,
    #[serde(alias = "date_before")]
    pub date_before: String,
    #[serde(alias = "duration_min")]
    pub duration_min: f64,
    #[serde(alias = "duration_max")]
    pub duration_max: f64,
    #[serde(alias = "live_filter")]
    pub live_filter: String,
    #[serde(alias = "live_from_start")]
    pub live_from_start: bool,
    #[serde(alias = "wait_for_live")]
    pub wait_for_live: bool,
    #[serde(alias = "wait_min")]
    pub wait_min: u32,
    #[serde(alias = "wait_max")]
    pub wait_max: u32,
    #[serde(alias = "section_start")]
    pub section_start: String,
    #[serde(alias = "section_end")]
    pub section_end: String,
    #[serde(alias = "split_chapters")]
    pub split_chapters: bool,
    #[serde(alias = "subtitle_format")]
    pub subtitle_format: String,
    #[serde(alias = "subtitle_language")]
    pub subtitle_language: String,
    pub subtitle_languages: Vec<String>,
    pub automatic_subtitles: bool,
    #[serde(alias = "embed_subtitles")]
    pub embed_subtitles: bool,
    #[serde(alias = "write_thumbnail")]
    pub write_thumbnail: bool,
    #[serde(alias = "write_description")]
    pub write_description: bool,
    #[serde(alias = "write_comments")]
    pub write_comments: bool,
    #[serde(alias = "write_info_json")]
    pub write_info_json: bool,
    #[serde(alias = "embed_metadata")]
    pub embed_metadata: bool,
    #[serde(alias = "embed_chapters")]
    pub embed_chapters: bool,
    #[serde(alias = "embed_thumbnail")]
    pub embed_thumbnail: bool,
    #[serde(alias = "sponsorblock_mode")]
    pub sponsorblock_mode: String,
    #[serde(alias = "sponsorblock_categories")]
    pub sponsorblock_categories: Vec<String>,
    #[serde(alias = "rate_limit")]
    pub rate_limit: String,
    #[serde(alias = "organize_task_folder")]
    pub organize_task_folder: bool,
    #[serde(alias = "processing_temp_dir")]
    pub processing_temp_dir: String,
    #[serde(alias = "prepend_cover_enabled")]
    pub prepend_cover_enabled: bool,
    #[serde(alias = "prepend_cover_frames")]
    pub prepend_cover_frames: u32,
    pub encoder: String,
    #[serde(alias = "format_id")]
    pub format_id: String,
    pub video_format_id: String,
    pub audio_format_ids: Vec<String>,
    pub cookie_id: String,
    pub proxy: String,
    pub fragment_concurrent: u32,
    pub request_delay: f64,
    pub filename_template: String,
    pub convert_cover_jpeg: bool,
    pub cover_jpeg_quality: u8,
}

impl Default for DownloadOptions {
    fn default() -> Self {
        Self {
            quality: "best".into(),
            content_mode: "video".into(),
            audio_format: "best".into(),
            audio_track: "default".into(),
            container: "auto".into(),
            video_fps: "best".into(),
            source_video_codec: "auto".into(),
            vr_mode: "any".into(),
            compatibility_target: "auto".into(),
            collection_mode: "select".into(),
            collection_order: "original".into(),
            first_n: 0,
            playlist_items: String::new(),
            date_after: String::new(),
            date_before: String::new(),
            duration_min: 0.0,
            duration_max: 0.0,
            live_filter: "videos".into(),
            live_from_start: false,
            wait_for_live: false,
            wait_min: 60,
            wait_max: 300,
            section_start: String::new(),
            section_end: String::new(),
            split_chapters: false,
            subtitle_format: "srt".into(),
            subtitle_language: "none".into(),
            subtitle_languages: vec![],
            automatic_subtitles: true,
            embed_subtitles: false,
            write_thumbnail: true,
            write_description: false,
            write_comments: false,
            write_info_json: true,
            embed_metadata: false,
            embed_chapters: false,
            embed_thumbnail: false,
            sponsorblock_mode: "off".into(),
            sponsorblock_categories: vec![],
            rate_limit: String::new(),
            organize_task_folder: false,
            processing_temp_dir: String::new(),
            prepend_cover_enabled: false,
            prepend_cover_frames: 3,
            encoder: "copy".into(),
            format_id: String::new(),
            video_format_id: String::new(),
            audio_format_ids: vec![],
            cookie_id: "auto".into(),
            proxy: String::new(),
            fragment_concurrent: 4,
            request_delay: 0.0,
            filename_template: "%(title)s.%(ext)s".into(),
            convert_cover_jpeg: true,
            cover_jpeg_quality: 90,
        }
    }
}

impl DownloadOptions {
    pub fn parse(value: Value) -> Result<Self> {
        let mut options: Self =
            serde_json::from_value(if value.is_null() { json!({}) } else { value })?;
        if (9..=32).contains(&options.fragment_concurrent) {
            options.fragment_concurrent = 8;
        }
        options.cookie_id = "auto".into();
        if options.filename_template == "%(title)s [%(id)s].%(ext)s" {
            options.filename_template = "%(title)s.%(ext)s".into();
        }
        options.validate()?;
        Ok(options)
    }

    pub fn validate(&self) -> Result<()> {
        for (name, value, allowed) in [
            (
                "contentMode",
                self.content_mode.as_str(),
                "video audio manual",
            ),
            (
                "quality",
                &self.quality,
                "best worst 8k 4k 2k 1k 1080p 720p 480p 360p 240p qcif",
            ),
            (
                "audioFormat",
                &self.audio_format,
                "best aac alac flac m4a mp3 opus vorbis wav",
            ),
            ("container", &self.container, "auto mp4 mkv mov webm"),
            ("videoFps", &self.video_fps, "best 240 120 60 50 30 25 24"),
            (
                "sourceVideoCodec",
                &self.source_video_codec,
                "auto h264 h265 av1 vp9",
            ),
            ("vrMode", &self.vr_mode, "any 2d360 3d180 3d360 none"),
            (
                "compatibilityTarget",
                &self.compatibility_target,
                "auto windows macos linux ios android",
            ),
            ("collectionMode", &self.collection_mode, "select single all"),
            (
                "collectionOrder",
                &self.collection_order,
                "original reverse random",
            ),
            ("liveFilter", &self.live_filter, "videos all live"),
            (
                "subtitleFormat",
                &self.subtitle_format,
                "best vtt srt ass lrc",
            ),
            (
                "sponsorblockMode",
                &self.sponsorblock_mode,
                "off mark remove",
            ),
            (
                "encoder",
                &self.encoder,
                "copy original libx264 libx265 libvpx-vp9 libsvtav1 libaom-av1 librav1e h264_nvenc hevc_nvenc av1_nvenc h264_qsv hevc_qsv av1_qsv h264_amf hevc_amf av1_amf",
            ),
            (
                "audioTrack",
                &self.audio_track,
                "default original all zh-Hans zh-Hant en ja ko es fr de ru pt ar hi id vi th tr",
            ),
        ] {
            ensure!(
                allowed.split_whitespace().any(|v| v == value),
                "Invalid {name}: {value}"
            );
        }
        ensure!(self.first_n <= 1_000_000, "firstN is too large");
        ensure!(
            self.wait_min >= 1 && self.wait_max >= self.wait_min && self.wait_max <= 86400,
            "Invalid live wait interval"
        );
        ensure!(
            (1..=300).contains(&self.prepend_cover_frames),
            "Cover frames must be 1–300"
        );
        ensure!(
            (1..=8).contains(&self.fragment_concurrent),
            "Fragment concurrency must be 1–8"
        );
        ensure!(
            (1..=100).contains(&self.cover_jpeg_quality),
            "JPEG quality must be 1–100"
        );
        ensure!(
            self.filename_template.len() <= 240
                && !self.filename_template.contains(['/', '\\'])
                && !self.filename_template.contains(".."),
            "Filename template must be a filename, without directories"
        );
        let remaining = self
            .filename_template
            .replace("%(title)s", "")
            .replace("%(id)s", "")
            .replace("%(ext)s", "");
        ensure!(
            !remaining.contains('%'),
            "Filename template supports title, id and ext fields only"
        );
        ensure!(
            self.request_delay.is_finite() && (0.0..=60.0).contains(&self.request_delay),
            "Request delay must be 0–60 seconds"
        );
        ensure!(
            self.duration_min.is_finite()
                && self.duration_max.is_finite()
                && self.duration_min >= 0.0
                && self.duration_max >= 0.0,
            "Invalid duration filter"
        );
        ensure!(
            self.duration_max == 0.0 || self.duration_max >= self.duration_min,
            "Duration range is reversed"
        );
        for date in [&self.date_after, &self.date_before] {
            if !date.is_empty() {
                let d = date.replace('-', "");
                ensure!(
                    d.len() == 8
                        && d.chars().all(|c| c.is_ascii_digit())
                        && chrono::NaiveDate::parse_from_str(&d, "%Y%m%d").is_ok(),
                    "Invalid date: {date}"
                );
            }
        }
        let start = parse_time(&self.section_start)?.unwrap_or(0.0);
        let end = parse_time(&self.section_end)?;
        ensure!(
            end.is_none_or(|v| v > start),
            "Section end must follow start"
        );
        ensure!(
            self.playlist_items.len() <= 200
                && self
                    .playlist_items
                    .chars()
                    .all(|c| c.is_ascii_digit() || ":,-".contains(c)),
            "Invalid playlist items"
        );
        for id in self
            .audio_format_ids
            .iter()
            .chain([&self.format_id, &self.video_format_id])
        {
            ensure!(
                id.len() <= 100
                    && id
                        .chars()
                        .all(|c| c.is_ascii_alphanumeric() || "_-.".contains(c)),
                "Invalid format ID"
            );
        }
        if self.content_mode == "manual" {
            ensure!(
                !self.format_id.is_empty() || !self.video_format_id.is_empty(),
                "Manual mode requires an explicitly selected format"
            );
        }
        for language in self.languages() {
            ensure!(
                language.len() <= 40
                    && language
                        .chars()
                        .all(|c| c.is_ascii_alphanumeric() || "-_.*".contains(c)),
                "Invalid subtitle language"
            );
        }
        for category in &self.sponsorblock_categories {
            ensure!(
                "sponsor intro outro selfpromo interaction preview music_offtopic filler"
                    .split_whitespace()
                    .any(|v| v == category),
                "Invalid SponsorBlock category"
            );
        }
        if self.sponsorblock_mode != "off" {
            ensure!(
                !self.sponsorblock_categories.is_empty(),
                "Select at least one SponsorBlock category"
            );
        }
        if self.embed_subtitles {
            ensure!(
                !self.languages().is_empty(),
                "Select subtitle languages before embedding subtitles"
            );
        }
        if !self.rate_limit.is_empty() {
            ensure!(parse_rate(&self.rate_limit).is_some(), "Invalid rate limit");
        }
        if !self.proxy.is_empty() {
            let u = url::Url::parse(&self.proxy)?;
            ensure!(
                matches!(u.scheme(), "http" | "https" | "socks5" | "socks5h"),
                "Unsupported proxy scheme"
            );
        }
        // shortcut: arbitrary temporary paths are accepted only by local hosts, web strips this option at its boundary.
        Ok(())
    }

    pub fn languages(&self) -> Vec<String> {
        let mut result = self.subtitle_languages.clone();
        if self.subtitle_language != "none" && !self.subtitle_language.is_empty() {
            result.extend(
                self.subtitle_language
                    .split(',')
                    .map(|s| s.trim().to_string()),
            );
        }
        result.sort();
        result.dedup();
        result
    }
    pub fn effective_container(&self) -> &str {
        if self.container != "auto" {
            &self.container
        } else if self.compatibility_target == "linux" {
            "mkv"
        } else if self.compatibility_target != "auto" {
            "mp4"
        } else {
            "auto"
        }
    }
    pub fn effective_codec(&self) -> &str {
        if self.source_video_codec != "auto" {
            &self.source_video_codec
        } else if matches!(
            self.compatibility_target.as_str(),
            "windows" | "macos" | "ios" | "android"
        ) {
            "h264"
        } else {
            "auto"
        }
    }
    pub fn max_height(&self) -> Option<u64> {
        match self.quality.as_str() {
            "8k" => Some(4320),
            "4k" => Some(2160),
            "2k" => Some(1440),
            "1k" | "1080p" => Some(1080),
            "720p" => Some(720),
            "480p" => Some(480),
            "360p" => Some(360),
            "240p" => Some(240),
            "qcif" => Some(144),
            _ => None,
        }
    }
    pub fn public(&self) -> Value {
        let mut value = serde_json::to_value(self).unwrap();
        value.as_object_mut().unwrap().remove("proxy");
        value.as_object_mut().unwrap().remove("processingTempDir");
        value
    }
    pub fn matches(&self, info: &Value) -> bool {
        self.exclusion_reason(info).is_none()
    }
    pub(crate) fn exclusion_reason(&self, info: &Value) -> Option<&'static str> {
        let live = info["is_live"].as_bool().unwrap_or(false)
            || matches!(text(info, "live_status"), "is_live" | "is_upcoming");
        if self.live_filter == "videos" && live {
            return Some(
                "Source is live or upcoming, but liveFilter is set to videos. Choose All content to download live streams.",
            );
        }
        if self.live_filter == "live" && !live {
            return Some(
                "Source is not live or upcoming, but liveFilter is set to live. Choose All content to download recordings.",
            );
        }
        if let Some(d) = info["duration"].as_f64() {
            if d < self.duration_min {
                return Some("Source duration is shorter than durationMin.");
            }
            if self.duration_max > 0.0 && d > self.duration_max {
                return Some("Source duration is longer than durationMax.");
            }
        }
        let date = text(info, "upload_date").replace('-', "");
        if !date.is_empty() {
            if !self.date_after.is_empty() && date < self.date_after.replace('-', "") {
                return Some("Source upload date is earlier than dateAfter.");
            }
            if !self.date_before.is_empty() && date > self.date_before.replace('-', "") {
                return Some("Source upload date is later than dateBefore.");
            }
        }
        None
    }
}

pub fn parse_time(value: &str) -> Result<Option<f64>> {
    if value.is_empty() || value == "inf" {
        return Ok(None);
    }
    ensure!(value.len() <= 24, "Invalid timestamp");
    let parts: Vec<_> = value.split(':').collect();
    ensure!(parts.len() <= 3, "Invalid timestamp: {value}");
    let mut time = 0.0;
    for part in parts {
        let v: f64 = part.parse()?;
        ensure!(v.is_finite() && v >= 0.0, "Invalid timestamp");
        time = time * 60.0 + v;
    }
    Ok(Some(time))
}

pub fn parse_rate(value: &str) -> Option<u64> {
    let s = value.trim().to_ascii_lowercase();
    let s = s.trim_end_matches('b').trim_end_matches('i');
    let (number, power) = match s.chars().last()? {
        'k' => (s.strip_suffix('k')?, 1),
        'm' => (s.strip_suffix('m')?, 2),
        'g' => (s.strip_suffix('g')?, 3),
        't' => (s.strip_suffix('t')?, 4),
        'p' => (s.strip_suffix('p')?, 5),
        'e' => (s.strip_suffix('e')?, 6),
        c if c.is_ascii_digit() => (s, 0),
        _ => return None,
    };
    let rate = number.parse::<f64>().ok()? * 1024f64.powi(power);
    (rate.is_finite() && rate >= 1.0 && rate <= u64::MAX as f64).then_some(rate as u64)
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct Task {
    pub id: String,
    #[serde(default = "media_kind")]
    pub kind: String,
    #[serde(default)]
    pub files: Vec<GeneratedFile>,
    pub url: String,
    pub title: String,
    #[serde(default)]
    pub thumbnail: Option<PathBuf>,
    #[serde(default)]
    pub size: Option<u64>,
    #[serde(default)]
    pub transfer_bytes: Vec<TransferBytes>,
    #[serde(default)]
    pub source_info: Option<MediaInfo>,
    #[serde(default)]
    pub media_info: Option<MediaInfo>,
    pub status: String,
    #[serde(default)]
    pub priority: u64,
    #[serde(default)]
    pub live: bool,
    pub stage: String,
    #[serde(default)]
    pub steps: Vec<String>,
    #[serde(default)]
    pub step_index: Option<usize>,
    pub progress: f64,
    pub stage_progress: Option<f64>,
    pub stage_indeterminate: bool,
    pub speed: Option<f64>,
    pub eta: Option<f64>,
    pub error: Option<String>,
    #[serde(default)]
    pub warnings: Vec<String>,
    pub created_at: String,
    pub options: DownloadOptions,
    #[serde(default)]
    pub download_dir: Option<PathBuf>,
    pub output_path: Option<PathBuf>,
    #[serde(default)]
    pub previous_outputs: Vec<PathBuf>,
    #[serde(default)]
    pub cookie_snapshot: Option<String>,
    #[serde(default)]
    pub proxy_snapshot: Option<String>,
    #[serde(default)]
    pub manifest: Option<SourceManifest>,
    #[serde(default)]
    pub publication: Option<Publication>,
    #[serde(default)]
    pub destination: Option<crate::destination::Destination>,
    #[serde(default)]
    pub conversion: bool,
    #[serde(default)]
    // Private immutable runtime references. Missing snapshots must fail when used, never resolve current tools.
    pub tools: std::collections::HashMap<String, PathBuf>,
}

impl Task {
    pub fn public(&self) -> Value {
        let downloaded_bytes = (!self.transfer_bytes.is_empty())
            .then(|| {
                self.transfer_bytes
                    .iter()
                    .try_fold(0u64, |sum, item| sum.checked_add(item.downloaded))
            })
            .flatten();
        let total_bytes = (!self.transfer_bytes.is_empty())
            .then(|| {
                self.transfer_bytes
                    .iter()
                    .try_fold(0u64, |sum, item| sum.checked_add(item.total?))
            })
            .flatten();
        json!({"id":self.id,"kind":self.kind,"files":self.files,"url":redact_url(&self.url),"title":self.title,"status":self.status,"stage":self.stage,"priority":self.priority,"live":self.live,"conversion":self.conversion,"destinationConflict":self.destination.as_ref().and_then(|d| d.public_conflict()),
            "progress":self.progress,"stageProgress":self.stage_progress,"stageIndeterminate":self.stage_indeterminate,"stepIndex":self.step_index,"stepCount":self.steps.len(),
            "speed":self.speed,"eta":self.eta,"size":self.size,"downloadedBytes":downloaded_bytes,"totalBytes":total_bytes,"totalBytesEstimated":total_bytes.is_some() && self.transfer_bytes.iter().any(|item| item.estimated),"sourceInfo":self.source_info,"mediaInfo":self.media_info,"error":self.error,"warnings":self.warnings,"createdAt":self.created_at,"options":self.options.public(),"hasThumbnail":self.thumbnail.is_some(),
            "outputPath":self.output_path.as_ref().and_then(|p|p.file_name()).map(|p|p.to_string_lossy())})
    }
    pub fn stage(&mut self, status: &str, stage: &str, percent: Option<f64>) {
        self.status = status.into();
        self.stage = stage.into();
        if let Some(index) = crate::workflow::position(stage, &self.steps) {
            self.step_index = Some(index);
        }
        self.stage_progress = percent;
        self.stage_indeterminate = percent.is_none();
        self.speed = None;
        self.eta = None;
        self.progress = if status == "completed" {
            100.0
        } else {
            percent.unwrap_or(0.0).min(99.0)
        };
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub(crate) struct TransferBytes {
    pub format_id: String,
    pub downloaded: u64,
    pub total: Option<u64>,
    pub estimated: bool,
}

fn media_kind() -> String {
    "media".into()
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct GeneratedFile {
    pub filename: String,
    pub kind: String,
    pub size: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub(crate) struct SourceFile {
    pub path: PathBuf,
    pub hash: String,
    pub size: u64,
    pub kind: String,
    pub format_id: String,
    pub language: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub(crate) struct SourceManifest {
    pub version: u32,
    pub directory: PathBuf,
    pub files: Vec<SourceFile>,
    pub metadata: Value,
    pub duration: Option<f64>,
    pub chapters: Vec<Chapter>,
    pub sponsor_segments: Vec<Chapter>,
    pub output_extension: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub(crate) struct Chapter {
    pub start: f64,
    pub end: f64,
    pub title: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub(crate) struct Publication {
    #[serde(default)]
    pub committed: bool,
    pub temporary: PathBuf,
    pub target: PathBuf,
    pub hash: String,
    pub size: u64,
    pub duration: f64,
    pub auxiliary: Vec<AuxiliaryPublication>,
    pub backup: Option<PathBuf>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub(crate) struct AuxiliaryPublication {
    pub temporary: PathBuf,
    pub target: PathBuf,
    pub hash: String,
    pub size: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct Media {
    pub id: String,
    pub task_id: String,
    pub title: String,
    pub filename: String,
    pub size: u64,
    pub duration: f64,
    pub path: PathBuf,
    #[serde(default)]
    pub assets: Vec<PathBuf>,
    #[serde(default)]
    pub media_info: Option<MediaInfo>,
}
impl Media {
    pub fn public(&self) -> Value {
        let mut info = self.media_info.clone();
        if let Some(info) = info.as_mut() {
            info.size = Some(self.size);
        }
        json!({"id":self.id,"taskId":self.task_id,"title":self.title,"filename":self.filename,"size":self.size,"duration":self.duration,"mediaInfo":info})
    }
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct MediaInfo {
    pub width: Option<u64>,
    pub height: Option<u64>,
    pub fps: Option<f64>,
    pub video_codec: Option<String>,
    pub audio_codec: Option<String>,
    pub container: Option<String>,
    pub size: Option<u64>,
    pub duration: Option<f64>,
}

impl MediaInfo {
    pub fn from_probe(probe: &Value, size: Option<u64>, container: Option<&str>) -> Self {
        let streams = probe["streams"]
            .as_array()
            .map(Vec::as_slice)
            .unwrap_or(&[]);
        let video = streams
            .iter()
            .find(|s| text(s, "codec_type") == "video" && s["disposition"]["attached_pic"] != 1)
            .unwrap_or(&Value::Null);
        let audio = streams
            .iter()
            .find(|s| text(s, "codec_type") == "audio")
            .unwrap_or(&Value::Null);
        Self {
            width: video["width"].as_u64().filter(|v| *v > 0),
            height: video["height"].as_u64().filter(|v| *v > 0),
            fps: rate(text(video, "avg_frame_rate")).or_else(|| rate(text(video, "r_frame_rate"))),
            video_codec: codec(text(video, "codec_name")),
            audio_codec: codec(text(audio, "codec_name")),
            container: container.and_then(codec),
            size,
            duration: text(&probe["format"], "duration")
                .parse::<f64>()
                .ok()
                .filter(|v| v.is_finite() && *v > 0.0),
        }
    }

    pub fn from_formats(formats: &[Value], duration: Option<f64>) -> Self {
        let video = formats
            .iter()
            .find(|f| codec(text(f, "vcodec")).is_some())
            .unwrap_or(&Value::Null);
        let audio = formats
            .iter()
            .find(|f| codec(text(f, "acodec")).is_some())
            .unwrap_or(&Value::Null);
        Self {
            width: video["width"].as_u64().filter(|v| *v > 0),
            height: video["height"].as_u64().filter(|v| *v > 0),
            fps: video["fps"].as_f64().filter(|v| v.is_finite() && *v > 0.0),
            video_codec: codec(text(video, "vcodec")),
            audio_codec: codec(text(audio, "acodec")),
            container: codec(text(if video.is_null() { audio } else { video }, "ext")),
            size: None,
            duration: duration.filter(|v| v.is_finite() && *v > 0.0),
        }
    }
}

fn codec(value: &str) -> Option<String> {
    (!value.is_empty()
        && value != "none"
        && value.len() <= 64
        && value
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || "._,-".contains(c)))
    .then(|| value.into())
}

fn rate(value: &str) -> Option<f64> {
    let value = if let Some((a, b)) = value.split_once('/') {
        a.parse::<f64>().ok()? / b.parse::<f64>().ok()?
    } else {
        value.parse().ok()?
    };
    (value.is_finite() && value > 0.0).then_some(value)
}

pub(crate) fn text<'a>(value: &'a Value, key: &str) -> &'a str {
    value[key].as_str().unwrap_or("")
}

pub fn redact_url(value: &str) -> String {
    if let Ok(mut url) = url::Url::parse(value) {
        // A public YouTube video ID is needed to identify and re-add failed tasks.
        let video_id = if matches!(
            url.host_str(),
            Some("youtube.com" | "www.youtube.com" | "m.youtube.com" | "music.youtube.com")
        ) && url.path() == "/watch"
        {
            url.query_pairs()
                .find(|(key, value)| {
                    key == "v"
                        && value.len() == 11
                        && value
                            .chars()
                            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_'))
                })
                .map(|(_, value)| value.into_owned())
        } else {
            None
        };
        let _ = url.set_username("");
        let _ = url.set_password(None);
        url.set_query(None);
        url.set_fragment(None);
        if let Some(video_id) = video_id {
            url.query_pairs_mut().append_pair("v", &video_id);
        }
        url.to_string()
    } else {
        "[redacted URL]".into()
    }
}

pub(crate) fn validate_url(value: &str) -> Result<()> {
    ensure!(value.len() <= 8192, "URL is too long");
    let url = url::Url::parse(value)?;
    if !matches!(url.scheme(), "http" | "https") {
        bail!("Only HTTP and HTTPS media URLs are supported");
    }
    ensure!(
        url.host_str().is_some() && url.username().is_empty() && url.password().is_none(),
        "URL cannot include login credentials"
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn finished_live_recordings_are_downloadable_as_regular_videos() {
        let options = DownloadOptions::parse(json!({"liveFilter":"videos"})).unwrap();
        for status in ["was_live", "post_live", "not_live"] {
            assert!(
                options.matches(&json!({"is_live":false,"was_live":true,"live_status":status})),
                "A finished recording must not be excluded by the non-live filter: {status}"
            );
        }
        for status in ["is_live", "is_upcoming"] {
            assert!(!options.matches(&json!({"live_status":status})));
        }
        let live_only = DownloadOptions::parse(json!({"liveFilter":"live"})).unwrap();
        assert!(!live_only.matches(&json!({"live_status":"was_live"})));
        assert!(live_only.matches(&json!({"live_status":"is_live"})));
        let all = DownloadOptions::parse(json!({"liveFilter":"all"})).unwrap();
        assert!(all.matches(&json!({"live_status":"is_live"})));
    }
    #[test]
    fn filters_report_the_specific_exclusion_without_changing_date_or_duration_bounds() {
        let options = DownloadOptions::parse(json!({"durationMin":10,"durationMax":20,"dateAfter":"2026-10-01","dateBefore":"2026-10-17"})).unwrap();
        assert!(
            options
                .exclusion_reason(&json!({"live_status":"is_live"}))
                .unwrap()
                .contains("liveFilter")
        );
        for (info, field) in [
            (json!({"duration":9}), "durationMin"),
            (json!({"duration":21}), "durationMax"),
            (json!({"upload_date":"20260930"}), "dateAfter"),
            (json!({"upload_date":"20261018"}), "dateBefore"),
        ] {
            assert!(options.exclusion_reason(&info).unwrap().contains(field));
            assert!(!options.matches(&info));
        }
        for duration in [10, 20] {
            for date in ["20261001", "20261017"] {
                assert!(options.matches(&json!({"duration":duration,"upload_date":date})));
            }
        }
    }
    #[test]
    fn media_summaries_only_expose_selected_technical_fields() {
        let info = MediaInfo::from_formats(
            &[
                json!({"width":1080,"height":1920,"fps":60,"vcodec":"avc1.640028","acodec":"none","ext":"mp4","url":"https://secret.invalid/?token=secret","http_headers":{"Cookie":"secret"}}),
                json!({"vcodec":"none","acodec":"aac"}),
            ],
            Some(12.0),
        );
        assert_eq!(info.width, Some(1080));
        assert_eq!(info.height, Some(1920));
        assert_eq!(info.audio_codec.as_deref(), Some("aac"));
        assert!(!serde_json::to_string(&info).unwrap().contains("secret"));
        let probe = json!({"streams":[{"codec_type":"video","codec_name":"mjpeg","width":16,"height":16,"disposition":{"attached_pic":1}},{"codec_type":"audio","codec_name":"aac"}],"format":{"filename":"/private/media","duration":"NaN"}});
        let audio = MediaInfo::from_probe(&probe, Some(123), Some("m4a"));
        assert_eq!(audio.height, None);
        assert_eq!(audio.video_codec, None);
        assert_eq!(audio.duration, None);
        assert_eq!(audio.size, Some(123));
        assert!(!serde_json::to_string(&audio).unwrap().contains("private"));
        assert!(rate("0/0").is_none());
        assert_eq!(rate("30000/1001"), Some(30000.0 / 1001.0));
    }
    #[test]
    fn legacy_publication_journals_start_uncommitted() {
        let journal: Publication = serde_json::from_value(json!({
            "temporary":"prepared.publish", "target":"output.mp4", "hash":"hash", "size":1,
            "duration":1.0, "auxiliary":[], "backup":null
        }))
        .unwrap();
        assert!(!journal.committed);
    }
    #[test]
    fn new_downloads_use_automatic_cookies_and_bounded_fragment_defaults() {
        let defaults = DownloadOptions::parse(json!({})).unwrap();
        assert_eq!(defaults.fragment_concurrent, 4);
        assert_eq!(defaults.subtitle_language, "none");
        assert_eq!(defaults.subtitle_format, "srt");
        for value in [1, 4, 6, 8] {
            assert_eq!(
                DownloadOptions::parse(json!({"fragmentConcurrent":value}))
                    .unwrap()
                    .fragment_concurrent,
                value
            );
        }
        assert_eq!(
            DownloadOptions::parse(json!({"fragmentConcurrent":32,"cookieId":"legacy-id"}))
                .unwrap()
                .fragment_concurrent,
            8
        );
        assert_eq!(
            DownloadOptions::parse(json!({"cookieId":"legacy-id"}))
                .unwrap()
                .cookie_id,
            "auto"
        );
        for value in [0, 33] {
            assert!(DownloadOptions::parse(json!({"fragmentConcurrent":value})).is_err());
        }
        let legacy: DownloadOptions =
            serde_json::from_value(json!({"fragmentConcurrent":32,"cookieId":"legacy-id"}))
                .unwrap();
        assert_eq!(legacy.fragment_concurrent, 32);
        assert_eq!(legacy.cookie_id, "legacy-id");
    }
    #[test]
    fn public_youtube_links_keep_only_the_video_id() {
        assert_eq!(
            redact_url(
                "https://user:secret@www.youtube.com/watch?v=jNQXAC9IVRw&token=secret#secret"
            ),
            "https://www.youtube.com/watch?v=jNQXAC9IVRw"
        );
        assert_eq!(
            redact_url("https://www.youtube.com.evil.test/watch?v=jNQXAC9IVRw&token=secret"),
            "https://www.youtube.com.evil.test/watch"
        );
        assert_eq!(
            redact_url("https://www.youtube.com/watch?v=secret&token=secret"),
            "https://www.youtube.com/watch"
        );
    }
    #[test]
    fn filename_defaults_and_legacy_options_do_not_append_ids() {
        assert_eq!(
            DownloadOptions::default().filename_template,
            "%(title)s.%(ext)s"
        );
        let legacy =
            DownloadOptions::parse(json!({"filenameTemplate":"%(title)s [%(id)s].%(ext)s"}))
                .unwrap();
        assert_eq!(legacy.filename_template, "%(title)s.%(ext)s");
        let custom = "Archive - %(title)s.%(ext)s";
        assert_eq!(
            DownloadOptions::parse(json!({"filenameTemplate":custom}))
                .unwrap()
                .filename_template,
            custom
        );
    }

    #[test]
    fn options_are_frozen_strict_and_compatible() {
        let options=DownloadOptions::parse(json!({"content_mode":"audio","audio_track":"all","audio_format":"flac","section_start":"1:02.5","section_end":"1:10"})).unwrap();
        assert_eq!(options.audio_track, "all");
        assert_eq!(parse_time(&options.section_start).unwrap(), Some(62.5));
        assert!(DownloadOptions::parse(json!({"unknownRequestedFeature":true})).is_err());
        assert!(DownloadOptions::parse(json!({"sectionStart":"10","sectionEnd":"3"})).is_err());
        assert!(DownloadOptions::parse(json!({"embedSubtitles":true})).is_err());
        assert_eq!(parse_rate("1.5MiB"), Some(1572864));
        assert_eq!(
            redact_url("https://a:secret@example.com/watch?token=secret#x"),
            "https://example.com/watch"
        );
    }
}

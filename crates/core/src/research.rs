//! Bounded yt-dlp queries shared by the desktop MCP adapter.
use crate::{
    App, DownloadOptions,
    download::{Cookies, base_args},
    model::{text, validate_url},
    process,
};
use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};
use tokio_util::sync::CancellationToken;

impl App {
    pub fn mcp_settings(&self) -> Result<Value> {
        let value = self.db.settings()?["mcp"].clone();
        Ok(if value.is_object() {
            value
        } else {
            json!({"enabled":false,"downloads":false,"port":19492})
        })
    }
    pub fn mcp_save_settings(&self, config: Value) -> Result<()> {
        let mut settings = self.db.settings()?;
        settings["mcp"] = config;
        self.db.save_settings(&settings)?;
        self.emit("settings.updated", self.public_settings()?);
        Ok(())
    }
    pub fn research_error(&self, error: &str) -> String {
        let mut message = process::redact(error);
        for path in [
            Some(self.config.data_dir.as_path()),
            Some(self.config.download_dir.as_path()),
            self.config.tool_dir.as_deref(),
        ]
        .into_iter()
        .flatten()
        {
            let path = path.to_string_lossy();
            message = message
                .replace(path.as_ref(), "[application storage]")
                .replace(&path.replace('\\', "/"), "[application storage]");
        }
        message
    }
    pub fn research_options(&self) -> Result<Value> {
        let mut value = self.db.settings()?["downloadOptions"].clone();
        if !value.is_object() {
            value = json!({});
        }
        Ok(value)
    }
    pub fn research_tasks(&self) -> Result<Vec<Value>> {
        Ok(self
            .db
            .tasks()?
            .iter()
            .map(|task| {
                let public = task.public();
                let mut value = project(
                    &public,
                    &[
                        "id",
                        "title",
                        "status",
                        "stage",
                        "progress",
                        "speed",
                        "eta",
                        "error",
                        "createdAt",
                    ],
                );
                if let Some(error) = value["error"].as_str() {
                    value["error"] = json!(self.research_error(error));
                }
                value
            })
            .collect())
    }
    pub async fn research(
        &self,
        operation: &str,
        args: &Value,
        cancel: &CancellationToken,
    ) -> Result<Value> {
        let mut options = DownloadOptions::parse(self.research_options()?)?;
        options.proxy = self
            .credentials
            .secret("downloadProxy")?
            .unwrap_or_default();
        let cookie = self.credential_snapshot(&options)?;
        let cookies = Cookies::new(self, cookie.as_deref())?;
        let _permit = tokio::select! { p=self.network.acquire()=>p?, _=cancel.cancelled()=>anyhow::bail!("Query cancelled") };
        let mut cli = base_args(self, &options, &cookies, None).await?;
        let url = text(args, "url");
        if operation != "search_videos" {
            validate_url(url)?;
        }
        cli.extend(["--simulate".into(), "--dump-single-json".into()]);
        if matches!(operation, "get_video_info" | "get_video_transcript") {
            // Some extractors discover tracks only when subtitle extraction is requested.
            cli.extend(["--write-subs".into(), "--write-auto-subs".into()]);
        }
        let offset = args["offset"].as_u64().unwrap_or(0);
        let limit = args["limit"]
            .as_u64()
            .unwrap_or(if operation == "search_videos" { 10 } else { 20 });
        let input = if operation == "search_videos" {
            let query = text(args, "query");
            ensure!(
                !query.trim().is_empty() && query.len() <= 1000,
                "Enter a search query of at most 1000 bytes"
            );
            cli.push("--flat-playlist".into());
            format!("ytsearch{limit}:{query}")
        } else {
            if operation == "list_collection_items" {
                cli.extend([
                    "--flat-playlist".into(),
                    "--playlist-items".into(),
                    format!("{}:{}", offset + 1, offset + limit + 1),
                ]);
            } else {
                cli.push("--no-playlist".into());
            }
            if operation == "get_video_comments" {
                ensure!(
                    youtube_url(url),
                    "Bounded comments are currently supported only for YouTube; this site's extractor cannot enforce a comment limit"
                );
                cli.extend([
                    "--write-comments".into(),
                    "--extractor-args".into(),
                    format!(
                        "youtube:max_comments={limit};comment_sort={}",
                        if text(args, "sort") == "newest" {
                            "new"
                        } else {
                            "top"
                        }
                    ),
                ]);
            }
            url.to_owned()
        };
        let temp = tempfile::tempdir_in(&self.config.data_dir)?;
        if operation == "get_video_transcript" {
            // Track selection needs metadata first; subtitle URLs never leave the core.
            let info = download_info(self, &cli, &input, cancel).await?;
            let language = choose_language(
                &info,
                text(args, "language"),
                text(&self.db.settings()?, "uiLanguage"),
            )?;
            let automatic = info["subtitles"][&language]
                .as_array()
                .is_none_or(|tracks| tracks.is_empty());
            let mut sub_cli = base_args(self, &options, &cookies, None).await?;
            sub_cli.extend([
                "--skip-download".into(),
                "--no-playlist".into(),
                if automatic {
                    "--write-auto-subs"
                } else {
                    "--write-subs"
                }
                .into(),
                "--sub-langs".into(),
                language.clone(),
                "--sub-format".into(),
                "json3/vtt/srt".into(),
                "-o".into(),
                temp.path()
                    .join("subtitle.%(ext)s")
                    .to_string_lossy()
                    .into_owned(),
                "--".into(),
                input,
            ]);
            process::run(
                &self.tool("yt-dlp")?,
                &sub_cli,
                Some(temp.path()),
                cancel,
                None,
            )
            .await?;
            let mut segments = Vec::new();
            for file in std::fs::read_dir(temp.path())? {
                let path = file?.path();
                ensure!(
                    std::fs::symlink_metadata(&path)?.is_file(),
                    "Invalid subtitle output"
                );
                ensure!(
                    std::fs::metadata(&path)?.len() <= 16 * 1024 * 1024,
                    "Subtitle is too large"
                );
                if path.extension().is_some_and(|ext| ext == "json3") {
                    segments = parse_json3(&std::fs::read_to_string(&path)?)?;
                    break;
                }
                if path
                    .extension()
                    .is_some_and(|ext| ext == "vtt" || ext == "srt")
                {
                    segments = parse_vtt(&std::fs::read_to_string(&path)?);
                }
            }
            ensure!(
                !segments.is_empty(),
                "No subtitles found for the selected language"
            );
            let start = args["startTime"].as_f64().unwrap_or(0.0);
            let end = args["endTime"].as_f64().unwrap_or(f64::MAX);
            segments.retain(|s| {
                s["end"].as_f64().unwrap_or(0.0) > start && s["start"].as_f64().unwrap_or(0.0) < end
            });
            let limit = args["limit"].as_u64().unwrap_or(200) as usize;
            let page = segments
                .iter()
                .skip(offset as usize)
                .take(limit)
                .cloned()
                .collect::<Vec<_>>();
            return Ok(
                json!({"language":language,"automatic":automatic,"text":page.iter().map(|s|text(s,"text")).collect::<Vec<_>>().join("\n"),"segments":page,"nextOffset":if offset as usize+limit<segments.len(){Some(offset as usize+limit)}else{None}}),
            );
        }
        let info = download_info(self, &cli, &input, cancel).await?;
        Ok(match operation {
            "get_video_info" => {
                let mut result = project(
                    &info,
                    &[
                        "id",
                        "title",
                        "description",
                        "uploader",
                        "channel",
                        "upload_date",
                        "duration",
                        "view_count",
                        "like_count",
                        "is_live",
                    ],
                );
                result["subtitleLanguages"] = json!(languages(&info));
                result["chapters"] = json!(
                    info["chapters"]
                        .as_array()
                        .into_iter()
                        .flatten()
                        .map(|chapter| project(chapter, &["title", "start_time", "end_time"]))
                        .collect::<Vec<_>>()
                );
                result
            }
            "list_video_formats" => {
                json!({"formats":info["formats"].as_array().into_iter().flatten().map(|f|project(f,&["format_id","ext","width","height","fps","vcodec","acodec","language","filesize","filesize_approx","format_note"])).collect::<Vec<_>>()})
            }
            "get_video_comments" => {
                ensure!(
                    info["comments"].is_array(),
                    "Comments are unavailable or unsupported for this site"
                );
                json!({"comments":info["comments"].as_array().into_iter().flatten().take(limit as usize).map(|c|project(c,&["id","text","author","like_count","timestamp","parent","is_pinned"])).collect::<Vec<_>>(),"limit":limit,"available":info["comments"].is_array()})
            }
            _ => {
                let entries = info["entries"]
                    .as_array()
                    .context("No collection/search entries returned")?;
                json!({"title":info["title"],"entries":entries.iter().take(limit as usize).map(|e|{let mut v=project(e,&["id","title","uploader","channel","duration"]);v["url"]=public_entry_url(e);v}).collect::<Vec<_>>(),"nextOffset":if entries.len()>limit as usize{Some(offset+limit)}else{None}})
            }
        })
    }
}

async fn download_info(
    app: &App,
    cli: &[String],
    input: &str,
    cancel: &CancellationToken,
) -> Result<Value> {
    let mut cli = cli.to_vec();
    cli.extend(["--".into(), input.into()]);
    let output = process::run(&app.tool("yt-dlp")?, &cli, None, cancel, None).await?;
    serde_json::from_str(&output.stdout).context("yt-dlp returned invalid metadata")
}
fn project(value: &Value, fields: &[&str]) -> Value {
    Value::Object(
        fields
            .iter()
            .filter_map(|key| {
                value
                    .get(*key)
                    .filter(|value| !value.is_object() && !value.is_array())
                    .map(|value| ((*key).to_string(), value.clone()))
            })
            .collect(),
    )
}
fn youtube_url(value: &str) -> bool {
    url::Url::parse(value).is_ok_and(|url| {
        url.host_str().is_some_and(|host| {
            host == "youtu.be" || host == "youtube.com" || host.ends_with(".youtube.com")
        })
    })
}
fn public_entry_url(entry: &Value) -> Value {
    if text(entry, "ie_key") == "Youtube" {
        let id = text(entry, "id");
        if !id.is_empty()
            && id
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_'))
        {
            return json!(format!("https://www.youtube.com/watch?v={id}"));
        }
    }
    // Only webpage URLs or delegated extractor URLs are public; generic direct media stays private.
    let candidate = if entry["webpage_url"].is_string() {
        text(entry, "webpage_url")
    } else if matches!(text(entry, "_type"), "url" | "url_transparent") {
        text(entry, "url")
    } else {
        return Value::Null;
    };
    let Ok(url) = url::Url::parse(candidate) else {
        return Value::Null;
    };
    if !matches!(url.scheme(), "http" | "https") {
        return Value::Null;
    }
    let mut public = url::Url::parse(&crate::model::redact_url(candidate)).unwrap();
    if url
        .host_str()
        .is_some_and(|host| host == "bilibili.com" || host.ends_with(".bilibili.com"))
    {
        // A part number selects the video; signed and tracking parameters stay private.
        if let Some((_, part)) = url
            .query_pairs()
            .find(|(key, part)| key == "p" && part.parse::<u32>().is_ok_and(|p| p > 0))
        {
            public.query_pairs_mut().append_pair("p", &part);
        }
    }
    json!(public.as_str())
}
fn languages(info: &Value) -> Vec<String> {
    let mut values = std::collections::BTreeSet::new();
    for key in ["subtitles", "automatic_captions"] {
        if let Some(object) = info[key].as_object() {
            values.extend(
                object
                    .iter()
                    .filter(|(_, v)| v.as_array().is_some_and(|a| !a.is_empty()))
                    .map(|(k, _)| k.clone()),
            );
        }
    }
    values.into_iter().collect()
}
fn choose_language(info: &Value, requested: &str, ui: &str) -> Result<String> {
    let available = languages(info);
    ensure!(!available.is_empty(), "No subtitles available");
    if !requested.is_empty() {
        ensure!(
            available.iter().any(|s| s == requested),
            "Requested subtitle language is unavailable"
        );
        return Ok(requested.into());
    }
    for preferred in [if ui.starts_with("zh") { "zh" } else { "en" }, "en"] {
        if let Some(value) = available.iter().find(|s| s.starts_with(preferred)) {
            return Ok(value.clone());
        }
    }
    Ok(available[0].clone())
}
fn parse_json3(source: &str) -> Result<Vec<Value>> {
    let value: Value = serde_json::from_str(source)?;
    Ok(value["events"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|e| {
            let text = e["segs"]
                .as_array()?
                .iter()
                .filter_map(|s| s["utf8"].as_str())
                .collect::<String>();
            let start = e["tStartMs"].as_f64()? / 1000.;
            let duration = e["dDurationMs"].as_f64().unwrap_or(0.) / 1000.;
            (!text.trim().is_empty())
                .then(|| json!({"start":start,"end":start+duration,"text":text.trim()}))
        })
        .collect())
}
fn timestamp(value: &str) -> Option<f64> {
    let mut seconds = 0.;
    for part in value.trim().split(':') {
        seconds = seconds * 60. + part.replace(',', ".").parse::<f64>().ok()?;
    }
    Some(seconds)
}
fn parse_vtt(source: &str) -> Vec<Value> {
    let normalized = source.replace("\r\n", "\n");
    normalized
        .split("\n\n")
        .filter_map(|block| {
            let mut lines = block.lines();
            let timing = lines.find(|l| l.contains(" --> "))?;
            let (a, b) = timing.split_once(" --> ")?;
            let start = timestamp(a)?;
            let end = timestamp(b.split_whitespace().next()?)?;
            let raw = lines.collect::<Vec<_>>().join(" ");
            let mut inside = false;
            let text = raw
                .chars()
                .filter(|c| {
                    if *c == '<' {
                        inside = true;
                        return false;
                    }
                    if *c == '>' {
                        inside = false;
                        return false;
                    }
                    !inside
                })
                .collect::<String>()
                .replace("&amp;", "&")
                .replace("&lt;", "<")
                .replace("&gt;", ">");
            Some(json!({"start":start,"end":end,"text":text}))
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn captions_and_languages() {
        let values = parse_json3(
            r#"{"events":[{"tStartMs":1000,"dDurationMs":500,"segs":[{"utf8":"hello"}]}]}"#,
        )
        .unwrap();
        assert_eq!(values[0]["start"], 1.);
        assert_eq!(
            parse_vtt("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<b>hello</b>\n")[0]["text"],
            "hello"
        );
        let srt = parse_vtt(
            "1\r\n00:00:01,250 --> 00:00:02,500\r\n<b>你好</b>\r\n\r\n2\r\n00:00:03,000 --> 00:00:04,000\r\n字幕\r\n",
        );
        assert_eq!(srt.len(), 2);
        assert_eq!(srt[0], json!({"start":1.25,"end":2.5,"text":"你好"}));
        assert_eq!(
            choose_language(&json!({"subtitles":{"zh-CN":[{}],"en":[{}]}}), "", "zh-CN").unwrap(),
            "zh-CN"
        );
    }
    #[test]
    fn public_collection_urls_keep_part_numbers_but_remove_secrets() {
        for part in [1, 2] {
            let entry = json!({"_type":"url","url":format!("https://www.bilibili.com/video/BV1xx?p={part}&token=secret#private")});
            assert_eq!(
                public_entry_url(&entry),
                format!("https://www.bilibili.com/video/BV1xx?p={part}")
            );
        }
        assert_eq!(
            public_entry_url(&json!({"url":"https://cdn.test/media?token=secret"})),
            Value::Null
        );
        assert!(youtube_url("https://www.youtube.com/watch?v=test"));
        assert!(youtube_url("https://youtu.be/test"));
        assert!(!youtube_url("https://youtube.com.evil.test/test"));
        assert!(!youtube_url("https://www.bilibili.com/video/BV1xx"));
    }
}

//! Bounded yt-dlp queries shared by the desktop MCP adapter.
use crate::{
    App, DownloadOptions,
    download::{Cookies, base_args},
    model::{text, validate_url},
    process,
};
use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{sync::Mutex, time::Duration};
use tokio_util::sync::CancellationToken;

mod cache;
mod engine;
#[cfg(test)]
mod native_tests;

#[derive(Default)]
pub(crate) struct ResearchState {
    cache: Mutex<cache::Cache>,
    engine: Mutex<Option<engine::Engine>>,
}

impl App {
    pub fn resolve_research_url(&self, args: &Value) -> Result<String> {
        for key in ["url", "sourceRef"] {
            if let Some(value) = args.get(key) {
                ensure!(
                    value.as_str().is_some_and(
                        |value| !value.is_empty() && !value.chars().any(char::is_control)
                    ),
                    "Source URL and reference must be nonempty text"
                );
            }
        }
        ensure!(
            args["sourceRef"]
                .as_str()
                .is_none_or(|value| value.len() <= 128),
            "Source reference is too long"
        );
        let url = args
            .get("url")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty());
        let reference = args
            .get("sourceRef")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty());
        ensure!(
            url.is_some() != reference.is_some(),
            "Provide exactly one of url or sourceRef"
        );
        let url = match (url, reference) {
            (Some(url), None) => url.to_owned(),
            (None, Some(reference)) => self
                .research_state
                .cache
                .lock()
                .unwrap()
                .resolve(reference)?,
            _ => unreachable!(),
        };
        validate_url(&url)?;
        Ok(url)
    }

    fn remember_research_source(&self, url: &str) -> String {
        self.research_state.cache.lock().unwrap().remember(url)
    }

    async fn engine_result(
        &self,
        operation: &str,
        args: &Value,
        cancel: &CancellationToken,
    ) -> Result<Value> {
        if matches!(operation, "get_ytdlp_status" | "get_engine_status") {
            let mut tools = Vec::new();
            for name in ["yt-dlp", "ffmpeg", "ffprobe", "deno", "qjs"] {
                let tool = match self.tool(name) {
                    Ok(path) => match process::run_limited(
                        &path,
                        &[if matches!(name, "ffmpeg" | "ffprobe") {
                            "-version".into()
                        } else {
                            "--version".into()
                        }],
                        None,
                        cancel,
                        256 * 1024,
                        Duration::from_secs(20),
                    )
                    .await
                    {
                        Ok(output) => {
                            json!({"name":name,"available":true,"version":output.stdout.lines().next().unwrap_or("")})
                        }
                        Err(error) => {
                            json!({"name":name,"available":false,"error":self.research_error(&format!("{error:#}"))})
                        }
                    },
                    Err(_) => json!({"name":name,"available":false}),
                };
                tools.push(tool);
            }
            return match self.research_engine(cancel).await {
                Ok(engine) => Ok(
                    json!({"available":true,"version":engine.version,"extractorCount":engine.extractors.len(),"searchProviderCount":engine.providers.len(),"tools":tools,"runtimes":tools,"siteSupport":"Extractor listings are indicative; actual URL extraction is the reliable support check","capabilities":["metadata","formats","search","collections","subtitles","comments","live","download","postprocessing"]}),
                ),
                Err(error) => Ok(
                    json!({"available":false,"tools":tools,"runtimes":tools,"error":self.research_error(&format!("{error:#}"))}),
                ),
            };
        }
        let engine = self.research_engine(cancel).await?;
        let offset = args["offset"].as_u64().unwrap_or(0) as usize;
        let limit = args["limit"].as_u64().unwrap_or(100) as usize;
        ensure!(
            offset <= 10000 && (1..=100).contains(&limit),
            "Query page exceeds its limits"
        );
        let query = text(args, "query").to_lowercase();
        let values = if operation == "list_extractors" {
            engine.extractors
        } else {
            engine.providers
        };
        let filtered = values
            .into_iter()
            .filter(|value| query.is_empty() || value.to_string().to_lowercase().contains(&query))
            .collect::<Vec<_>>();
        let key = if operation == "list_extractors" {
            "extractors"
        } else {
            "providers"
        };
        let mut result = json!({"version":engine.version,"total":filtered.len(),"nextOffset":if offset+limit<filtered.len(){Some(offset+limit)}else{None}});
        result[key] = json!(
            filtered
                .into_iter()
                .skip(offset)
                .take(limit)
                .collect::<Vec<_>>()
        );
        Ok(result)
    }

    pub fn mcp_settings(&self) -> Result<Value> {
        let mut value = self.db.settings()?["mcp"].clone();
        if !value.is_object() {
            value = json!({"enabled":false,"downloads":false,"port":19492});
        }
        if !value["autoStart"].is_boolean() {
            value["autoStart"] = json!(value["enabled"] == true);
        }
        Ok(value)
    }
    pub fn mcp_save_settings(&self, config: Value) -> Result<()> {
        self.db.save_mcp_settings(&config)?;
        self.emit("settings.updated", self.public_settings()?);
        Ok(())
    }
    pub fn research_error(&self, error: &str) -> String {
        let mut message = process::redact(error);
        for path in self.download_roots().into_iter().chain(
            [
                Some(self.config.data_dir.clone()),
                self.config.tool_dir.clone(),
            ]
            .into_iter()
            .flatten(),
        ) {
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
                        "kind",
                        "title",
                        "status",
                        "stage",
                        "live",
                        "priority",
                        "progress",
                        "speed",
                        "eta",
                        "size",
                        "sourceInfo",
                        "mediaInfo",
                        "error",
                        "warnings",
                        "createdAt",
                    ],
                );
                if let Some(error) = value["error"].as_str() {
                    value["error"] = json!(self.research_error(error));
                }
                value["warnings"] = json!(
                    task.warnings
                        .iter()
                        .map(|warning| self.research_error(warning))
                        .collect::<Vec<_>>()
                );
                value["files"] = public["files"].clone();
                value["destinationConflict"] = public["destinationConflict"].clone();
                value
            })
            .collect())
    }
    pub fn research_media(
        &self,
        id: Option<&str>,
        offset: usize,
        limit: usize,
        query: &str,
    ) -> Result<Value> {
        if let Some(id) = id {
            return Ok(self
                .db
                .media()?
                .into_iter()
                .find(|media| media.id == id)
                .context("Media not found")?
                .public());
        }
        let query = query.to_lowercase();
        let media: Vec<_> = self
            .db
            .media()?
            .into_iter()
            .filter(|media| {
                query.is_empty()
                    || format!("{} {}", media.title, media.filename)
                        .to_lowercase()
                        .contains(&query)
            })
            .collect();
        Ok(
            json!({"media":media.iter().rev().skip(offset).take(limit).map(crate::model::Media::public).collect::<Vec<_>>(),"nextOffset":if offset+limit<media.len(){Some(offset+limit)}else{None}}),
        )
    }
    pub async fn research_convert_media(&self, id: &str, encoder: &str) -> Result<Value> {
        let media = self
            .db
            .media()?
            .into_iter()
            .find(|media| media.id == id)
            .context("Media not found")?;
        self.command(
            "tasks.convert",
            json!({"id":media.task_id,"encoder":encoder}),
        )
        .await
    }
    pub async fn research_retry_processing(&self, id: &str) -> Result<Value> {
        let task = self.db.task(id)?;
        ensure!(
            task.status == "processing_failed" && task.manifest.is_some(),
            "Only a failed processing task with saved source files can be retried"
        );
        self.command("tasks.retry", json!({"id":id})).await
    }
    pub async fn research(
        &self,
        operation: &str,
        args: &Value,
        cancel: &CancellationToken,
    ) -> Result<Value> {
        if matches!(
            operation,
            "get_ytdlp_status" | "get_engine_status" | "list_extractors" | "list_search_providers"
        ) {
            return self.engine_result(operation, args, cancel).await;
        }
        ensure!(
            matches!(
                operation,
                "search_videos"
                    | "inspect_url"
                    | "preview_download"
                    | "get_video_info"
                    | "get_video_chapters"
                    | "list_subtitle_languages"
                    | "get_channel_info"
                    | "list_video_thumbnails"
                    | "get_video_transcript"
                    | "list_video_formats"
                    | "get_video_comments"
                    | "list_collection_items"
            ),
            "Unknown media query"
        );
        let mut options = DownloadOptions::parse(
            if matches!(operation, "preview_download" | "list_collection_items") {
                args.get("options")
                    .cloned()
                    .unwrap_or(self.research_options()?)
            } else {
                self.research_options()?
            },
        )?;
        options.proxy = self
            .credentials
            .secret("downloadProxy")?
            .unwrap_or_default();
        let offset = args["offset"].as_u64().unwrap_or(0);
        let limit = args["limit"]
            .as_u64()
            .unwrap_or(if operation == "search_videos" {
                10
            } else if operation == "get_video_transcript" {
                200
            } else {
                20
            });
        ensure!(
            offset <= 10000
                && limit > 0
                && limit
                    <= if operation == "get_video_transcript" {
                        1000
                    } else {
                        100
                    },
            "Query page exceeds its limits"
        );
        let provider = if operation == "search_videos" {
            let engine = self.research_engine(cancel).await?;
            Some(engine::search_provider(&engine.providers, text(args, "provider"))?.clone())
        } else {
            None
        };
        let url = if let Some(provider) = &provider {
            engine::search_home(provider).to_owned()
        } else {
            self.resolve_research_url(args)?
        };
        let cookie = if url.is_empty() {
            None
        } else {
            self.credential_snapshot(&options, &url)?
        };
        let comment_key = format!(
            "{}:{}:{:x}",
            url,
            text(args, "sort"),
            Sha256::digest(cookie.as_deref().unwrap_or_default().as_bytes())
        );
        if operation == "get_video_comments" {
            validate_comment_sort(&url, text(args, "sort"))?;
            if let Some(cached) = self
                .research_state
                .cache
                .lock()
                .unwrap()
                .comments(&comment_key)
            {
                return Ok(comment_page(&cached, offset, limit, true));
            }
        }
        let cookies = Cookies::new(self, cookie.as_deref())?;
        let _permit = tokio::select! { p=self.metadata.acquire()=>p?, _=cancel.cancelled()=>anyhow::bail!("Query cancelled") };
        let mut cli = base_args(self, &options, &cookies, None).await?;
        cli.extend([
            "--simulate".into(),
            "--skip-download".into(),
            "--dump-single-json".into(),
        ]);
        if matches!(
            operation,
            "get_video_info"
                | "inspect_url"
                | "preview_download"
                | "get_video_transcript"
                | "list_subtitle_languages"
        ) {
            // Some extractors discover tracks only when subtitle extraction is requested.
            cli.extend(["--write-subs".into(), "--write-auto-subs".into()]);
        }
        let input = if operation == "search_videos" {
            let query = text(args, "query");
            ensure!(
                !query.trim().is_empty() && query.len() <= 1000,
                "Enter a search query of at most 1000 bytes"
            );
            cli.push("--flat-playlist".into());
            format!(
                "{}{}:{query}",
                text(provider.as_ref().unwrap(), "searchPrefix"),
                offset + limit + 1
            )
        } else {
            if matches!(operation, "get_channel_info" | "inspect_url") {
                cli.extend([
                    "--flat-playlist".into(),
                    "--playlist-items".into(),
                    "1".into(),
                ]);
                if operation == "inspect_url" {
                    cli.push("--no-playlist".into());
                }
            } else if operation == "list_collection_items" {
                cli.extend([
                    "--flat-playlist".into(),
                    "--playlist-items".into(),
                    format!("{}:{}", offset + 1, offset + limit + 1),
                ]);
            } else {
                cli.push("--no-playlist".into());
            }
            if operation == "get_video_comments" {
                cli.push("--write-comments".into());
                if youtube_url(&url) {
                    cli.extend([
                        "--extractor-args".into(),
                        format!(
                            "youtube:max_comments=10000;comment_sort={}",
                            if matches!(text(args, "sort"), "top") {
                                "top"
                            } else {
                                "new"
                            }
                        ),
                    ]);
                }
            }
            url.clone()
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
                "json3/vtt/srt/ttml/ass/best".into(),
                "--convert-subs".into(),
                "vtt".into(),
                "--ffmpeg-location".into(),
                self.tool("ffmpeg")?.to_string_lossy().into_owned(),
                "-o".into(),
                temp.path()
                    .join("subtitle.%(ext)s")
                    .to_string_lossy()
                    .into_owned(),
                "--".into(),
                input,
            ]);
            process::run_limited(
                &self.tool("yt-dlp")?,
                &sub_cli,
                Some(temp.path()),
                cancel,
                32 * 1024 * 1024,
                Duration::from_secs(120),
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
                json!({"language":language,"automatic":automatic,"sourceRef":self.remember_research_source(&url),"text":page.iter().map(|s|text(s,"text")).collect::<Vec<_>>().join("\n"),"segments":page,"nextOffset":if offset as usize+limit<segments.len(){Some(offset as usize+limit)}else{None}}),
            );
        }
        let info = download_info(self, &cli, &input, cancel).await?;
        if operation == "get_video_comments" {
            ensure!(
                info["comments"].is_array(),
                "Comments were not provided by this site's extractor for this media"
            );
            let comments = info["comments"]
                .as_array()
                .unwrap()
                .iter()
                .map(|comment| {
                    project(
                        comment,
                        &[
                            "id",
                            "text",
                            "author",
                            "like_count",
                            "timestamp",
                            "parent",
                            "is_pinned",
                        ],
                    )
                })
                .collect::<Vec<_>>();
            let cached = json!({"sourceRef":self.remember_research_source(&url),"comments":comments,"mode":if youtube_url(&url){"native-limit"}else{"extractor-result"},"sorting":if youtube_url(&url){if text(args,"sort")=="top"{"top"}else{"newest"}}else{"native"},"upstreamLimit":if youtube_url(&url){Some(10000)}else{None},"possiblyTruncated":youtube_url(&url) && comments.len()>=10000,"note":"Pagination is over the cached extractor result; limits do not bound subprocess memory"});
            self.research_state
                .cache
                .lock()
                .unwrap()
                .save_comments(comment_key, cached.clone())?;
            return Ok(comment_page(&cached, offset, limit, false));
        }
        if operation == "list_collection_items" && args["options"].is_object() {
            return self.collection_result(&info, &options, offset, limit);
        }
        let mut result = if operation == "preview_download" {
            preview(&info, &options)?
        } else if operation == "inspect_url" {
            let mut value = metadata(&info);
            value["kind"] = json!(if info["entries"].is_array()
                || matches!(text(&info, "_type"), "playlist" | "multi_video")
            {
                "collection"
            } else {
                "video"
            });
            value
        } else {
            research_result(operation, &info, offset, limit)?
        };
        if let Some(provider) = provider {
            result["provider"] = json!(text(&provider, "id"));
        }
        if let Some(entries) = result["entries"].as_array_mut() {
            let source_entries = info["entries"].as_array().unwrap();
            let start = if operation == "search_videos" {
                offset as usize
            } else {
                0
            };
            for (entry, source) in entries.iter_mut().zip(source_entries.iter().skip(start)) {
                if let Some(source) = source_page_url(source) {
                    entry["sourceRef"] = json!(self.remember_research_source(&source));
                }
            }
        } else if !url.is_empty() {
            let source =
                source_page_url(&info).or_else(|| (!direct_media_url(&url)).then(|| url.clone()));
            if let Some(source) = source {
                result["sourceRef"] = json!(self.remember_research_source(&source));
            }
        }
        Ok(result)
    }

    fn collection_result(
        &self,
        info: &Value,
        options: &DownloadOptions,
        offset: u64,
        limit: u64,
    ) -> Result<Value> {
        let entries = info["entries"]
            .as_array()
            .context("No collection entries returned")?;
        let mut selected = Vec::new();
        for (index, source) in entries.iter().take(limit as usize).enumerate() {
            let position = offset as usize + index + 1;
            if options.first_n > 0 && position > options.first_n
                || !playlist_position(&options.playlist_items, position)?
                || !options.matches(source)
            {
                continue;
            }
            let mut entry = metadata(source);
            entry["url"] = public_entry_url(source);
            if let Some(url) = source_page_url(source) {
                entry["sourceRef"] = json!(self.remember_research_source(&url));
            }
            selected.push(entry);
        }
        match options.collection_order.as_str() {
            "reverse" => selected.reverse(),
            "random" => {
                use rand::seq::SliceRandom;
                selected.shuffle(&mut rand::rng());
            }
            _ => {}
        }
        Ok(
            json!({"title":info["title"],"entries":selected,"nextOffset":if entries.len()>limit as usize{Some(offset+limit)}else{None},"selectionScope":"range-local","sourceOffset":offset,"sourceLimit":limit,"note":"Filters and ordering apply within the requested source window; missing metadata cannot be used to enforce a filter until the task probes that media"}),
        )
    }
}

fn playlist_position(expression: &str, position: usize) -> Result<bool> {
    if expression.is_empty() {
        return Ok(true);
    }
    let mut matched = false;
    for term in expression.split(',') {
        let normalized = if !term.starts_with('-') && !term.contains(':') {
            term.replace('-', ":")
        } else {
            term.to_owned()
        };
        let pieces = normalized.split(':').collect::<Vec<_>>();
        if pieces.len() == 1 {
            let number = term.parse::<usize>().context(
                "Collection item indices must be positive numbers or start:end:step ranges",
            )?;
            ensure!(number > 0, "Collection item indices must be positive");
            matched |= number == position;
        } else {
            ensure!(pieces.len() <= 3, "Invalid collection item range");
            let start = if pieces[0].is_empty() {
                1
            } else {
                pieces[0]
                    .parse::<usize>()
                    .context("Collection range start must be positive")?
            };
            let end = if pieces[1].is_empty() {
                usize::MAX
            } else {
                pieces[1]
                    .parse::<usize>()
                    .context("Collection range end must be positive")?
            };
            let step = if pieces.len() < 3 || pieces[2].is_empty() {
                1
            } else {
                pieces[2]
                    .parse::<usize>()
                    .context("Collection range step must be positive")?
            };
            ensure!(
                start > 0 && end >= start && step > 0,
                "Collection ranges must ascend with a positive step"
            );
            matched |=
                position >= start && position <= end && (position - start).is_multiple_of(step);
        }
    }
    Ok(matched)
}

fn research_result(operation: &str, info: &Value, offset: u64, limit: u64) -> Result<Value> {
    Ok(match operation {
        "get_video_chapters" => {
            let chapters = info["chapters"]
                .as_array()
                .map(Vec::as_slice)
                .unwrap_or(&[]);
            json!({"chapters":chapters.iter().skip(offset as usize).take(limit as usize).map(|chapter| project(chapter, &["title", "start_time", "end_time"])).collect::<Vec<_>>(),"nextOffset":if offset+limit < chapters.len() as u64 {Some(offset+limit)} else {None}})
        }
        "list_subtitle_languages" => {
            let languages = languages(info);
            json!({"languages":languages.iter().skip(offset as usize).take(limit as usize).map(|language| json!({"language":language,"manual":info["subtitles"][language].as_array().is_some_and(|tracks| !tracks.is_empty()),"automatic":info["automatic_captions"][language].as_array().is_some_and(|tracks| !tracks.is_empty())})).collect::<Vec<_>>(),"nextOffset":if offset+limit < languages.len() as u64 {Some(offset+limit)} else {None}})
        }
        "get_channel_info" => metadata(info),
        "list_video_thumbnails" => {
            json!({"thumbnails":info["thumbnails"].as_array().into_iter().flatten().take(100).map(|thumbnail| {
                let mut value = project(thumbnail, &["id", "width", "height", "resolution", "preference"]);
                if let Some(url) = thumbnail["url"].as_str().filter(|url| reqwest::Url::parse(url).is_ok_and(|url| matches!(url.scheme(),"http"|"https") && url.username().is_empty() && url.password().is_none() && url.query().is_none() && url.fragment().is_none())) { value["url"] = json!(url); }
                value
            }).collect::<Vec<_>>()})
        }
        "get_video_info" => {
            let mut result = metadata(info);
            result["subtitleLanguages"] = json!(languages(info));
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
            let start = if operation == "search_videos" {
                offset as usize
            } else {
                0
            };
            json!({"title":info["title"],"entries":entries.iter().skip(start).take(limit as usize).map(|e|{let mut v=metadata(e);v["url"]=public_entry_url(e);v}).collect::<Vec<_>>(),"nextOffset":if entries.len()>start+limit as usize{Some(offset+limit)}else{None}})
        }
    })
}

async fn download_info(
    app: &App,
    cli: &[String],
    input: &str,
    cancel: &CancellationToken,
) -> Result<Value> {
    let mut cli = cli.to_vec();
    cli.extend(["--".into(), input.into()]);
    let executable = app.tool("yt-dlp")?;
    let output = tokio::select! {
        result=process::run_limited(&executable, &cli, None, cancel, 32 * 1024 * 1024, Duration::from_secs(120))=>result?,
        _=app.shutdown_token.cancelled()=>anyhow::bail!("Query cancelled"),
    };
    serde_json::from_str(&output.stdout).context("yt-dlp returned invalid metadata")
}

fn metadata(info: &Value) -> Value {
    let mut value = project(
        info,
        &[
            "id",
            "title",
            "description",
            "extractor",
            "extractor_key",
            "uploader",
            "uploader_id",
            "channel",
            "channel_id",
            "channel_follower_count",
            "upload_date",
            "release_date",
            "duration",
            "view_count",
            "like_count",
            "comment_count",
            "is_live",
            "was_live",
            "live_status",
            "availability",
            "age_limit",
            "playlist_count",
            "series",
            "season",
            "season_number",
            "episode",
            "episode_number",
            "track",
            "album",
            "media_type",
        ],
    );
    for field in ["tags", "categories", "artists", "creators", "genres"] {
        if let Some(values) = info[field].as_array() {
            value[field] = json!(
                values
                    .iter()
                    .filter(|value| value.is_string())
                    .take(100)
                    .collect::<Vec<_>>()
            );
        }
    }
    if let Some(url) = source_page_url(info) {
        value["webpageUrl"] = json!(crate::model::redact_url(&url));
    }
    value["observed"] = json!({"formats":info["formats"].as_array().is_some_and(|v|!v.is_empty()),"subtitles":!languages(info).is_empty(),"chapters":info["chapters"].as_array().is_some_and(|v|!v.is_empty()),"comments":if info["comments"].is_array(){json!(true)}else{Value::Null},"live":info.get("live_status").cloned().unwrap_or(Value::Null)});
    value["capabilityNote"] = json!(
        "Observed data applies to this response; missing data does not establish that the site never supports a feature"
    );
    value["capabilities"] = value["observed"].clone();
    value
}

fn preview(info: &Value, options: &DownloadOptions) -> Result<Value> {
    ensure!(
        !info["entries"].is_array(),
        "Preview requires a single media item; list collection items first"
    );
    let selected = crate::download::select_formats(info, options)?;
    let mut warnings = Vec::new();
    let eligible = options.matches(info);
    if !eligible {
        warnings.push(
            "This media does not match the current date, duration or live filters".to_owned(),
        );
    }
    if !options.languages().is_empty() && languages(info).is_empty() {
        warnings.push(
            "The extractor did not provide subtitles for this media; video download can continue"
                .to_owned(),
        );
    }
    let extractor = text(info, "extractor").to_ascii_lowercase();
    if options.live_from_start
        && !["youtube", "twitch", "tver", "mellowfan", "mellow-fan"]
            .iter()
            .any(|site| extractor.contains(site))
    {
        warnings.push("Downloading a live stream from its start is experimental and may not be supported by this extractor".to_owned());
    }
    let mut value = metadata(info);
    value["eligible"] = json!(eligible);
    value["selectedFormats"] = json!(
        selected
            .iter()
            .map(|format| project(
                format,
                &[
                    "format_id",
                    "ext",
                    "width",
                    "height",
                    "fps",
                    "vcodec",
                    "acodec",
                    "language",
                    "filesize",
                    "filesize_approx",
                    "format_note"
                ]
            ))
            .collect::<Vec<_>>()
    );
    value["warnings"] = json!(warnings);
    value["options"] = project(
        &serde_json::to_value(options)?,
        &[
            "quality",
            "contentMode",
            "audioFormat",
            "audioTrack",
            "container",
            "videoFps",
            "sourceVideoCodec",
            "compatibilityTarget",
            "liveFromStart",
            "sectionStart",
            "sectionEnd",
            "splitChapters",
            "subtitleLanguage",
            "subtitleFormat",
            "automaticSubtitles",
            "skipUnavailableSubtitles",
            "embedSubtitles",
            "writeThumbnail",
            "writeDescription",
            "writeComments",
            "writeInfoJson",
            "embedMetadata",
            "embedChapters",
            "embedThumbnail",
            "rateLimit",
            "encoder",
            "fragmentConcurrent",
        ],
    );
    value["subtitleLanguages"] = json!(options.languages());
    value["createsTask"] = json!(false);
    value["formats"] = value["selectedFormats"].clone();
    value["plan"] = value["options"].clone();
    Ok(value)
}

fn validate_comment_sort(url: &str, sort: &str) -> Result<()> {
    ensure!(
        matches!(sort, "" | "native" | "default" | "top" | "newest"),
        "Unsupported comment sort mode"
    );
    ensure!(
        youtube_url(url) || matches!(sort, "" | "native" | "default"),
        "This extractor does not provide the requested comment ordering; use native sorting"
    );
    Ok(())
}

fn comment_page(cached: &Value, offset: u64, limit: u64, hit: bool) -> Value {
    let comments = cached["comments"].as_array().unwrap();
    let mut result = cached.clone();
    result["comments"] = json!(
        comments
            .iter()
            .skip(offset as usize)
            .take(limit as usize)
            .collect::<Vec<_>>()
    );
    result["nextOffset"] = if offset + limit < comments.len() as u64 {
        json!(offset + limit)
    } else {
        Value::Null
    };
    result["fetchedCount"] = json!(comments.len());
    result["limit"] = json!(limit);
    result["available"] = json!(true);
    result["cached"] = json!(hit);
    result
}

fn direct_media_url(value: &str) -> bool {
    url::Url::parse(value).is_ok_and(|url| {
        let path = url.path().to_ascii_lowercase();
        [
            ".mp4", ".webm", ".mkv", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wav",
            ".m3u8", ".mpd", ".m4s", ".ts", ".f4m",
        ]
        .iter()
        .any(|ext| path.ends_with(ext))
    })
}

fn source_page_url(entry: &Value) -> Option<String> {
    if entry["direct"].as_bool() == Some(true) {
        return None;
    }
    if text(entry, "ie_key") == "Youtube" {
        let id = text(entry, "id");
        if !id.is_empty()
            && id
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_'))
        {
            return Some(format!("https://www.youtube.com/watch?v={id}"));
        }
    }
    let candidate = if entry["webpage_url"].is_string() {
        text(entry, "webpage_url")
    } else if matches!(text(entry, "_type"), "url" | "url_transparent") {
        text(entry, "url")
    } else {
        return None;
    };
    if validate_url(candidate).is_err() || direct_media_url(candidate) {
        return None;
    }
    Some(candidate.to_owned())
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
    if source_page_url(entry).is_none() {
        return Value::Null;
    }
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
                    .filter(|(language, v)| {
                        language.as_str() != "live_chat"
                            && v.as_array().is_some_and(|a| !a.is_empty())
                    })
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
    #[tokio::test]
    async fn task_recovery_projection_keeps_conflicts_public_without_paths_or_source_urls()
    -> Result<()> {
        let root = tempfile::tempdir()?;
        let app = App::open(crate::AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("downloads"),
            tool_dir: None,
        })
        .await?;
        let mut task = crate::tests::paused_task(Default::default());
        task.url = "https://example.test/watch?token=private-source".into();
        task.priority = 42;
        task.live = true;
        task.warnings.push(format!(
            "Completed cache cleanup did not finish: Cannot validate owned path {} https://warning.test/media?token=private-warning",
            app.config.data_dir.join("tasks").display()
        ));
        task.warnings.push(format!(
            "Previous result cleanup did not finish: {}",
            app.config.download_dir.join("old.mp4").display()
        ));
        let original_warnings = task.warnings.clone();
        let target = app.config.download_dir.join("Video.mp4");
        task.destination = Some(crate::destination::Destination {
            target: target.clone(),
            occupied: vec![crate::destination::Occupied {
                path: target,
                hash: None,
                size: 10,
                owners: vec![],
            }],
            confirmed: false,
        });
        app.db.insert_task(&task)?;
        let tasks = app.research_tasks()?;
        assert_eq!(tasks[0]["priority"], 42);
        assert_eq!(tasks[0]["live"], true);
        assert!(
            tasks[0]["warnings"][0]
                .as_str()
                .unwrap()
                .contains("[application storage]")
        );
        assert_eq!(app.db.task(&task.id)?.warnings, original_warnings);
        assert_eq!(
            tasks[0]["destinationConflict"],
            json!({"filename":"Video.mp4","files":["Video.mp4"]})
        );
        let output = serde_json::to_string(&tasks)?;
        assert!(
            !output.contains("private-source")
                && !output.contains("https://example.test")
                && !output.contains("downloads")
                && !output.contains("outputPath")
                && !output.contains("private-warning")
                && !output.contains(root.path().to_string_lossy().as_ref())
        );
        app.shutdown().await?;
        Ok(())
    }
    #[test]
    fn preview_uses_the_real_selector_without_exporting_stream_urls_or_private_options()
    -> Result<()> {
        let info = json!({"id":"audio1","title":"Audio","extractor":"soundcloud","track":"Track","album":"Album","artists":["Artist"],"url":"https://cdn.test/signed?token=private","formats":[{"format_id":"low","url":"https://cdn.test/a?token=private","acodec":"aac","vcodec":"none","abr":64},{"format_id":"high","url":"https://cdn.test/b?token=private","acodec":"aac","vcodec":"none","abr":256}],"subtitles":{"en":[{"url":"https://private.test/sub?token=private"}]}});
        let options = DownloadOptions::parse(
            json!({"contentMode":"audio","proxy":"http://proxy.test","processingTempDir":"C:/private/path"}),
        )?;
        let value = preview(&info, &options)?;
        assert_eq!(value["selectedFormats"][0]["format_id"], "high");
        assert_eq!(value["track"], "Track");
        assert_eq!(value["album"], "Album");
        assert_eq!(value["createsTask"], false);
        assert!(!value.to_string().contains("private"));
        assert!(!value.to_string().contains("proxy.test"));
        assert!(!value.to_string().contains("cdn.test"));
        assert_eq!(value["observed"]["comments"], Value::Null);
        Ok(())
    }
    #[test]
    fn source_references_keep_queries_only_inside_the_core() {
        let entry = json!({"_type":"url","ie_key":"Vimeo","url":"https://vimeo.com/42?unlisted_hash=private#section"});
        assert_eq!(
            source_page_url(&entry).as_deref(),
            Some("https://vimeo.com/42?unlisted_hash=private#section")
        );
        assert!(!public_entry_url(&entry).to_string().contains("private"));
        assert!(
            source_page_url(&json!({"webpage_url":"https://cdn.test/media.m3u8?token=private"}))
                .is_none()
        );
        assert!(
            source_page_url(
                &json!({"direct":true,"webpage_url":"https://cdn.test/signed?token=private"})
            )
            .is_none()
        );
        assert!(source_page_url(&json!({"url":"https://cdn.test/signed?token=private"})).is_none());
    }
    #[test]
    fn comments_use_cached_pages_and_do_not_claim_cross_site_sorting() -> Result<()> {
        let cached = json!({"comments":[{"id":"1"},{"id":"2"},{"id":"3"}],"mode":"extractor-result","sorting":"native"});
        let page = comment_page(&cached, 1, 1, true);
        assert_eq!(page["comments"], json!([{"id":"2"}]));
        assert_eq!(page["nextOffset"], 2);
        assert_eq!(page["fetchedCount"], 3);
        assert_eq!(page["cached"], true);
        validate_comment_sort("https://vimeo.com/42", "native")?;
        assert!(validate_comment_sort("https://vimeo.com/42", "top").is_err());
        validate_comment_sort("https://youtu.be/123", "top")?;
        Ok(())
    }
    #[test]
    fn collection_filters_only_select_positive_source_positions() -> Result<()> {
        for index in [1, 3, 5, 7, 8] {
            assert!(playlist_position("1,3:5:2,7-8", index)?);
        }
        for index in [2, 4, 6, 9] {
            assert!(!playlist_position("1,3:5:2,7-8", index)?);
        }
        for invalid in ["-1", "1,-2", "1:5:0", "5:1", "1,", "0"] {
            assert!(playlist_position(invalid, 1).is_err(), "{invalid}");
        }
        Ok(())
    }
    #[tokio::test]
    async fn source_resolution_is_per_app_and_requires_exactly_one_input() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let app = App::open(crate::AppConfig {
            data_dir: temp.path().join("data"),
            download_dir: temp.path().join("downloads"),
            tool_dir: None,
        })
        .await?;
        let reference =
            app.remember_research_source("https://example.test/item?id=42&token=private");
        assert_eq!(
            app.resolve_research_url(&json!({"sourceRef":reference}))?,
            "https://example.test/item?id=42&token=private"
        );
        assert!(
            app.resolve_research_url(
                &json!({"sourceRef":reference,"url":"https://example.test/item"})
            )
            .is_err()
        );
        assert!(app.resolve_research_url(&json!({})).is_err());
        assert!(
            app.resolve_research_url(&json!({"sourceRef":"unknown"}))
                .is_err()
        );
        assert!(
            app.resolve_research_url(&json!({"url":"https://user:secret@example.test"}))
                .is_err()
        );
        app.shutdown().await?;
        Ok(())
    }
    #[test]
    fn extra_metadata_is_bounded_and_never_exposes_signed_streams_or_subtitle_urls() -> Result<()> {
        let info = json!({"id":"channel-id","title":"Channel","url":"https://stream.invalid/private?token=secret","entries":[{"private":"never-return"}],"chapters":[{"title":"First","start_time":0,"end_time":2,"url":"secret"},{"title":"Second","start_time":2,"end_time":3}],"subtitles":{"en":[{"url":"secret"}]},"automatic_captions":{"en":[{"url":"secret"}],"ja":[{}],"live_chat":[{}]},"thumbnails":[{"id":"public","url":"https://example.com/cover.jpg","width":100},{"id":"signed","url":"https://example.com/cover.jpg?token=secret"}]});
        let chapters = research_result("get_video_chapters", &info, 0, 1)?;
        assert_eq!(chapters["chapters"].as_array().unwrap().len(), 1);
        assert_eq!(chapters["nextOffset"], 1);
        let languages = research_result("list_subtitle_languages", &info, 0, 1)?;
        assert_eq!(
            languages["languages"][0],
            json!({"language":"en","manual":true,"automatic":true})
        );
        assert_eq!(languages["nextOffset"], 1);
        for operation in [
            "get_video_chapters",
            "list_subtitle_languages",
            "get_channel_info",
            "list_video_thumbnails",
        ] {
            let value = research_result(operation, &info, 0, 100)?;
            assert!(!value.to_string().contains("secret"));
            assert!(!value.to_string().contains("never-return"));
        }
        assert_eq!(
            research_result("list_video_thumbnails", &info, 0, 100)?["thumbnails"][0]["url"],
            "https://example.com/cover.jpg"
        );
        Ok(())
    }
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

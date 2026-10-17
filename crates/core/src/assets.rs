use crate::{
    App, DownloadOptions,
    model::{Task, text, validate_url},
};
use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};

impl App {
    pub async fn research_download_assets(&self, args: &Value) -> Result<Value> {
        let _adding = self.task_add_lock.lock().await;
        let url = text(args, "url");
        validate_url(url)?;
        let mut options =
            DownloadOptions::parse(args.get("options").cloned().unwrap_or(json!({})))?;
        let assets = args["assets"]
            .as_array()
            .context("assets must be an array")?;
        ensure!(
            !assets.is_empty() && assets.len() <= 4,
            "Choose between 1 and 4 asset types"
        );
        let mut requested = std::collections::BTreeSet::new();
        for asset in assets {
            let asset = asset.as_str().context("Asset type must be text")?;
            ensure!(
                matches!(asset, "subtitles" | "thumbnail" | "description" | "info"),
                "Unsupported asset type: {asset}"
            );
            requested.insert(asset);
        }
        options.collection_mode = "single".into();
        options.write_thumbnail = requested.contains("thumbnail");
        options.write_description = requested.contains("description");
        options.write_info_json = requested.contains("info");
        options.write_comments = false;
        options.embed_subtitles = false;
        options.embed_thumbnail = false;
        options.embed_metadata = false;
        options.embed_chapters = false;
        options.prepend_cover_enabled = false;
        options.sponsorblock_mode = "off".into();
        options.split_chapters = false;
        if requested.contains("subtitles") {
            if options.languages().is_empty() {
                options.subtitle_language = "all".into();
            }
        } else {
            options.subtitle_language = "none".into();
            options.subtitle_languages.clear();
        }
        // Download credentials and runtimes are frozen before the task enters the queue.
        options.proxy = self
            .credentials
            .secret("downloadProxy")?
            .unwrap_or_default();
        let key = crate::download_key(url)?;
        let matches = self
            .db
            .tasks()?
            .into_iter()
            .filter(|task| {
                task.kind == "assets" && crate::download_key(&task.url).ok().as_ref() == Some(&key)
            })
            .collect::<Vec<_>>();
        let previous = matches
            .iter()
            .find(|task| {
                matches!(
                    task.status.as_str(),
                    "queued"
                        | "probing"
                        | "downloading"
                        | "waiting_processing"
                        | "processing"
                        | "paused"
                )
            })
            .or_else(|| matches.first())
            .cloned();
        if let Some(task) = &previous
            && matches!(
                task.status.as_str(),
                "queued"
                    | "probing"
                    | "downloading"
                    | "waiting_processing"
                    | "processing"
                    | "paused"
            )
        {
            return Ok(
                json!({"taskIds":[task.id],"kind":"assets","files":task.files,"results":[{"id":task.id,"status":task.status,"action":"existing"}]}),
            );
        }
        let tools = self.freeze_tools().await?;
        let id = previous
            .as_ref()
            .map(|task| task.id.clone())
            .unwrap_or_else(|| uuid::Uuid::new_v4().to_string());
        let mut previous_outputs = previous
            .as_ref()
            .map(|task| task.previous_outputs.clone())
            .unwrap_or_default();
        if let Some(task) = &previous
            && task.status == "completed"
            && let Some(publication) = &task.publication
        {
            previous_outputs.push(publication.target.clone());
            previous_outputs.extend(publication.auxiliary.iter().map(|file| file.target.clone()));
        }
        previous_outputs.sort();
        previous_outputs.dedup();
        if let Some(task) = &previous {
            self.stop_task(&id, "cancelled").await?;
            crate::processing::prepare_redownload(self, task).await?;
            crate::remove_task_directory(&self.config.data_dir.join("tasks").join(&id))?;
            if !task.options.processing_temp_dir.is_empty() {
                crate::remove_task_directory(&crate::processing::workspace(self, task))?;
            }
        }
        self.credentials
            .delete_secret(&format!("task-cookie:{id}"))?;
        self.credentials
            .delete_secret(&format!("task-proxy:{id}"))?;
        let cookie_snapshot = self
            .credential_snapshot(&options, url)?
            .map(|cookie| {
                let key = format!("task-cookie:{id}");
                self.credentials.put_secret(&key, &cookie)?;
                Ok::<_, anyhow::Error>(key)
            })
            .transpose()?;
        let proxy_snapshot = if options.proxy.is_empty() {
            None
        } else {
            let key = format!("task-proxy:{id}");
            self.credentials.put_secret(&key, &options.proxy)?;
            Some(key)
        };
        options.proxy.clear();
        let task = Task {
            id: id.clone(),
            kind: "assets".into(),
            files: vec![],
            url: url.into(),
            title: "Waiting for metadata".into(),
            thumbnail: None,
            size: None,
            source_info: None,
            media_info: None,
            status: "queued".into(),
            stage: "queued".into(),
            steps: vec![],
            step_index: None,
            progress: 0.0,
            stage_progress: None,
            stage_indeterminate: true,
            speed: None,
            eta: None,
            error: None,
            warnings: vec![],
            created_at: chrono::Utc::now().to_rfc3339(),
            options,
            download_dir: Some(self.download_directory()),
            output_path: previous.as_ref().and_then(|task| task.output_path.clone()),
            previous_outputs,
            cookie_snapshot,
            proxy_snapshot,
            manifest: None,
            publication: None,
            conversion: false,
            tools,
        };
        if previous.is_some() {
            self.db.save_task(&task)?;
        } else {
            self.db.insert_task(&task)?;
        }
        self.emit("task.upsert", task.public());
        self.schedule(&id)?;
        Ok(
            json!({"taskIds":[id],"kind":"assets","files":[],"results":[{"id":id,"action":if previous.is_some(){"restarted"}else{"added"},"status":"queued"}]}),
        )
    }

    pub async fn research_convert_media_options(&self, id: &str, options: &Value) -> Result<Value> {
        let media = self.db.media_by_id(id)?;
        crate::processing::queue_conversion_options(self, &media.task_id, options).await
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    #[tokio::test]
    async fn assets_reject_invalid_requests_before_freezing_tools() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let app = App::open(crate::AppConfig {
            data_dir: temp.path().join("data"),
            download_dir: temp.path().join("downloads"),
            tool_dir: None,
        })
        .await?;
        for assets in [
            json!([]),
            json!(["cookies"]),
            json!([1]),
            json!(["info", "info", "info", "info", "info"]),
        ] {
            assert!(
                app.research_download_assets(
                    &json!({"url":"https://example.test/video", "assets":assets})
                )
                .await
                .is_err()
            );
        }
        assert!(app.db.tasks()?.is_empty());
        app.shutdown().await?;
        Ok(())
    }

    #[tokio::test]
    async fn deleting_asset_tasks_keeps_or_removes_exported_files_as_requested() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let app = App::open(crate::AppConfig {
            data_dir: temp.path().join("data"),
            download_dir: temp.path().join("downloads"),
            tool_dir: None,
        })
        .await?;
        for delete_files in [false, true] {
            let mut task = crate::tests::paused_task(Default::default());
            task.kind = "assets".into();
            task.stage("completed", "completed", Some(100.0));
            let target = app
                .config
                .download_dir
                .join(format!("{}.info.json", task.id));
            let auxiliary = target.with_extension("description.txt");
            std::fs::write(&target, b"{}")?;
            std::fs::write(&auxiliary, b"Notes")?;
            task.output_path = Some(target.clone());
            task.publication = Some(crate::model::Publication {
                committed: false,
                temporary: target.with_extension("publish"),
                target: target.clone(),
                hash: String::new(),
                size: 2,
                duration: 0.0,
                auxiliary: vec![crate::model::AuxiliaryPublication {
                    temporary: auxiliary.with_extension("publish"),
                    target: auxiliary.clone(),
                    hash: String::new(),
                    size: 5,
                }],
                backup: None,
            });
            app.db.insert_task(&task)?;
            app.command(
                "tasks.delete",
                json!({"id":task.id,"deleteFiles":delete_files}),
            )
            .await?;
            assert!(app.db.task(&task.id).is_err());
            assert_eq!(target.exists(), !delete_files);
            assert_eq!(auxiliary.exists(), !delete_files);
            assert!(app.db.media()?.is_empty());
        }
        app.shutdown().await?;
        Ok(())
    }

    #[tokio::test]
    #[ignore = "requires bundled yt-dlp and FFmpeg runtimes"]
    async fn asset_task_uses_frozen_site_cookies_without_downloading_media() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let tool_dir = std::env::var_os("HUIFA_TEST_TOOLS")
            .map(std::path::PathBuf::from)
            .unwrap_or_else(|| {
                std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../runtime-tools")
            });
        let app = App::open(crate::AppConfig {
            data_dir: temp.path().join("data"),
            download_dir: temp.path().join("downloads"),
            tool_dir: Some(tool_dir),
        })
        .await?;
        let fixture = temp.path().join("podcast.mp4");
        let cli: Vec<String> = [
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
            "-c:v",
            "libx264",
            "-threads",
            "1",
        ]
        .into_iter()
        .map(str::to_string)
        .chain([fixture.to_string_lossy().into_owned()])
        .collect();
        crate::process::run(
            &app.tool("ffmpeg")?,
            &cli,
            None,
            &tokio_util::sync::CancellationToken::new(),
            None,
        )
        .await?;
        let bytes = std::sync::Arc::new(std::fs::read(&fixture)?);
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
        let url = format!(
            "http://{}/podcast.mp4?necessary=retained",
            listener.local_addr()?
        );
        let stop = tokio_util::sync::CancellationToken::new();
        let shutdown = stop.clone();
        let served = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let count = served.clone();
        let server = tokio::spawn(async move {
            loop {
                let accepted =
                    tokio::select! {v=listener.accept()=>v,_=shutdown.cancelled()=>break};
                let Ok((mut socket, _)) = accepted else { break };
                let bytes = bytes.clone();
                let count = count.clone();
                tokio::spawn(async move {
                    let mut request = [0u8; 16384];
                    let Ok(size) = socket.read(&mut request).await else {
                        return;
                    };
                    let request = String::from_utf8_lossy(&request[..size]);
                    let authenticated = request.lines().any(|line| {
                        line.split_once(':').is_some_and(|(name, value)| {
                            name.eq_ignore_ascii_case("cookie")
                                && value.contains("login_sid=asset-auth")
                        })
                    });
                    if !authenticated {
                        let _ = socket.write_all(b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\nConnection: close\r\n\r\n").await;
                        return;
                    }
                    count.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                    let header = format!(
                        "HTTP/1.1 200 OK\r\nContent-Type: video/mp4\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                        bytes.len()
                    );
                    let _ = socket.write_all(header.as_bytes()).await;
                    if !request.starts_with("HEAD ") {
                        let _ = socket.write_all(&bytes).await;
                    }
                });
            }
        });
        app.import_login_cookies(
            "Local site",
            "# Netscape HTTP Cookie File\n127.0.0.1\tFALSE\t/\tFALSE\t0\tlogin_sid\tasset-auth\n",
        )?;
        let queued = app
            .research_download_assets(&json!({"url":url,"assets":["info"]}))
            .await?;
        let id = queued["taskIds"][0].as_str().unwrap();
        let completed = tokio::time::timeout(std::time::Duration::from_secs(60), async {
            loop {
                let task = app.db.task(id)?;
                ensure!(
                    !matches!(task.status.as_str(), "failed" | "processing_failed"),
                    "{:?}",
                    task.error
                );
                if task.status == "completed" {
                    return Ok::<_, anyhow::Error>(task);
                };
                tokio::time::sleep(std::time::Duration::from_millis(100)).await;
            }
        })
        .await??;
        stop.cancel();
        server.await?;
        assert!(served.load(std::sync::atomic::Ordering::SeqCst) > 0);
        assert_eq!(completed.kind, "assets");
        assert_eq!(completed.files.len(), 1);
        assert_eq!(completed.files[0].kind, "info");
        assert!(
            completed
                .manifest
                .as_ref()
                .unwrap()
                .files
                .iter()
                .all(|file| !matches!(file.kind.as_str(), "audio" | "video"))
        );
        assert!(completed.cookie_snapshot.is_none());
        assert!(app.db.media()?.is_empty());
        let exported: Value =
            serde_json::from_slice(&std::fs::read(completed.output_path.as_ref().unwrap())?)?;
        assert!(!exported.to_string().contains("asset-auth"));
        assert!(!exported.to_string().contains("necessary=retained"));
        app.shutdown().await?;
        Ok(())
    }
}

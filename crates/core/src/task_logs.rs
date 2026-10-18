use crate::{App, model::Task, process};
use anyhow::{Context, Result, ensure};
use rusqlite::{Connection, params};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    fs::{File, OpenOptions},
    io::{Read, Seek, SeekFrom, Write},
    path::PathBuf,
    sync::{Arc, Mutex},
};

const LIMIT: usize = 500;
const PAGE_LIMIT: u64 = 256 * 1024;

#[derive(Clone)]
pub(crate) struct RawLog {
    file: Arc<File>,
    lock: Arc<Mutex<()>>,
    pub(crate) directory: PathBuf,
}

impl RawLog {
    pub(crate) fn write_file(&self, source: &str, input: &mut File) -> Result<()> {
        let mut last = [0];
        if input.metadata()?.len() > 0 {
            input.seek(SeekFrom::End(-1))?;
            input.read_exact(&mut last)?;
        }
        input.rewind()?;
        let _guard = self.lock.lock().unwrap();
        let mut file = self.file.as_ref();
        writeln!(file, "[{}] {source}", chrono::Utc::now().to_rfc3339())?;
        std::io::copy(input, &mut file)?;
        if last != *b"\n" {
            writeln!(file)?;
        }
        file.flush()?;
        Ok(())
    }

    pub(crate) fn write(&self, source: &str, bytes: &[u8]) -> Result<()> {
        let _guard = self.lock.lock().unwrap();
        let mut file = self.file.as_ref();
        writeln!(file, "[{}] {source}", chrono::Utc::now().to_rfc3339())?;
        file.write_all(bytes)?;
        if !bytes.ends_with(b"\n") {
            writeln!(file)?;
        }
        file.flush()?;
        Ok(())
    }
}

#[derive(Clone, Serialize, Deserialize)]
pub(crate) struct Entry {
    pub timestamp: String,
    pub level: String,
    pub message: String,
}

fn sanitize(message: &str) -> String {
    let message = process::redact(message).replace("[application storage]", "/application-storage");
    let mut quoted_path = None;
    message
        .split_whitespace()
        .map(|word| {
            let raw_value = word.rsplit_once('=').map_or(word, |(_, value)| value);
            let value = raw_value.trim_start_matches(['\'', '"', '(', '[']);
            let bytes = value.as_bytes();
            let absolute = value.starts_with('/')
                || value.starts_with("\\\\")
                || (bytes.len() > 2
                    && bytes[0].is_ascii_alphabetic()
                    && bytes[1] == b':'
                    && matches!(bytes[2], b'\\' | b'/'));
            let lower = word.to_ascii_lowercase();
            let secret = [
                "password=",
                "passwd=",
                "api_key=",
                "apikey=",
                "api-key=",
                "secret=",
            ]
            .iter()
            .any(|key| lower.contains(key));
            if absolute || quoted_path.is_some() {
                if absolute && matches!(raw_value.chars().next(), Some('\'' | '"')) {
                    quoted_path = raw_value.chars().next();
                }
                if quoted_path.is_some_and(|quote| word.ends_with(quote) && word.len() > 1) {
                    quoted_path = None;
                }
                "[local path]".to_string()
            } else if secret {
                "[redacted]".to_string()
            } else {
                word.to_string()
            }
        })
        .collect::<Vec<_>>()
        .join(" ")
}

impl Entry {
    fn new(level: &str, message: &str) -> Self {
        Self {
            timestamp: chrono::Utc::now().to_rfc3339(),
            level: level.into(),
            message: sanitize(message),
        }
    }
}

pub(crate) fn append(db: &Connection, id: &str, entry: &Entry) -> Result<()> {
    db.execute(
        "INSERT INTO task_logs(task_id,timestamp,level,message) VALUES(?1,?2,?3,?4)",
        params![id, entry.timestamp, entry.level, entry.message],
    )?;
    // Keep one extra row so the response can report that older entries were dropped.
    db.execute(
        "DELETE FROM task_logs WHERE task_id=?1 AND seq NOT IN (SELECT seq FROM task_logs WHERE task_id=?1 ORDER BY seq DESC LIMIT ?2)",
        params![id, LIMIT + 1],
    )?;
    Ok(())
}

pub(crate) fn record_changes(db: &Connection, previous: Option<&Task>, task: &Task) -> Result<()> {
    if previous.is_none_or(|old| old.status != task.status || old.stage != task.stage) {
        append(
            db,
            &task.id,
            &Entry::new(
                "info",
                &format!("Task status: {}; stage: {}", task.status, task.stage),
            ),
        )?;
        if previous.is_none() || task.status == "queued" {
            append(
                db,
                &task.id,
                &Entry::new(
                    "info",
                    &format!(
                        "Download settings: quality={}; liveFilter={}; fragmentConcurrent={}",
                        task.options.quality,
                        task.options.live_filter,
                        task.options.fragment_concurrent,
                    ),
                ),
            )?;
        }
    }
    if let Some(error) = task.error.as_deref()
        && previous.is_none_or(|old| old.error != task.error)
    {
        append(db, &task.id, &Entry::new("error", error))?;
    }
    for warning in &task.warnings {
        if previous.is_none_or(|old| !old.warnings.contains(warning)) {
            append(db, &task.id, &Entry::new("warning", warning))?;
        }
    }
    Ok(())
}

impl App {
    pub(crate) fn log_task(&self, id: &str, level: &str, message: &str) -> Result<()> {
        let level = match level {
            "error" => "error",
            "warning" => "warning",
            _ => "info",
        };
        self.raw_task_log(id)?.write(level, message.as_bytes())?;
        self.db.append_task_log(id, &Entry::new(level, message))
    }

    fn raw_task_log_path(&self, id: &str) -> Result<PathBuf> {
        let task = self.db.task(id)?;
        ensure!(
            uuid::Uuid::parse_str(&task.id)?.to_string() == task.id,
            "Invalid task ID"
        );
        let path = self
            .config
            .data_dir
            .join("task-logs")
            .join(format!("{}.log", task.id));
        crate::reject_links(&path)?;
        Ok(path)
    }

    pub(crate) fn raw_task_log(&self, id: &str) -> Result<RawLog> {
        let path = self.raw_task_log_path(id)?;
        std::fs::create_dir_all(path.parent().unwrap())?;
        crate::reject_links(&path)?;
        let _guard = self.task_log_lock.lock().unwrap();
        let mut file = OpenOptions::new().create(true).append(true).open(path)?;
        if file.metadata()?.len() == 0 {
            let task = self.db.task(id)?;
            writeln!(
                file,
                "[{}] Raw task journal started; earlier runs' complete output may be unavailable.\nsource: {}\noptions: {}",
                chrono::Utc::now().to_rfc3339(),
                task.url,
                serde_json::to_string(&task.options)?
            )?;
        }
        Ok(RawLog {
            file: Arc::new(file),
            lock: self.task_log_lock.clone(),
            directory: self.config.data_dir.join("task-logs"),
        })
    }

    pub(crate) fn remove_task_log(&self, id: &str) -> Result<()> {
        let path = self.raw_task_log_path(id)?;
        let _guard = self.task_log_lock.lock().unwrap();
        match std::fs::remove_file(path) {
            Ok(()) => Ok(()),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(error) => Err(error).context("Cannot remove the task log"),
        }
    }

    pub(crate) fn task_logs(&self, id: &str, args: &Value) -> Result<Value> {
        let path = self.raw_task_log_path(id)?;
        let cursor = |key: &str| -> Result<Option<u64>> {
            args.get(key)
                .map(|value| {
                    value
                        .as_u64()
                        .with_context(|| format!("{key} must be a non-negative byte offset"))
                })
                .transpose()
        };
        let before = cursor("before")?;
        let offset = cursor("offset")?;
        ensure!(
            before.is_none() || offset.is_none(),
            "Use before or offset, not both"
        );
        let limit = cursor("limit")?.unwrap_or(PAGE_LIMIT);
        ensure!(
            (4..=PAGE_LIMIT).contains(&limit),
            "Log page limit must be 4–262144 bytes"
        );
        let _guard = self.task_log_lock.lock().unwrap();
        let (mut reader, legacy): (Box<dyn ReadSeek>, bool) = match File::open(path) {
            Ok(file) => (Box::new(file), false),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                let task = self.db.task(id)?;
                let mut text = "Earlier versions saved summary logs only; full original tool output is unavailable.\n".to_string();
                for entry in self.db.task_logs(id)? {
                    text.push_str(&format!(
                        "[{}] {}\n{}\n",
                        entry.timestamp, entry.level, entry.message
                    ));
                }
                if let Some(error) = task.error {
                    text.push_str(&format!(
                        "Earlier task error (failure time unavailable): {error}\n"
                    ));
                }
                (Box::new(std::io::Cursor::new(text.into_bytes())), true)
            }
            Err(error) => return Err(error).context("Cannot read the task log"),
        };
        let total = reader.seek(SeekFrom::End(0))?;
        let end = before.unwrap_or(total).min(total);
        let mut start = offset
            .unwrap_or_else(|| end.saturating_sub(limit))
            .min(total);
        let mut end = if offset.is_some() {
            (start + limit).min(total)
        } else {
            end
        };
        // Cursors always land between UTF-8 characters, including tiny requested pages.
        if start > 0 {
            reader.seek(SeekFrom::Start(start))?;
            let mut probe = [0u8; 3];
            let count = reader.read(&mut probe)?;
            for byte in &probe[..count] {
                if byte & 0xc0 != 0x80 {
                    break;
                }
                start += 1;
            }
        }
        if end < total {
            reader.seek(SeekFrom::Start(end))?;
            let mut probe = [0u8; 3];
            let count = reader.read(&mut probe)?;
            for byte in &probe[..count] {
                if byte & 0xc0 != 0x80 {
                    break;
                }
                end += 1;
            }
        }
        reader.seek(SeekFrom::Start(start))?;
        let mut bytes = Vec::new();
        reader
            .take(end.saturating_sub(start))
            .read_to_end(&mut bytes)?;
        let text = String::from_utf8_lossy(&bytes);
        Ok(
            json!({"text":text,"start":start,"end":end,"total":total,"hasMore":if offset.is_some(){end<total}else{start>0},"raw":true,"legacy":legacy}),
        )
    }
}

trait ReadSeek: Read + Seek {}
impl<T: Read + Seek> ReadSeek for T {}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::AppConfig;

    #[tokio::test]
    async fn raw_task_logs_keep_all_output_paginate_and_survive_restart() -> Result<()> {
        let root = tempfile::tempdir()?;
        let config = AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("downloads"),
            tool_dir: None,
        };
        let app = App::open(config.clone()).await?;
        let task = crate::tests::paused_task(Default::default());
        app.db.insert_task(&task)?;
        let journal = app.raw_task_log(&task.id)?;
        for index in 0..1005 {
            journal.write(
                "stdout",
                format!("engine record {index} 中文 {}\n", "x".repeat(1000)).as_bytes(),
            )?;
        }
        drop(journal);
        let response = app.command("tasks.logs", json!({"id":task.id})).await?;
        assert_eq!(response["raw"], true);
        assert_eq!(response["legacy"], false);
        assert_eq!(response["hasMore"], true);
        assert!(
            response["text"]
                .as_str()
                .unwrap()
                .contains("engine record 1004")
        );
        let mut complete = String::new();
        let mut offset = 0;
        loop {
            let page = app
                .command(
                    "tasks.logs",
                    json!({"id":task.id,"offset":offset,"limit":4093}),
                )
                .await?;
            assert_eq!(page["start"], offset);
            complete.push_str(page["text"].as_str().unwrap());
            let end = page["end"].as_u64().unwrap();
            assert!(end >= offset && end - offset <= 4096);
            offset = end;
            if page["hasMore"] == false {
                break;
            }
        }
        assert_eq!(
            complete,
            std::fs::read_to_string(app.raw_task_log_path(&task.id)?)?
        );
        assert!(complete.contains("engine record 0 中文"));
        assert!(!complete.contains('\u{fffd}'));
        assert!(
            app.command("tasks.logs", json!({"id":"missing"}))
                .await
                .is_err()
        );
        assert!(
            app.command("tasks.logs", json!({"path":"/unowned/file"}))
                .await
                .is_err()
        );
        let snapshot = app
            .command("snapshot", json!({"includeTools":false}))
            .await?;
        assert!(snapshot["tasks"][0].get("logs").is_none());
        for args in [
            json!({"id":task.id,"limit":1}),
            json!({"id":task.id,"limit":PAGE_LIMIT+1}),
            json!({"id":task.id,"offset":-1}),
            json!({"id":task.id,"offset":1,"before":2}),
        ] {
            assert!(app.command("tasks.logs", args).await.is_err());
        }
        app.shutdown().await?;
        drop(app);
        let app = App::open(config).await?;
        assert_eq!(
            app.command("tasks.logs", json!({"id":task.id})).await?,
            response
        );
        let path = app.raw_task_log_path(&task.id)?;
        app.command("tasks.delete", json!({"id":task.id,"deleteFiles":false}))
            .await?;
        assert!(!path.exists());
        assert!(app.db.task_logs(&task.id)?.is_empty());
        assert!(
            app.command("tasks.logs", json!({"id":task.id}))
                .await
                .is_err()
        );
        app.shutdown().await?;
        Ok(())
    }

    #[tokio::test]
    async fn task_logs_record_transitions_errors_and_warnings_without_progress_spam() -> Result<()>
    {
        let root = tempfile::tempdir()?;
        let app = App::open(AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("downloads"),
            tool_dir: None,
        })
        .await?;
        let mut task = crate::tests::paused_task(Default::default());
        app.db.insert_task(&task)?;
        task.stage("downloading", "downloading_video", Some(1.0));
        app.save_task(&task)?;
        let before = app.db.task_logs(&task.id)?.len();
        task.stage_progress = Some(55.0);
        app.save_task(&task)?;
        assert_eq!(app.db.task_logs(&task.id)?.len(), before);
        task.warnings
            .push("Subtitle language was not provided".into());
        app.save_task(&task)?;
        task.status = "failed".into();
        task.error = Some("yt-dlp exited with code 1: requested format is unavailable".into());
        app.save_task(&task)?;
        app.save_task(&task)?;
        let entries = app.db.task_logs(&task.id)?;
        assert_eq!(
            entries
                .iter()
                .filter(|entry| entry.level == "warning")
                .count(),
            1
        );
        assert_eq!(
            entries
                .iter()
                .filter(|entry| entry.level == "error")
                .count(),
            1
        );
        assert_eq!(
            entries.last().unwrap().message,
            task.error.as_deref().unwrap()
        );
        app.shutdown().await?;
        Ok(())
    }

    #[tokio::test]
    async fn raw_task_logs_preserve_secrets_paths_and_honestly_label_legacy_errors() -> Result<()> {
        let root = tempfile::tempdir()?;
        let app = App::open(AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("downloads"),
            tool_dir: None,
        })
        .await?;
        let mut task = crate::tests::paused_task(Default::default());
        app.db.insert_task(&task)?;
        for message in [
            "ERROR: request https://media.test/video?signature=private-signature",
            "ERROR: Cookie: session=private-cookie",
            "ERROR: Authorization: Bearer private-token",
            "ERROR: password=private-password api_key=private-api-key",
            "ERROR: cannot read 'C:\\private data\\hidden\\file.mp4' or /private/hidden/input.mp4",
            "ERROR: output='C:\\private data\\hidden\\assigned.mp4' path=/private/hidden/assigned.mp4",
        ] {
            app.log_task(&task.id, "error", message)?;
        }
        app.log_task(
            &task.id,
            "error",
            &format!(
                "Cannot read {}",
                app.config
                    .data_dir
                    .join("private-name/fragment.part")
                    .display()
            ),
        )?;
        let connection = rusqlite::Connection::open(app.config.data_dir.join("rust-core.sqlite3"))?;
        connection.execute(
            "INSERT INTO task_logs(task_id,timestamp,level,message) VALUES(?1,?2,'error',?3)",
            params![
                task.id,
                task.created_at,
                "Earlier log: [application storage]/private-name/fragment.part"
            ],
        )?;
        app.log_task(&task.id, "info", "HU_PROGRESS: {\"speed\":100}")?;
        let logs = app.task_logs(&task.id, &json!({}))?["text"]
            .as_str()
            .unwrap()
            .to_string();
        for secret in [
            "private-signature",
            "private-cookie",
            "private-token",
            "private-password",
            "private-api-key",
            "hidden",
            "input.mp4",
            "private-name",
            "fragment.part",
        ] {
            assert!(logs.contains(secret), "raw output lost {secret}: {logs}");
        }
        task.status = "failed".into();
        task.error = Some("Legacy failure: no stream was returned".into());
        app.db.save_task(&task)?;
        connection.execute("DELETE FROM task_logs WHERE task_id=?1", [&task.id])?;
        app.remove_task_log(&task.id)?;
        let logs = app.command("tasks.logs", json!({"id":task.id})).await?;
        assert_eq!(logs["legacy"], true);
        let text = logs["text"].as_str().unwrap();
        assert!(text.contains("full original tool output is unavailable"));
        assert!(text.contains(
            "Earlier task error (failure time unavailable): Legacy failure: no stream was returned"
        ));
        app.shutdown().await?;
        Ok(())
    }

    #[tokio::test]
    async fn raw_process_log_records_the_complete_command_stdout_stderr_and_exit() -> Result<()> {
        let root = tempfile::tempdir()?;
        let app = App::open(AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("downloads"),
            tool_dir: None,
        })
        .await?;
        let task = crate::tests::paused_task(Default::default());
        app.db.insert_task(&task)?;
        #[cfg(windows)]
        let (executable, args) = (
            PathBuf::from("powershell.exe"),
            vec!["-NoProfile".into(),"-NonInteractive".into(),"-Command".into(),
                "[Console]::WriteLine('{\"url\":\"https://media.test/play?token=raw-metadata-canary\",\"title\":\"mock yt-dlp probe\"}'); [Console]::Error.WriteLine('[debug] Cookie: session=raw-cookie-canary'); [Console]::Error.WriteLine('[debug] Authorization: Bearer raw-token-canary'); [Console]::WriteLine('HU_PROGRESS: {\"percent\":50,\"format_id\":\"raw-format-canary\"}'); [Console]::Error.WriteLine('ERROR: raw stderr tail'); exit 7".into()],
        );
        #[cfg(unix)]
        let (executable, args) = (
            PathBuf::from("sh"),
            vec!["-c".into(), "printf '%s\\n' '{\"url\":\"https://media.test/play?token=raw-metadata-canary\",\"title\":\"mock yt-dlp probe\"}'; printf '%s\\n' '[debug] Cookie: session=raw-cookie-canary' '[debug] Authorization: Bearer raw-token-canary' >&2; printf '%s\\n' 'HU_PROGRESS: {\"percent\":50,\"format_id\":\"raw-format-canary\"}'; printf '%s\\n' 'ERROR: raw stderr tail' >&2; exit 7".into()],
        );
        let error = process::run_logged(
            &executable,
            &args,
            Some(root.path()),
            &tokio_util::sync::CancellationToken::new(),
            None,
            app.raw_task_log(&task.id)?,
        )
        .await
        .err()
        .context("Mock yt-dlp should fail")?;
        assert!(error.to_string().contains("code 7"));
        assert!(!error.to_string().contains("raw-cookie-canary"));
        assert!(!error.to_string().contains("raw-token-canary"));
        let value = app.command("tasks.logs", json!({"id":task.id})).await?;
        let text = value["text"].as_str().unwrap();
        for canary in [
            "raw-metadata-canary",
            "raw-cookie-canary",
            "raw-token-canary",
            "raw-format-canary",
            "raw stderr tail",
            "stdout",
            "stderr",
            "command",
            "exit",
        ] {
            assert!(text.contains(canary), "missing {canary}: {text}");
        }
        assert!(text.contains(&serde_json::to_string(&args)?));
        assert!(
            text.contains("stdout\n{\"url\":\"https://media.test/play?token=raw-metadata-canary\"")
        );
        assert!(text.contains("stderr\n[debug] Cookie: session=raw-cookie-canary"));
        assert!(text.contains("stderr\n[debug] Authorization: Bearer raw-token-canary"));
        assert!(text.contains("stdout\nHU_PROGRESS: {\"percent\":50"));
        let snapshot = app
            .command("snapshot", json!({"includeTools":false}))
            .await?
            .to_string();
        assert!(!snapshot.contains("raw-cookie-canary"));
        assert!(!snapshot.contains("raw-token-canary"));
        let result = app.command("tasks.get", json!({"id":task.id})).await?;
        assert_eq!(result["id"], task.id);
        assert!(result.get("tools").is_none());
        assert!(result.get("logs").is_none());
        app.shutdown().await?;
        Ok(())
    }

    #[tokio::test]
    async fn cancelling_a_raw_logged_process_drains_its_final_partial_line() -> Result<()> {
        let root = tempfile::tempdir()?;
        let app = App::open(AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("downloads"),
            tool_dir: None,
        })
        .await?;
        let task = crate::tests::paused_task(Default::default());
        app.db.insert_task(&task)?;
        let cancel = tokio_util::sync::CancellationToken::new();
        let stopped = cancel.clone();
        let callback = Arc::new(move |_: bool, line: &str| {
            if line == "READY" {
                stopped.cancel();
            }
        });
        #[cfg(windows)]
        let (executable, args) = (
            PathBuf::from("powershell.exe"),
            vec!["-NoProfile".into(),"-NonInteractive".into(),"-Command".into(),"[Console]::Write('raw-final-partial-canary'); [Console]::Error.WriteLine('READY'); Start-Sleep -Seconds 30".into()],
        );
        #[cfg(unix)]
        let (executable, args) = (
            PathBuf::from("sh"),
            vec![
                "-c".into(),
                "printf 'raw-final-partial-canary'; printf 'READY\\n' >&2; sleep 30 & wait".into(),
            ],
        );
        let error = tokio::time::timeout(
            std::time::Duration::from_secs(5),
            process::run_logged(
                &executable,
                &args,
                None,
                &cancel,
                Some(callback),
                app.raw_task_log(&task.id)?,
            ),
        )
        .await?
        .err()
        .context("Mock yt-dlp should be cancelled")?;
        assert!(error.to_string().contains("interrupted"));
        let text = app.command("tasks.logs", json!({"id":task.id})).await?["text"]
            .as_str()
            .unwrap()
            .to_string();
        assert!(text.contains("stdout\nraw-final-partial-canary\n"));
        assert!(text.contains("exit\nOperation interrupted"));
        app.shutdown().await?;
        Ok(())
    }

    #[tokio::test]
    async fn retained_media_keeps_its_log_until_the_media_is_deleted() -> Result<()> {
        let root = tempfile::tempdir()?;
        let app = App::open(AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("downloads"),
            tool_dir: None,
        })
        .await?;
        let mut task = crate::tests::paused_task(Default::default());
        let path = app.config.download_dir.join("kept.mp4");
        std::fs::write(&path, b"kept source")?;
        task.stage("completed", "completed", Some(100.0));
        task.output_path = Some(path.clone());
        app.db.insert_task(&task)?;
        app.db.complete(
            &task,
            &crate::model::Media {
                id: uuid::Uuid::new_v4().to_string(),
                task_id: task.id.clone(),
                title: task.title.clone(),
                filename: "kept.mp4".into(),
                size: 11,
                duration: 1.0,
                path: path.clone(),
                assets: vec![],
                media_info: None,
            },
        )?;
        app.log_task(&task.id, "info", "retained-raw-canary")?;
        let journal = app.raw_task_log_path(&task.id)?;
        app.command("tasks.delete", json!({"id":task.id,"deleteFiles":false}))
            .await?;
        assert!(journal.is_file());
        assert_eq!(std::fs::read(&path)?, b"kept source");
        assert_eq!(
            app.command("tasks.get", json!({"id":task.id})).await?["status"],
            "retained"
        );
        assert!(
            app.command("tasks.logs", json!({"id":task.id})).await?["text"]
                .as_str()
                .unwrap()
                .contains("retained-raw-canary")
        );
        app.command("tasks.delete", json!({"id":task.id,"deleteFiles":true}))
            .await?;
        assert!(!journal.exists());
        assert!(!path.exists());
        app.shutdown().await?;
        Ok(())
    }
}

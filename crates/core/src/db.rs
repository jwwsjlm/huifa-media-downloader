use crate::model::{Media, Task};
use anyhow::{Context, Result, ensure};
use rusqlite::{Connection, OptionalExtension, params};
use serde_json::{Value, json};
use std::{
    path::{Path, PathBuf},
    sync::Mutex,
};

pub(crate) struct Database {
    connection: Mutex<Connection>,
}

fn task_record(state: &str, media: Option<&str>) -> Result<Task> {
    let mut task: Task = serde_json::from_str(state)?;
    if matches!(task.status.as_str(), "completed" | "retained") {
        if let Some(record) = media {
            let media: Media = serde_json::from_str(record)?;
            task.size = Some(media.size);
            task.media_info = media.media_info.or(task.media_info);
        } else if let Some(publication) = task.publication.as_ref().filter(|p| p.committed) {
            task.size = Some(publication.size);
        }
        if let Some(info) = task.media_info.as_mut() {
            info.size = task.size;
        }
    }
    Ok(task)
}
impl Database {
    pub fn open(path: &Path) -> Result<Self> {
        let connection = Connection::open(path)?;
        let version: i64 = connection.query_row("PRAGMA user_version", [], |r| r.get(0))?;
        ensure!(
            version <= 1,
            "Database schema version {version} is newer than this application supports"
        );
        connection.busy_timeout(std::time::Duration::from_secs(5))?;
        connection.execute_batch(
            "PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA foreign_keys=ON;",
        )?;
        if version == 0 {
            connection.execute_batch("BEGIN IMMEDIATE;
                CREATE TABLE tasks(id TEXT PRIMARY KEY,state TEXT NOT NULL);
                CREATE TABLE media(id TEXT PRIMARY KEY,task_id TEXT NOT NULL UNIQUE,record TEXT NOT NULL);
                CREATE TABLE settings(id INTEGER PRIMARY KEY CHECK(id=1),value TEXT NOT NULL);
                INSERT INTO settings VALUES(1,'{}'); PRAGMA user_version=1; COMMIT;")?;
        }
        connection.execute_batch(
            "CREATE TABLE IF NOT EXISTS task_logs(seq INTEGER PRIMARY KEY,task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,timestamp TEXT NOT NULL,level TEXT NOT NULL,message TEXT NOT NULL);
             CREATE INDEX IF NOT EXISTS task_logs_by_task ON task_logs(task_id,seq);",
        )?;
        Ok(Self {
            connection: Mutex::new(connection),
        })
    }
    pub fn tasks(&self) -> Result<Vec<Task>> {
        Ok(self
            .tasks_including_retained()?
            .into_iter()
            .filter(|task| task.status != "retained")
            .collect())
    }
    pub fn tasks_including_retained(&self) -> Result<Vec<Task>> {
        let db = self.connection.lock().unwrap();
        let mut query = db.prepare("SELECT tasks.state,media.record FROM tasks LEFT JOIN media ON media.task_id=tasks.id ORDER BY tasks.rowid DESC")?;
        query
            .query_map([], |r| {
                Ok((r.get::<_, String>(0)?, r.get::<_, Option<String>>(1)?))
            })?
            .map(|r| {
                let (state, media) = r?;
                task_record(&state, media.as_deref())
            })
            .collect()
    }
    pub fn task(&self, id: &str) -> Result<Task> {
        let db = self.connection.lock().unwrap();
        let (state, media): (String, Option<String>) = db
            .query_row("SELECT tasks.state,media.record FROM tasks LEFT JOIN media ON media.task_id=tasks.id WHERE tasks.id=?1", [id], |r| Ok((r.get(0)?,r.get(1)?)))
            .optional()?
            .context("Task does not exist")?;
        task_record(&state, media.as_deref())
    }
    pub fn insert_task(&self, task: &Task) -> Result<()> {
        let mut db = self.connection.lock().unwrap();
        let transaction = db.transaction()?;
        transaction.execute(
            "INSERT INTO tasks VALUES(?1,?2)",
            params![task.id, serde_json::to_string(task)?],
        )?;
        crate::task_logs::record_changes(&transaction, None, task)?;
        transaction.commit()?;
        Ok(())
    }
    pub fn save_task(&self, task: &Task) -> Result<()> {
        let mut db = self.connection.lock().unwrap();
        let transaction = db.transaction()?;
        let previous: Option<String> = transaction
            .query_row("SELECT state FROM tasks WHERE id=?1", [&task.id], |row| {
                row.get(0)
            })
            .optional()?;
        let previous: Task = serde_json::from_str(&previous.context("Task does not exist")?)?;
        let changed = transaction.execute(
            "UPDATE tasks SET state=?2 WHERE id=?1",
            params![task.id, serde_json::to_string(task)?],
        )?;
        ensure!(changed == 1, "Task does not exist");
        crate::task_logs::record_changes(&transaction, Some(&previous), task)?;
        transaction.commit()?;
        Ok(())
    }
    pub fn append_task_log(&self, id: &str, entry: &crate::task_logs::Entry) -> Result<()> {
        let mut db = self.connection.lock().unwrap();
        let transaction = db.transaction()?;
        let exists = transaction.query_row(
            "SELECT EXISTS(SELECT 1 FROM tasks WHERE id=?1)",
            [id],
            |row| row.get::<_, bool>(0),
        )?;
        ensure!(exists, "Task does not exist");
        crate::task_logs::append(&transaction, id, entry)?;
        transaction.commit()?;
        Ok(())
    }
    pub fn task_logs(&self, id: &str) -> Result<Vec<crate::task_logs::Entry>> {
        let db = self.connection.lock().unwrap();
        let mut query = db.prepare(
            "SELECT timestamp,level,message FROM task_logs WHERE task_id=?1 ORDER BY seq",
        )?;
        Ok(query
            .query_map([id], |row| {
                Ok(crate::task_logs::Entry {
                    timestamp: row.get(0)?,
                    level: row.get(1)?,
                    message: row.get(2)?,
                })
            })?
            .collect::<rusqlite::Result<_>>()?)
    }
    pub fn remove_task(&self, task: &Task, delete_media: bool) -> Result<()> {
        let mut db = self.connection.lock().unwrap();
        let transaction = db.transaction()?;
        let retained = !delete_media
            && transaction.query_row(
                "SELECT EXISTS(SELECT 1 FROM media WHERE task_id=?1)",
                [&task.id],
                |row| row.get::<_, bool>(0),
            )?;
        if retained {
            // Library media still needs its source metadata and frozen tools after queue removal.
            let mut task = task.clone();
            task.status = "retained".into();
            task.cookie_snapshot = None;
            task.proxy_snapshot = None;
            task.publication = None;
            transaction.execute(
                "UPDATE tasks SET state=?2 WHERE id=?1",
                params![task.id, serde_json::to_string(&task)?],
            )?;
        } else {
            transaction.execute("DELETE FROM tasks WHERE id=?1", [&task.id])?;
        }
        if delete_media {
            transaction.execute("DELETE FROM media WHERE task_id=?1", [&task.id])?;
        }
        transaction.commit()?;
        Ok(())
    }
    pub fn media(&self) -> Result<Vec<Media>> {
        let db = self.connection.lock().unwrap();
        let mut query = db.prepare("SELECT record FROM media ORDER BY rowid DESC")?;
        query
            .query_map([], |r| r.get::<_, String>(0))?
            .map(|r| Ok(serde_json::from_str(&r?)?))
            .collect()
    }
    pub fn media_by_id(&self, id: &str) -> Result<Media> {
        let record: Option<String> = self
            .connection
            .lock()
            .unwrap()
            .query_row("SELECT record FROM media WHERE id=?1", [id], |r| r.get(0))
            .optional()?;
        serde_json::from_str(&record.context("Media does not exist")?).map_err(Into::into)
    }
    pub fn append_media_asset(&self, id: &str, asset: PathBuf) -> Result<Media> {
        let mut db = self.connection.lock().unwrap();
        let transaction = db.transaction()?;
        let record: Option<String> = transaction
            .query_row("SELECT record FROM media WHERE id=?1", [id], |row| {
                row.get(0)
            })
            .optional()?;
        let mut media: Media = serde_json::from_str(&record.context("Media does not exist")?)?;
        media.assets.push(asset);
        transaction.execute(
            "UPDATE media SET record=?2 WHERE id=?1",
            params![id, serde_json::to_string(&media)?],
        )?;
        transaction.commit()?;
        Ok(media)
    }
    pub fn media_for_task(&self, id: &str) -> Result<Option<Media>> {
        let record: Option<String> = self
            .connection
            .lock()
            .unwrap()
            .query_row("SELECT record FROM media WHERE task_id=?1", [id], |r| {
                r.get(0)
            })
            .optional()?;
        record
            .map(|value| serde_json::from_str(&value))
            .transpose()
            .map_err(Into::into)
    }
    pub fn save_media_info(
        &self,
        id: &str,
        path: &Path,
        info: &crate::model::MediaInfo,
    ) -> Result<()> {
        let mut db = self.connection.lock().unwrap();
        let transaction = db.transaction()?;
        let record: Option<(String, String)> = transaction
            .query_row(
                "SELECT id,record FROM media WHERE task_id=?1",
                [id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()?;
        if let Some((media_id, record)) = record {
            let mut media: Media = serde_json::from_str(&record)?;
            if media.path == path {
                media.media_info = Some(info.clone());
                transaction.execute(
                    "UPDATE media SET record=?2 WHERE id=?1",
                    params![media_id, serde_json::to_string(&media)?],
                )?;
            }
        }
        transaction.commit()?;
        Ok(())
    }
    pub fn complete(&self, task: &Task, media: &Media) -> Result<Media> {
        let mut db = self.connection.lock().unwrap();
        let transaction = db.transaction()?;
        let mut media = media.clone();
        if task.media_info.is_some() {
            media.media_info = task.media_info.clone();
        }
        if task.conversion {
            let record: Option<String> = transaction
                .query_row(
                    "SELECT record FROM media WHERE task_id=?1",
                    [&task.id],
                    |row| row.get(0),
                )
                .optional()?;
            if let Some(record) = record {
                let latest: Media = serde_json::from_str(&record)?;
                media.id = latest.id;
                for asset in latest.assets {
                    if !media.assets.contains(&asset) {
                        media.assets.push(asset);
                    }
                }
            }
        }
        if let Some(destination) = task.destination.as_ref().filter(|value| value.confirmed) {
            let mut replaced = destination.replaced_tasks();
            replaced.sort();
            replaced.dedup();
            for id in replaced.into_iter().filter(|id| id != &task.id) {
                let state: Option<String> = transaction
                    .query_row("SELECT state FROM tasks WHERE id=?1", [&id], |row| {
                        row.get(0)
                    })
                    .optional()?;
                if let Some(state) = state {
                    let mut previous: Task = serde_json::from_str(&state)?;
                    ensure!(
                        matches!(
                            previous.status.as_str(),
                            "completed" | "retained" | "failed" | "processing_failed" | "cancelled"
                        ),
                        "Another task started using the destination; original results were preserved"
                    );
                    previous.output_path = None;
                    previous.publication = None;
                    previous.destination = None;
                    previous.size = None;
                    previous.stage("failed", "result_replaced", None);
                    previous.error = Some("This result was replaced by another source after confirmation. Download again to restore it.".into());
                    transaction.execute("DELETE FROM media WHERE task_id=?1", [&id])?;
                    transaction.execute(
                        "UPDATE tasks SET state=?2 WHERE id=?1",
                        params![id, serde_json::to_string(&previous)?],
                    )?;
                    crate::task_logs::record_changes(
                        &transaction,
                        Some(&serde_json::from_str::<Task>(&state)?),
                        &previous,
                    )?;
                }
            }
        }
        transaction.execute("INSERT INTO media VALUES(?1,?2,?3) ON CONFLICT(task_id) DO UPDATE SET record=excluded.record",params![media.id,media.task_id,serde_json::to_string(&media)?])?;
        if task
            .destination
            .as_ref()
            .is_some_and(|value| value.confirmed)
        {
            let states = {
                let mut query = transaction.prepare("SELECT state FROM tasks WHERE id<>?1")?;
                query
                    .query_map([&task.id], |row| row.get::<_, String>(0))?
                    .collect::<rusqlite::Result<Vec<_>>>()?
            };
            for state in states {
                let mut other: Task = serde_json::from_str(&state)?;
                let count = other.previous_outputs.len();
                other
                    .previous_outputs
                    .retain(|path| *path != media.path && !media.assets.contains(path));
                if other.previous_outputs.len() != count {
                    transaction.execute(
                        "UPDATE tasks SET state=?2 WHERE id=?1",
                        params![other.id, serde_json::to_string(&other)?],
                    )?;
                }
            }
        }
        transaction.execute(
            "UPDATE tasks SET state=?2 WHERE id=?1",
            params![task.id, serde_json::to_string(task)?],
        )?;
        transaction.commit()?;
        Ok(media)
    }
    pub fn settings(&self) -> Result<Value> {
        let db = self.connection.lock().unwrap();
        let value: Option<String> = db
            .query_row("SELECT value FROM settings WHERE id=1", [], |r| r.get(0))
            .optional()?;
        Ok(value
            .map(|v| serde_json::from_str(&v))
            .transpose()?
            .unwrap_or(json!({})))
    }
    pub fn activate_cookie(&self, id: &str) -> Result<()> {
        self.connection.lock().unwrap().execute(
            "UPDATE settings SET value=json_set(value, '$.downloadOptions.cookieId', ?1) WHERE id=1",
            [id],
        )?;
        Ok(())
    }

    pub fn save_settings(&self, value: &Value) -> Result<()> {
        let db = self.connection.lock().unwrap();
        let current: String =
            db.query_row("SELECT value FROM settings WHERE id=1", [], |r| r.get(0))?;
        let current: Value = serde_json::from_str(&current)?;
        let mut value = value.clone();
        let object = value
            .as_object_mut()
            .context("settings must be an object")?;
        // Full drafts must not roll back the listener or preferences saved independently.
        for key in ["mcp", "uiPreferences"] {
            object.remove(key);
            if let Some(saved) = current.get(key) {
                object.insert(key.into(), saved.clone());
            }
        }
        db.execute(
            "UPDATE settings SET value=?1 WHERE id=1",
            [serde_json::to_string(&value)?],
        )?;
        Ok(())
    }
    pub fn save_mcp_settings(&self, config: &Value) -> Result<()> {
        self.connection.lock().unwrap().execute(
            "UPDATE settings SET value=json_set(value, '$.mcp', json(?1)) WHERE id=1",
            [serde_json::to_string(config)?],
        )?;
        Ok(())
    }
    pub fn save_ui_preferences(&self, preferences: &Value) -> Result<()> {
        let db = self.connection.lock().unwrap();
        let current: String =
            db.query_row("SELECT value FROM settings WHERE id=1", [], |r| r.get(0))?;
        let mut current: Value = serde_json::from_str(&current)?;
        if !current["uiPreferences"].is_object() {
            current["uiPreferences"] = json!({});
        }
        current["uiPreferences"].as_object_mut().unwrap().extend(
            preferences
                .as_object()
                .context("preferences must be an object")?
                .clone(),
        );
        db.execute(
            "UPDATE settings SET value=?1 WHERE id=1",
            [serde_json::to_string(&current)?],
        )?;
        Ok(())
    }
    pub fn checkpoint(&self) -> Result<()> {
        self.connection
            .lock()
            .unwrap()
            .execute_batch("PRAGMA wal_checkpoint(TRUNCATE)")?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn legacy_completed_task_sizes_follow_media_without_reading_files() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let db = Database::open(&temp.path().join("catalog.sqlite3"))?;
        let mut task = crate::tests::paused_task(Default::default());
        task.stage("completed", "completed", Some(100.0));
        task.size = Some(1);
        db.insert_task(&task)?;
        let media = Media {
            media_info: Some(crate::model::MediaInfo {
                height: Some(2160),
                size: Some(1),
                ..Default::default()
            }),
            id: "legacy-media".into(),
            task_id: task.id.clone(),
            title: "Video".into(),
            filename: "missing.mp4".into(),
            size: 654321,
            duration: 10.0,
            path: temp.path().join("missing.mp4"),
            assets: vec![],
        };
        db.complete(&task, &media)?;
        let loaded = db.task(&task.id)?;
        assert_eq!(loaded.public()["size"], 654321);
        assert_eq!(loaded.public()["mediaInfo"]["size"], 654321);
        assert_eq!(loaded.public()["mediaInfo"]["height"], 2160);
        assert_eq!(db.tasks()?[0].size, Some(654321));
        assert!(loaded.source_info.is_none());
        assert!(
            !serde_json::to_string(&loaded.public())?
                .contains(&temp.path().to_string_lossy().to_string())
        );
        Ok(())
    }
    #[test]
    fn conversion_publication_keeps_assets_added_after_its_read() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let db = Database::open(&temp.path().join("catalog.sqlite3"))?;
        let mut task = crate::tests::paused_task(Default::default());
        db.insert_task(&task)?;
        let media = Media {
            media_info: None,
            id: "m".into(),
            task_id: task.id.clone(),
            title: "Audit".into(),
            filename: "output.mp4".into(),
            size: 1,
            duration: 1.0,
            path: temp.path().join("output.mp4"),
            assets: vec![],
        };
        db.complete(&task, &media)?;
        let mut stale = db.media_by_id("m")?;
        let asset = temp.path().join("new-cover.jpg");
        db.append_media_asset("m", asset.clone())?;
        stale.size = 99;
        task.conversion = true;
        let published = db.complete(&task, &stale)?;
        assert_eq!(published.size, 99);
        assert_eq!(published.assets, vec![asset]);
        assert_eq!(db.media_by_id("m")?.assets, published.assets);
        Ok(())
    }
    #[test]
    fn single_media_queries_preserve_results_and_ignore_unrelated_corrupt_records() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let db = Database::open(&temp.path().join("catalog.sqlite3"))?;
        let media = Media {
            media_info: None,
            id: "media-1".into(),
            task_id: "task-1".into(),
            title: "Original".into(),
            filename: "video.mp4".into(),
            size: 1234,
            duration: 12.0,
            path: temp.path().join("video.mp4"),
            assets: vec![temp.path().join("cover.jpg")],
        };
        {
            let connection = db.connection.lock().unwrap();
            connection.execute(
                "INSERT INTO media VALUES(?1,?2,?3)",
                params![media.id, media.task_id, serde_json::to_string(&media)?],
            )?;
            connection.execute(
                "INSERT INTO media VALUES('broken-media','broken-task','invalid-json')",
                [],
            )?;
        }
        assert_eq!(
            serde_json::to_value(db.media_by_id("media-1")?)?,
            serde_json::to_value(&media)?
        );
        assert_eq!(
            serde_json::to_value(db.media_for_task("task-1")?.unwrap())?,
            serde_json::to_value(&media)?
        );
        assert_eq!(
            db.media_by_id("missing").unwrap_err().to_string(),
            "Media does not exist"
        );
        assert!(db.media_for_task("missing")?.is_none());
        assert!(db.media_by_id("broken-media").is_err());
        assert!(db.media_for_task("broken-task").is_err());
        let edited = db.append_media_asset(&media.id, temp.path().join("edited.jpg"))?;
        assert_eq!(edited.assets.len(), 2);
        assert_eq!(db.media_for_task("task-1")?.unwrap().assets, edited.assets);
        Ok(())
    }
    #[test]
    fn rejects_future_schema_without_mutating_it() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("future.sqlite3");
        let db = Connection::open(&path).unwrap();
        db.execute_batch("PRAGMA user_version=99; CREATE TABLE sentinel(value TEXT); INSERT INTO sentinel VALUES('keep');").unwrap();
        drop(db);
        assert!(Database::open(&path).is_err());
        let db = Connection::open(path).unwrap();
        assert_eq!(
            db.query_row("SELECT value FROM sentinel", [], |r| r.get::<_, String>(0))
                .unwrap(),
            "keep"
        );
    }
}

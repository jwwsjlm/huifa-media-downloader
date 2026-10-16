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
        Ok(Self {
            connection: Mutex::new(connection),
        })
    }
    pub fn tasks(&self) -> Result<Vec<Task>> {
        let db = self.connection.lock().unwrap();
        let mut query = db.prepare("SELECT state FROM tasks ORDER BY rowid DESC")?;
        query
            .query_map([], |r| r.get::<_, String>(0))?
            .map(|r| Ok(serde_json::from_str(&r?)?))
            .collect()
    }
    pub fn task(&self, id: &str) -> Result<Task> {
        let db = self.connection.lock().unwrap();
        let state: String = db
            .query_row("SELECT state FROM tasks WHERE id=?1", [id], |r| r.get(0))
            .optional()?
            .context("Task does not exist")?;
        Ok(serde_json::from_str(&state)?)
    }
    pub fn insert_task(&self, task: &Task) -> Result<()> {
        self.connection.lock().unwrap().execute(
            "INSERT INTO tasks VALUES(?1,?2)",
            params![task.id, serde_json::to_string(task)?],
        )?;
        Ok(())
    }
    pub fn save_task(&self, task: &Task) -> Result<()> {
        let changed = self.connection.lock().unwrap().execute(
            "UPDATE tasks SET state=?2 WHERE id=?1",
            params![task.id, serde_json::to_string(task)?],
        )?;
        ensure!(changed == 1, "Task does not exist");
        Ok(())
    }
    pub fn remove_task(&self, id: &str, delete_media: bool) -> Result<()> {
        let mut db = self.connection.lock().unwrap();
        let transaction = db.transaction()?;
        transaction.execute("DELETE FROM tasks WHERE id=?1", [id])?;
        if delete_media {
            transaction.execute("DELETE FROM media WHERE task_id=?1", [id])?;
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
    pub fn complete(&self, task: &Task, media: &Media) -> Result<Media> {
        let mut db = self.connection.lock().unwrap();
        let transaction = db.transaction()?;
        let mut media = media.clone();
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
        transaction.execute("INSERT INTO media VALUES(?1,?2,?3) ON CONFLICT(task_id) DO UPDATE SET record=excluded.record",params![media.id,media.task_id,serde_json::to_string(&media)?])?;
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
    pub fn save_settings(&self, value: &Value) -> Result<()> {
        self.connection.lock().unwrap().execute(
            "UPDATE settings SET value=?1 WHERE id=1",
            [serde_json::to_string(value)?],
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
    fn conversion_publication_keeps_assets_added_after_its_read() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let db = Database::open(&temp.path().join("catalog.sqlite3"))?;
        let mut task = crate::tests::paused_task(Default::default());
        db.insert_task(&task)?;
        let media = Media {
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

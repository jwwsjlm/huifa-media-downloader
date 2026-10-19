use crate::App;
use anyhow::{Context, Result, ensure};
use serde::Serialize;
use serde_json::Value;
use std::{
    collections::HashSet,
    io::ErrorKind,
    path::{Component, Path, PathBuf},
    sync::{Arc, LazyLock},
    time::Duration,
};

const MAX_IDS: usize = 128;
static CHECKS: LazyLock<Arc<tokio::sync::Semaphore>> =
    LazyLock::new(|| Arc::new(tokio::sync::Semaphore::new(2)));

#[derive(Clone, Copy, Debug, PartialEq, Serialize)]
#[serde(rename_all = "lowercase")]
enum Status {
    Available,
    Missing,
    Unknown,
}

#[derive(Serialize)]
struct Availability {
    id: String,
    status: Status,
}

pub(crate) async fn check(app: &App, args: &Value) -> Result<Value> {
    let ids = args
        .get("ids")
        .and_then(Value::as_array)
        .context("Media IDs must be an array")?;
    ensure!(
        ids.len() <= MAX_IDS,
        "Check at most 128 media IDs at a time"
    );
    let mut seen = HashSet::new();
    let mut records = Vec::with_capacity(ids.len());
    for id in ids {
        let id = id
            .as_str()
            .filter(|id| !id.is_empty() && id.len() <= 256)
            .context("Media IDs must be nonempty strings")?;
        if seen.insert(id) {
            // Database methods return owned records, releasing their mutex before any filesystem call.
            records.push((
                id.to_owned(),
                app.db.media_by_id(id).ok().map(|media| media.path),
            ));
        }
    }
    let mut roots = app.download_roots();
    roots.push(app.config.data_dir.join("assets"));
    let unknown = records
        .iter()
        .map(|(id, _)| Availability {
            id: id.clone(),
            status: Status::Unknown,
        })
        .collect();
    let result = bounded_check(
        unknown,
        move || {
            records
                .into_iter()
                .map(|(id, path)| Availability {
                    id,
                    status: path.map_or(Status::Unknown, |path| path_status(&path, &roots)),
                })
                .collect()
        },
        CHECKS.clone(),
        Duration::from_secs(5),
    )
    .await?;
    Ok(serde_json::to_value(result)?)
}

async fn bounded_check(
    unknown: Vec<Availability>,
    work: impl FnOnce() -> Vec<Availability> + Send + 'static,
    checks: Arc<tokio::sync::Semaphore>,
    timeout: Duration,
) -> Result<Vec<Availability>> {
    let deadline = tokio::time::Instant::now() + timeout;
    let permit = match tokio::time::timeout_at(deadline, checks.acquire_owned()).await {
        Ok(Ok(permit)) => permit,
        _ => return Ok(unknown),
    };
    let worker = tokio::task::spawn_blocking(move || {
        // A timed-out OS stat cannot be cancelled. Keep its permit in the worker so
        // repeated requests cannot create an unbounded number of blocked threads.
        let _permit = permit;
        work()
    });
    match tokio::time::timeout_at(deadline, worker).await {
        Ok(result) => result.context("Could not check media availability"),
        Err(_) => Ok(unknown),
    }
}

fn path_status(path: &Path, roots: &[PathBuf]) -> Status {
    if !path.is_absolute()
        || path
            .components()
            .any(|component| matches!(component, Component::ParentDir))
    {
        return Status::Unknown;
    }
    let Some(root) = roots
        .iter()
        .filter(|root| path.starts_with(root))
        .max_by_key(|root| root.components().count())
    else {
        return Status::Unknown;
    };
    if crate::reject_links(path).is_err() {
        return Status::Unknown;
    }
    match std::fs::metadata(path) {
        Ok(metadata) if metadata.is_file() => Status::Available,
        Err(error) if error.kind() == ErrorKind::NotFound => {
            // An offline volume/share can also report NotFound. Require the saved storage root
            // and the immediate containing directory to be reachable before calling a file missing.
            let storage_reachable = std::fs::metadata(root).is_ok_and(|metadata| metadata.is_dir())
                && path
                    .parent()
                    .is_some_and(|parent| std::fs::read_dir(parent).is_ok());
            if storage_reachable
                && std::fs::metadata(path).is_err_and(|error| error.kind() == ErrorKind::NotFound)
            {
                Status::Missing
            } else {
                Status::Unknown
            }
        }
        _ => Status::Unknown,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[tokio::test]
    async fn stalled_filesystem_returns_unknown_without_spawning_more_workers() -> Result<()> {
        let checks = Arc::new(tokio::sync::Semaphore::new(1));
        let (release, wait) = std::sync::mpsc::channel();
        let (started, running) = tokio::sync::oneshot::channel();
        let unknown = || {
            vec![Availability {
                id: "media".into(),
                status: Status::Unknown,
            }]
        };
        let request = bounded_check(
            unknown(),
            move || {
                let _ = started.send(());
                let _ = wait.recv();
                vec![Availability {
                    id: "media".into(),
                    status: Status::Available,
                }]
            },
            checks.clone(),
            Duration::from_millis(100),
        );
        tokio::pin!(request);
        tokio::select! {
            _ = &mut request => anyhow::bail!("Filesystem worker did not start"),
            result = running => result?,
        }
        assert_eq!(request.await?[0].status, Status::Unknown);
        assert_eq!(checks.available_permits(), 0);
        let second = bounded_check(
            unknown(),
            || panic!("A second worker must not be spawned"),
            checks.clone(),
            Duration::from_millis(10),
        )
        .await?;
        assert_eq!(second[0].status, Status::Unknown);
        release.send(())?;
        let permit = tokio::time::timeout(Duration::from_secs(2), checks.acquire()).await??;
        drop(permit);
        Ok(())
    }

    #[test]
    fn missing_requires_a_reachable_storage_location_and_recovers_when_restored() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let root = temp.path().join("downloads");
        std::fs::create_dir(&root)?;
        let roots = vec![root.clone()];
        let path = root.join("video.mkv");
        assert_eq!(path_status(&path, &roots), Status::Missing);
        std::fs::write(&path, b"media")?;
        assert_eq!(path_status(&path, &roots), Status::Available);
        assert_eq!(path_status(&root, &roots), Status::Unknown);
        assert_eq!(
            path_status(&root.join("unreachable-folder/video.mkv"), &roots),
            Status::Unknown
        );
        assert_eq!(
            path_status(&temp.path().join("outside.mkv"), &roots),
            Status::Unknown
        );
        assert_eq!(
            path_status(Path::new("relative.mkv"), &roots),
            Status::Unknown
        );
        std::fs::remove_file(&path)?;
        std::fs::remove_dir(&root)?;
        assert_eq!(path_status(&path, &roots), Status::Unknown);
        Ok(())
    }

    #[cfg(unix)]
    #[test]
    fn missing_symlink_targets_are_unknown() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let link = temp.path().join("video.mkv");
        std::os::unix::fs::symlink(temp.path().join("offline-target.mkv"), &link)?;
        assert_eq!(path_status(&link, &[temp.path().into()]), Status::Unknown);
        Ok(())
    }

    #[tokio::test]
    async fn checks_only_requested_database_media_without_mutating_records() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let app = App::open(crate::AppConfig {
            data_dir: temp.path().join("data"),
            download_dir: temp.path().join("downloads"),
            tool_dir: None,
        })
        .await?;
        let mut task = crate::tests::paused_task(Default::default());
        task.status = "completed".into();
        let path = app.download_directory().join("saved.mkv");
        task.output_path = Some(path.clone());
        app.db.insert_task(&task)?;
        let media = crate::model::Media {
            id: "saved-media".into(),
            task_id: task.id.clone(),
            title: "Saved".into(),
            filename: "saved.mkv".into(),
            size: 5,
            duration: 1.0,
            path: path.clone(),
            assets: vec![],
            media_info: None,
        };
        app.db.complete(&task, &media)?;
        let before = app.db.media_by_id(&media.id)?.public();
        assert_eq!(
            check(&app, &json!({"ids":[media.id,"unknown-id",media.id]})).await?,
            json!([
                {"id":media.id,"status":"missing"}, {"id":"unknown-id","status":"unknown"}
            ])
        );
        std::fs::write(&path, b"media")?;
        assert_eq!(
            check(&app, &json!({"ids":[media.id]})).await?,
            json!([{"id":media.id,"status":"available"}])
        );
        assert_eq!(app.db.media_by_id(&media.id)?.public(), before);
        assert_eq!(app.db.task(&task.id)?.status, "completed");
        assert!(
            check(&app, &json!({"ids":vec!["id"; MAX_IDS + 1]}))
                .await
                .is_err()
        );
        assert!(check(&app, &json!({"ids":[42]})).await.is_err());
        assert!(check(&app, &json!({"path":path})).await.is_err());
        app.shutdown().await?;
        Ok(())
    }
}

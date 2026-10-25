use crate::{
    App,
    download::hash_file,
    model::{Task, text},
};
use anyhow::{Context, Result, bail, ensure};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::path::{Path, PathBuf};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub(crate) struct Occupied {
    pub path: PathBuf,
    pub hash: Option<String>,
    pub size: u64,
    pub owners: Vec<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub(crate) struct Destination {
    pub target: PathBuf,
    pub occupied: Vec<Occupied>,
    pub confirmed: bool,
}

impl Destination {
    pub(crate) fn public_conflict(&self) -> Option<Value> {
        (!self.confirmed && !self.occupied.is_empty()).then(|| json!({"filename":self.target.file_name().map(|v|v.to_string_lossy()),"files":self.occupied.iter().map(|file|file.path.file_name().unwrap_or_default().to_string_lossy()).collect::<Vec<_>>() }))
    }

    pub(crate) fn approves(&self, path: &Path) -> bool {
        self.confirmed && self.occupied.iter().any(|file| same_path(&file.path, path))
    }

    pub(crate) fn replaced_tasks(&self) -> Vec<String> {
        if !self.confirmed {
            return vec![];
        }
        self.occupied
            .iter()
            .flat_map(|file| file.owners.clone())
            .collect()
    }
}

fn same_path(left: &Path, right: &Path) -> bool {
    #[cfg(windows)]
    {
        left.to_string_lossy()
            .eq_ignore_ascii_case(&right.to_string_lossy())
    }
    #[cfg(not(windows))]
    {
        left == right
    }
}

fn same_family(path: &Path, target: &Path) -> bool {
    if !path
        .parent()
        .zip(target.parent())
        .is_some_and(|(a, b)| same_path(a, b))
    {
        return false;
    }
    let name = path.file_name().unwrap_or_default().to_string_lossy();
    let stem = target.file_stem().unwrap_or_default().to_string_lossy();
    #[cfg(windows)]
    {
        name.to_lowercase()
            .starts_with(&format!("{}.", stem.to_lowercase()))
    }
    #[cfg(not(windows))]
    {
        name.starts_with(&format!("{stem}."))
    }
}

fn conflicts(app: &App, task: &Task, target: &Path) -> Result<Vec<Occupied>> {
    crate::reject_links(target)?;
    let media = app.db.media()?;
    let own = media.iter().find(|media| media.task_id == task.id);
    let mut paths = Vec::new();
    if let Some(parent) = target.parent() {
        for entry in std::fs::read_dir(parent)? {
            let path = entry?.path();
            if same_family(&path, target) {
                paths.push(path);
            }
        }
    }
    for other in app.db.tasks_including_retained()? {
        if other.id != task.id
            && !matches!(
                other.status.as_str(),
                "failed" | "processing_failed" | "cancelled"
            )
        {
            if let Some(destination) = other.destination
                && same_family(&destination.target, target)
            {
                paths.push(destination.target);
            }
            if let Some(publication) = other.publication
                && same_family(&publication.target, target)
            {
                paths.push(publication.target);
            }
        }
    }
    // Replacing a managed main result also replaces its managed attachments and ownership.
    for other in &media {
        if other.task_id != task.id
            && (same_path(&other.path, target)
                || other.assets.iter().any(|asset| same_family(asset, target)))
        {
            paths.push(other.path.clone());
            paths.extend(other.assets.clone());
        }
    }
    paths.sort();
    paths.dedup();
    let mut result = Vec::new();
    for path in paths {
        if own.is_some_and(|own| {
            same_path(&own.path, &path) || own.assets.iter().any(|asset| same_path(asset, &path))
        }) {
            continue;
        }
        crate::reject_links(&path)?;
        let owners = media
            .iter()
            .filter(|media| {
                same_path(&media.path, &path)
                    || media.assets.iter().any(|asset| same_path(asset, &path))
            })
            .map(|media| media.task_id.clone())
            .collect::<Vec<_>>();
        let (hash, size) = if path.try_exists()? {
            ensure!(
                path.is_file(),
                "Destination conflict: a folder occupies the result filename"
            );
            let (hash, size) = hash_file(&path)?;
            (Some(hash), size)
        } else {
            (None, 0)
        };
        result.push(Occupied {
            path,
            hash,
            size,
            owners,
        });
    }
    Ok(result)
}

pub(crate) async fn reserve(
    app: &App,
    task: &mut Task,
    extension: &str,
    info: &Value,
) -> Result<()> {
    let _guard = app.publication_lock.lock().await;
    let title = text(info, "title");
    if !title.is_empty() {
        task.title = title.into();
    }
    if task.destination.is_none() {
        let root = app.task_download_directory(task)?;
        let directory = if task.options.organize_task_folder {
            root.join(crate::processing::sanitize_filename(&task.title))
        } else {
            root
        };
        crate::reject_links(&directory)?;
        std::fs::create_dir_all(&directory)?;
        let name = crate::processing::filename_with_id(
            task,
            extension,
            if text(info, "id").is_empty() {
                "video"
            } else {
                text(info, "id")
            },
        );
        task.destination = Some(Destination {
            target: directory.join(name),
            occupied: vec![],
            confirmed: false,
        });
    }
    let destination = task.destination.as_ref().unwrap();
    let found = conflicts(app, task, &destination.target)?;
    let approved = destination.confirmed
        && found.iter().all(|file| {
            destination.occupied.iter().any(|old| {
                same_path(&old.path, &file.path)
                    && old.hash == file.hash
                    && old.size == file.size
                    && old.owners == file.owners
            })
        });
    if !found.is_empty() && !approved {
        let destination = task.destination.as_mut().unwrap();
        destination.occupied = found;
        destination.confirmed = false;
        app.save_task(task)?;
        bail!(
            "Destination conflict: choose a new name, skip, or confirm replacing the existing files before downloading"
        );
    }
    app.save_task(task)
}

pub(crate) async fn resolve(app: &App, id: &str, args: &Value) -> Result<Value> {
    let _adding = app.task_add_lock.lock().await;
    let mut task = app.db.task(id)?;
    ensure!(
        matches!(
            task.status.as_str(),
            "failed" | "processing_failed" | "paused" | "cancelled"
        ),
        "Stop this task before resolving its destination"
    );
    let mut destination = task
        .destination
        .clone()
        .context("Task has no destination conflict")?;
    let choice = args["choice"]
        .as_str()
        .context("Choose rename, overwrite or skip")?;
    if choice == "skip" {
        task.stage("cancelled", "cancelled", None);
        task.error = None;
        app.save_task(&task)?;
        return Ok(task.public());
    }
    if choice == "rename" {
        let name = args["filename"]
            .as_str()
            .context("New filename is required")?
            .trim();
        ensure!(
            !name.is_empty()
                && name.len() <= 220
                && Path::new(name).file_name().is_some_and(|file| file == name)
                && crate::processing::sanitize_filename(name) == name,
            "Choose a valid filename without a folder"
        );
        ensure!(
            Path::new(name).extension() == destination.target.extension(),
            "Keep the result file extension"
        );
        destination.target.set_file_name(name);
        destination.confirmed = false;
        destination.occupied = conflicts(app, &task, &destination.target)?;
        ensure!(
            destination.occupied.is_empty(),
            "Destination conflict: the new name is also occupied"
        );
    } else {
        ensure!(
            choice == "overwrite" && args["confirmed"] == true,
            "Confirm replacing these existing files"
        );
        let found = conflicts(app, &task, &destination.target)?;
        ensure!(
            found
                .iter()
                .all(|file| destination.occupied.iter().any(|old| same_path(
                    &old.path, &file.path
                ) && old.hash == file.hash
                    && old.size == file.size
                    && old.owners == file.owners)),
            "Destination changed; reopen the conflict dialog before confirming"
        );
        for other in app.db.tasks_including_retained()? {
            if other.id != task.id
                && !matches!(
                    other.status.as_str(),
                    "completed" | "retained" | "failed" | "cancelled" | "processing_failed"
                )
                && (other
                    .destination
                    .as_ref()
                    .is_some_and(|other| same_family(&other.target, &destination.target))
                    || other
                        .publication
                        .as_ref()
                        .is_some_and(|other| same_family(&other.target, &destination.target)))
            {
                bail!(
                    "Another active task uses this filename; choose a different name or wait for it to finish"
                );
            }
        }
        destination.occupied = found;
        destination.confirmed = true;
    }
    task.destination = Some(destination);
    task.error = None;
    task.stage(
        if task.manifest.is_some() {
            "waiting_processing"
        } else {
            "queued"
        },
        if task.manifest.is_some() {
            "waiting_processing"
        } else {
            "queued"
        },
        None,
    );
    app.save_task(&task)?;
    app.schedule(id)?;
    Ok(task.public())
}

pub(crate) fn validate_approval(task: &Task, path: &Path) -> Result<bool> {
    let Some(destination) = task
        .destination
        .as_ref()
        .filter(|destination| destination.confirmed)
    else {
        return Ok(false);
    };
    let Some(approval) = destination
        .occupied
        .iter()
        .find(|file| same_path(&file.path, path))
    else {
        return Ok(false);
    };
    ensure!(
        match &approval.hash {
            Some(hash) => hash_file(path)
                .is_ok_and(|(current, size)| current == *hash && size == approval.size),
            None => !path.exists(),
        },
        "Destination changed after confirmation; original files were preserved"
    );
    Ok(true)
}

pub(crate) fn finish_replacement(app: &App, task: &Task, current: &[PathBuf]) -> Result<()> {
    if let Some(destination) = task.destination.as_ref().filter(|value| value.confirmed) {
        for old in &destination.occupied {
            if current.iter().any(|path| same_path(path, &old.path)) || !old.path.exists() {
                continue;
            }
            if old.hash.as_ref().is_some_and(|hash| {
                hash_file(&old.path).is_ok_and(|(actual, size)| actual == *hash && size == old.size)
            }) && !app
                .db
                .media()?
                .iter()
                .any(|media| same_path(&media.path, &old.path) || media.assets.contains(&old.path))
            {
                app.remove_owned(&old.path)?;
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::Media;

    #[tokio::test]
    async fn conflicts_require_confirmation_and_transfer_ownership_only_on_commit() -> Result<()> {
        let root = tempfile::tempdir()?;
        let app = App::open(crate::AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("output"),
            tool_dir: None,
        })
        .await?;
        let _network = app.metadata.clone().acquire_many_owned(3).await?;
        let mut old = crate::tests::paused_task(Default::default());
        old.title = "Same title".into();
        old.stage("completed", "completed", Some(100.0));
        let target = app.download_directory().join("Same title.mp4");
        let sidecar = target.with_extension("en.srt");
        std::fs::write(&target, b"original video")?;
        std::fs::write(&sidecar, b"original subtitle")?;
        old.output_path = Some(target.clone());
        app.db.insert_task(&old)?;
        let media = Media {
            id: "old-media".into(),
            task_id: old.id.clone(),
            title: old.title.clone(),
            filename: "Same title.mp4".into(),
            size: 14,
            duration: 1.0,
            path: target.clone(),
            assets: vec![sidecar.clone()],
            media_info: None,
        };
        app.db.complete(&old, &media)?;
        let mut next = crate::tests::paused_task(Default::default());
        next.title = old.title.clone();
        next.options.organize_task_folder = false;
        app.db.insert_task(&next)?;
        let metadata = json!({"title":next.title,"id":"different-source"});
        assert!(
            reserve(&app, &mut next, "mp4", &metadata)
                .await
                .unwrap_err()
                .to_string()
                .starts_with("Destination conflict:")
        );
        assert_eq!(std::fs::read(&target)?, b"original video");
        assert!(
            !next
                .public()
                .to_string()
                .contains(root.path().to_string_lossy().as_ref())
        );
        assert!(
            resolve(&app, &next.id, &json!({"choice":"overwrite"}))
                .await
                .is_err()
        );
        assert!(
            resolve(
                &app,
                &next.id,
                &json!({"choice":"rename","filename":"../bad.mp4"})
            )
            .await
            .is_err()
        );
        resolve(
            &app,
            &next.id,
            &json!({"choice":"overwrite","confirmed":true}),
        )
        .await?;
        let mut next = app.db.task(&next.id)?;
        assert!(validate_approval(&next, &target)?);
        std::fs::write(&target, b"user edit")?;
        assert!(validate_approval(&next, &target).is_err());
        std::fs::write(&target, b"original video")?;
        assert!(app.db.media_for_task(&old.id)?.is_some());
        next.stage("processing", "saving_results", None);
        let replacement = Media {
            id: "new-media".into(),
            task_id: next.id.clone(),
            title: next.title.clone(),
            filename: "Same title.mp4".into(),
            size: 9,
            duration: 1.0,
            path: target.clone(),
            assets: vec![],
            media_info: None,
        };
        app.db.complete(&next, &replacement)?;
        assert!(app.db.media_for_task(&old.id)?.is_none());
        assert!(app.db.task(&old.id)?.output_path.is_none());
        assert_eq!(app.db.task(&old.id)?.stage, "result_replaced");
        finish_replacement(&app, &next, std::slice::from_ref(&target))?;
        assert!(target.exists());
        assert!(!sidecar.exists());
        app.shutdown().await?;
        Ok(())
    }

    #[tokio::test]
    async fn concurrent_reservations_detect_shared_sidecars_before_transfer() -> Result<()> {
        let root = tempfile::tempdir()?;
        let app = App::open(crate::AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("output"),
            tool_dir: None,
        })
        .await?;
        let mut first = crate::tests::paused_task(Default::default());
        first.title = "same".into();
        first.options.organize_task_folder = false;
        first.status = "queued".into();
        let mut second = crate::tests::paused_task(Default::default());
        second.title = first.title.clone();
        second.options.organize_task_folder = false;
        app.db.insert_task(&first)?;
        app.db.insert_task(&second)?;
        reserve(&app, &mut first, "mp4", &json!({"title":"same"})).await?;
        assert!(
            reserve(&app, &mut second, "mkv", &json!({"title":"same"}))
                .await
                .is_err()
        );
        assert!(
            app.db
                .task(&second.id)?
                .destination
                .unwrap()
                .public_conflict()
                .is_some()
        );
        app.shutdown().await?;
        Ok(())
    }

    #[tokio::test]
    async fn overwrite_rejects_active_family_with_a_different_extension() -> Result<()> {
        for publication_only in [false, true] {
            let root = tempfile::tempdir()?;
            let app = App::open(crate::AppConfig {
                data_dir: root.path().join("data"),
                download_dir: root.path().join("output"),
                tool_dir: None,
            })
            .await?;
            let target = app.download_directory().join("same.webm");
            std::fs::write(&target, b"active result")?;
            let mut active = crate::tests::paused_task(Default::default());
            active.stage("processing", "saving_results", None);
            if publication_only {
                active.publication = Some(crate::model::Publication {
                    committed: false,
                    temporary: target.with_extension("partial"),
                    target: target.clone(),
                    hash: String::new(),
                    size: 0,
                    duration: 1.0,
                    auxiliary: vec![],
                    backup: None,
                });
            } else {
                active.destination = Some(Destination {
                    target: target.clone(),
                    occupied: vec![],
                    confirmed: false,
                });
            }
            app.db.insert_task(&active)?;
            let mut waiting = crate::tests::paused_task(Default::default());
            waiting.options.organize_task_folder = false;
            app.db.insert_task(&waiting)?;
            assert!(
                reserve(&app, &mut waiting, "mp4", &json!({"title":"same"}))
                    .await
                    .is_err()
            );
            let error = resolve(
                &app,
                &waiting.id,
                &json!({"choice":"overwrite","confirmed":true}),
            )
            .await
            .unwrap_err();
            assert!(
                error
                    .to_string()
                    .starts_with("Another active task uses this filename")
            );
            assert_eq!(app.db.task(&waiting.id)?.status, "paused");
            assert!(!app.db.task(&waiting.id)?.destination.unwrap().confirmed);
            assert_eq!(app.db.task(&active.id)?.status, "processing");
            assert_eq!(std::fs::read(&target)?, b"active result");
            assert!(app.runners.lock().unwrap().is_empty());
            app.shutdown().await?;
        }
        Ok(())
    }
}

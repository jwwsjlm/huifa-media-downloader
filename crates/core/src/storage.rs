use crate::{App, model::Task};
use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};
use std::path::{Path, PathBuf};

pub(crate) fn available(path: &Path) -> Result<u64> {
    let mut existing = path;
    while !existing.exists() {
        existing = existing.parent().context("Storage volume is unavailable")?;
    }
    crate::reject_links(existing)?;
    #[cfg(windows)]
    {
        use std::os::windows::ffi::OsStrExt;
        let path = existing
            .as_os_str()
            .encode_wide()
            .chain(Some(0))
            .collect::<Vec<_>>();
        let mut free = 0;
        ensure!(
            unsafe {
                windows_sys::Win32::Storage::FileSystem::GetDiskFreeSpaceExW(
                    path.as_ptr(),
                    &mut free,
                    std::ptr::null_mut(),
                    std::ptr::null_mut(),
                )
            } != 0,
            "Cannot read free disk space"
        );
        Ok(free)
    }
    #[cfg(unix)]
    {
        use std::os::unix::ffi::OsStrExt;
        let path = std::ffi::CString::new(existing.as_os_str().as_bytes())?;
        let mut stat = std::mem::MaybeUninit::<libc::statvfs>::uninit();
        ensure!(
            unsafe { libc::statvfs(path.as_ptr(), stat.as_mut_ptr()) } == 0,
            "Cannot read free disk space"
        );
        let stat = unsafe { stat.assume_init() };
        let bytes = u128::from(stat.f_bavail) * u128::from(stat.f_frsize);
        Ok(u64::try_from(bytes).unwrap_or(u64::MAX))
    }
}

fn require_space(path: &Path, required: u64, purpose: &str) -> Result<()> {
    let free = available(path)?;
    ensure!(
        free >= required.saturating_add(64 * 1024 * 1024),
        "Insufficient disk space for {purpose}: need at least {} MiB, available {} MiB. Free space or change the download/temporary folder, then retry",
        required / (1024 * 1024),
        free / (1024 * 1024)
    );
    Ok(())
}

pub(crate) fn before_download(app: &App, task: &Task, expected: u64) -> Result<()> {
    // Sources, processing and publication can coexist, including on the same volume.
    let required = expected.saturating_mul(3);
    require_space(&app.config.data_dir, required, "download sources")?;
    require_space(
        &crate::processing::workspace(app, task),
        required,
        "media processing",
    )?;
    require_space(
        &app.task_download_directory(task)?,
        required,
        "saved results",
    )
}

pub(crate) fn before_publication(path: &Path, size: u64) -> Result<()> {
    require_space(
        path.parent().context("Result has no parent folder")?,
        size,
        "saving results",
    )
}

fn cache_paths(app: &App, task: &Task) -> Vec<PathBuf> {
    vec![
        app.config
            .data_dir
            .join("tasks")
            .join(&task.id)
            .join("sources"),
        crate::processing::workspace(app, task),
    ]
}

fn remove_cache_directory(path: &Path) -> Result<()> {
    crate::reject_links(path)?;
    let parent = path
        .parent()
        .context("Cache has no parent")?
        .canonicalize()?;
    let canonical = path.canonicalize()?;
    ensure!(
        canonical.is_dir() && canonical.parent() == Some(parent.as_path()),
        "Cache resolved outside its expected parent"
    );
    std::fs::remove_dir_all(canonical)?;
    Ok(())
}

pub(crate) fn clean_completed(app: &App, task: &Task) -> Result<()> {
    ensure!(
        matches!(task.status.as_str(), "completed" | "retained"),
        "Only completed caches can be cleaned"
    );
    for path in cache_paths(app, task) {
        if path.try_exists()? {
            crate::reject_links(&path)?;
            // Never remove a published result even if an old configuration placed it in a workspace.
            ensure!(
                !task
                    .output_path
                    .as_ref()
                    .is_some_and(|output| output.starts_with(&path)),
                "Saved output is inside its cache; cache was preserved"
            );
            for media in app.db.media()? {
                ensure!(
                    !media.path.starts_with(&path)
                        && !media.assets.iter().any(|asset| asset.starts_with(&path)),
                    "A published file is inside this cache; cache was preserved"
                );
            }
            remove_cache_directory(&path)?;
        }
    }
    Ok(())
}

fn size(path: &Path) -> Result<u64> {
    if !path.try_exists()? {
        return Ok(0);
    }
    crate::reject_links(path)?;
    let metadata = std::fs::symlink_metadata(path)?;
    if metadata.is_file() {
        return Ok(metadata.len());
    }
    let mut bytes = 0u64;
    for entry in std::fs::read_dir(path)? {
        let entry = entry?;
        if entry.file_type()?.is_symlink() {
            continue;
        }
        bytes = bytes.saturating_add(size(&entry.path())?);
    }
    Ok(bytes)
}

pub(crate) fn status(app: &App) -> Result<Value> {
    let mut completed = 0u64;
    let mut recoverable = 0u64;
    for task in app.db.tasks_including_retained()? {
        for path in cache_paths(app, &task) {
            let bytes = size(&path)?;
            if matches!(task.status.as_str(), "completed" | "retained") {
                completed = completed.saturating_add(bytes);
            } else {
                recoverable = recoverable.saturating_add(bytes);
            }
        }
    }
    let settings = app.db.settings()?;
    let processing = settings["downloadOptions"]["processingTempDir"]
        .as_str()
        .filter(|path| !path.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| app.config.data_dir.clone());
    Ok(
        json!({"dataFree":available(&app.config.data_dir)?,"downloadFree":available(&app.download_directory())?,"processingFree":available(&processing)?,"completedCacheBytes":completed,"recoverableCacheBytes":recoverable,"unusedToolGroups":crate::tools::unused_cache(app)?.len()}),
    )
}

pub(crate) async fn cleanup(app: &App) -> Result<Value> {
    let _adding = app.task_add_lock.lock().await;
    let _tools = app
        .tool_update_lock
        .try_lock()
        .context("Runtime tools are in use or updating; try cleaning up again shortly")?;
    let _publication = app.publication_lock.lock().await;
    let mut cleaned = 0;
    let mut failures = Vec::new();
    for task in app.db.tasks_including_retained()? {
        if matches!(task.status.as_str(), "completed" | "retained") {
            match clean_completed(app, &task) {
                Ok(()) => cleaned += 1,
                Err(_) => failures.push(task.id),
            }
        }
    }
    let mut tool_groups = 0;
    for path in crate::tools::unused_cache(app)? {
        remove_cache_directory(&path)?;
        tool_groups += 1;
    }
    let unused_assets = crate::cover::clean_unused_assets(app)?;
    Ok(
        json!({"cleaned":cleaned,"toolGroups":tool_groups,"unusedAssets":unused_assets,"failedTaskIds":failures,"storage":status(app)?}),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn busy_runtime_cleanup_preserves_caches_and_does_not_block_cancellation() -> Result<()> {
        let root = tempfile::tempdir()?;
        let app = App::open(crate::AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("output"),
            tool_dir: None,
        })
        .await?;
        let mut completed = crate::tests::paused_task(Default::default());
        completed.status = "completed".into();
        app.db.insert_task(&completed)?;
        for path in cache_paths(&app, &completed) {
            std::fs::create_dir_all(&path)?;
            std::fs::write(path.join("cached"), b"cached media")?;
        }
        let obsolete = app.config.data_dir.join("tools/yt-dlp-2222222222222222");
        std::fs::create_dir_all(&obsolete)?;
        std::fs::write(obsolete.join("payload"), b"runtime")?;
        let task = crate::tests::paused_task(Default::default());
        app.db.insert_task(&task)?;
        let tools = app.tool_update_lock.lock().await;
        let error = tokio::time::timeout(
            std::time::Duration::from_secs(1),
            app.command("storage.cleanup", json!({})),
        )
        .await?
        .unwrap_err();
        assert!(error.to_string().contains("try cleaning up again"));
        assert!(
            cache_paths(&app, &completed)
                .iter()
                .all(|path| path.join("cached").exists())
        );
        assert_eq!(std::fs::read(obsolete.join("payload"))?, b"runtime");
        tokio::time::timeout(
            std::time::Duration::from_secs(1),
            app.command("tasks.cancel", json!({"id":task.id})),
        )
        .await??;
        assert_eq!(app.db.task(&task.id)?.status, "cancelled");
        drop(tools);
        let report = cleanup(&app).await?;
        assert_eq!(report["storage"]["completedCacheBytes"], 0);
        assert!(!obsolete.exists());
        app.shutdown().await?;
        Ok(())
    }

    #[tokio::test]
    async fn completed_cleanup_keeps_results_previews_and_failed_sources() -> Result<()> {
        let root = tempfile::tempdir()?;
        let app = App::open(crate::AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("output"),
            tool_dir: None,
        })
        .await?;
        let mut completed = crate::tests::paused_task(Default::default());
        completed.status = "completed".into();
        let result = app.download_directory().join("result.mp4");
        std::fs::write(&result, b"keep result")?;
        completed.output_path = Some(result.clone());
        app.db.insert_task(&completed)?;
        let failed = crate::tests::paused_task(Default::default());
        app.db.insert_task(&failed)?;
        for task in [&completed, &failed] {
            for path in cache_paths(&app, task) {
                std::fs::create_dir_all(&path)?;
                std::fs::write(path.join("cached"), b"cached media")?;
            }
        }
        let preview = app
            .config
            .data_dir
            .join("tasks")
            .join(&completed.id)
            .join("preview");
        std::fs::create_dir_all(&preview)?;
        std::fs::write(preview.join("thumbnail.jpg"), b"preview")?;
        let report = cleanup(&app).await?;
        assert_eq!(report["storage"]["completedCacheBytes"], 0);
        assert_eq!(std::fs::read(result)?, b"keep result");
        assert!(preview.join("thumbnail.jpg").exists());
        assert!(
            cache_paths(&app, &failed)
                .iter()
                .all(|path| path.join("cached").exists())
        );
        assert!(require_space(root.path(), u64::MAX, "fixture").is_err());
        app.shutdown().await?;
        Ok(())
    }

    #[tokio::test]
    async fn runtime_cleanup_preserves_current_and_task_references() -> Result<()> {
        let root = tempfile::tempdir()?;
        let app = App::open(crate::AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("output"),
            tool_dir: None,
        })
        .await?;
        let tools = app.config.data_dir.join("tools");
        let kept = tools.join("frozen").join("a".repeat(64));
        let unused = tools.join("frozen").join("b".repeat(64));
        let kept_object = tools.join("objects").join("c".repeat(64));
        let unused_object = tools.join("objects").join("d".repeat(64));
        let installed = tools.join("yt-dlp-1111111111111111");
        let obsolete = tools.join("yt-dlp-2222222222222222");
        for directory in [
            &kept,
            &unused,
            &kept_object,
            &unused_object,
            &installed,
            &obsolete,
        ] {
            std::fs::create_dir_all(directory)?;
            std::fs::write(directory.join("payload"), b"fixture")?;
        }
        std::fs::write(
            kept.join("manifest.json"),
            serde_json::to_vec(&json!({"yt-dlp":"c".repeat(64)}))?,
        )?;
        std::fs::write(
            tools.join("current.json"),
            br#"{"yt-dlp":"yt-dlp-1111111111111111/payload"}"#,
        )?;
        let mut task = crate::tests::paused_task(Default::default());
        task.tools
            .insert("yt-dlp".into(), kept.join("payload").canonicalize()?);
        app.db.insert_task(&task)?;
        cleanup(&app).await?;
        assert!(kept.exists() && kept_object.exists() && installed.exists());
        assert!(!unused.exists() && !unused_object.exists() && !obsolete.exists());
        app.shutdown().await?;
        Ok(())
    }
}

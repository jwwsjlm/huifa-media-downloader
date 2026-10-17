use crate::{App, model::Task};
use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};
use std::{
    io::Write,
    path::{Path, PathBuf},
    sync::Mutex,
};

pub(crate) const FILE_NAME: &str = "download-directory.json";

#[derive(Clone, Serialize, Deserialize)]
struct Directories {
    current: PathBuf,
    roots: Vec<PathBuf>,
}

pub(crate) struct DownloadDirectories {
    path: PathBuf,
    directories: Mutex<Directories>,
}

impl DownloadDirectories {
    pub(crate) fn open(data_dir: &Path, default: &Path) -> Result<Self> {
        let path = data_dir.join(FILE_NAME);
        crate::reject_links(&path)?;
        let mut directories = if path.exists() {
            serde_json::from_slice::<Directories>(&std::fs::read(&path)?)
                .context("Could not read the saved download folder")?
        } else {
            Directories {
                current: default.into(),
                roots: vec![],
            }
        };
        ensure!(
            directories.current.is_absolute()
                && directories.roots.iter().all(|root| root.is_absolute()),
            "Saved download folders must be absolute paths"
        );
        if !directories.roots.contains(&directories.current) {
            directories.roots.push(directories.current.clone());
        }
        if !directories.roots.iter().any(|root| root == default) {
            directories.roots.push(default.into());
        }
        Ok(Self {
            path,
            directories: Mutex::new(directories),
        })
    }

    fn current(&self) -> PathBuf {
        self.directories.lock().unwrap().current.clone()
    }

    fn roots(&self) -> Vec<PathBuf> {
        self.directories.lock().unwrap().roots.clone()
    }

    fn set(&self, path: &Path) -> Result<PathBuf> {
        ensure!(path.is_absolute(), "Choose an absolute download folder");
        crate::reject_links(path)?;
        let path = path
            .canonicalize()
            .context("Download folder does not exist")?;
        ensure!(path.is_dir(), "Download folder must be a directory");
        let probe = tempfile::Builder::new()
            .prefix(".huifa-write-check-")
            .tempfile_in(&path)
            .context("Download folder is not writable")?;
        probe
            .close()
            .context("Could not remove the download folder write check")?;
        let mut directories = self.directories.lock().unwrap();
        let mut next = directories.clone();
        next.current = path.clone();
        if !next.roots.contains(&path) {
            next.roots.push(path.clone());
        }
        crate::reject_links(&self.path)?;
        let mut file = tempfile::Builder::new()
            .prefix(".download-directory-")
            .tempfile_in(self.path.parent().unwrap())?;
        serde_json::to_writer(&mut file, &next)?;
        file.flush()?;
        file.as_file().sync_all()?;
        file.persist(&self.path)
            .context("Could not save the download folder")?;
        *directories = next;
        Ok(path)
    }
}

impl App {
    pub fn download_directory(&self) -> PathBuf {
        self.download_directories.current()
    }

    // Only the desktop host exposes this operation, outside the shared command API.
    pub fn set_download_directory(&self, path: &Path) -> Result<PathBuf> {
        self.download_directories.set(path)
    }

    pub(crate) fn task_download_directory(&self, task: &Task) -> Result<PathBuf> {
        let path = task
            .download_dir
            .as_ref()
            .unwrap_or(&self.config.download_dir);
        ensure!(
            self.download_directories.roots().contains(path),
            "Task download folder is not application-managed"
        );
        crate::reject_links(path)?;
        Ok(path.clone())
    }

    pub(crate) fn owns_download_path(&self, path: &Path) -> bool {
        self.download_directories
            .roots()
            .iter()
            .any(|root| path.starts_with(root))
    }

    pub(crate) fn download_roots(&self) -> Vec<PathBuf> {
        self.download_directories.roots()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{
        AppConfig,
        model::{SourceFile, SourceManifest},
    };
    use serde_json::json;
    use tokio_util::sync::CancellationToken;

    #[tokio::test]
    async fn saved_download_folder_preserves_legacy_files_and_publishes_new_assets() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let config = AppConfig {
            data_dir: temp.path().join("data"),
            download_dir: temp.path().join("old"),
            tool_dir: None,
        };
        let app = App::open(config.clone()).await?;
        let old = app.download_directory();
        let old_file = old.join("old.mp4");
        std::fs::write(&old_file, b"old media")?;
        let mut legacy = crate::tests::paused_task(Default::default());
        legacy.output_path = Some(old_file.clone());
        app.db.insert_task(&legacy)?;
        app.shutdown().await?;
        drop(app);

        let app = App::open(config.clone()).await?;
        let selected = temp.path().join("new");
        std::fs::create_dir(&selected)?;
        let selected = app.set_download_directory(&selected)?;
        assert_eq!(app.task_download_directory(&app.db.task(&legacy.id)?)?, old);
        assert_eq!(app.owned_path(&old_file)?, old_file);
        assert_eq!(std::fs::read(&old_file)?, b"old media");

        let mut task = crate::tests::paused_task(Default::default());
        task.kind = "assets".into();
        task.title = "New assets".into();
        task.download_dir = Some(app.download_directory());
        let directory = app
            .config
            .data_dir
            .join("tasks")
            .join(&task.id)
            .join("sources");
        std::fs::create_dir_all(&directory)?;
        let source = directory.join("notes.txt");
        std::fs::write(&source, b"new notes")?;
        let (hash, size) = crate::download::hash_file(&source)?;
        task.manifest = Some(SourceManifest {
            version: 1,
            directory,
            files: vec![SourceFile {
                path: source,
                hash,
                size,
                kind: "description".into(),
                format_id: String::new(),
                language: None,
            }],
            metadata: json!({"title":task.title}),
            duration: None,
            chapters: vec![],
            sponsor_segments: vec![],
            output_extension: String::new(),
        });
        app.db.insert_task(&task)?;
        crate::processing::process_task(&app, &task.id, &CancellationToken::new()).await?;
        let completed = app.db.task(&task.id)?;
        assert_eq!(
            completed.output_path.as_ref().unwrap().parent().unwrap(),
            selected
        );
        assert_eq!(
            std::fs::read(completed.output_path.as_ref().unwrap())?,
            b"new notes"
        );
        assert!(
            !completed
                .public()
                .to_string()
                .contains(temp.path().to_string_lossy().as_ref())
        );
        app.shutdown().await?;
        drop(app);
        let app = App::open(config).await?;
        assert_eq!(app.download_directory(), selected);
        assert!(app.owned_path(&old_file).is_ok());
        assert!(
            app.owned_path(completed.output_path.as_ref().unwrap())
                .is_ok()
        );
        assert_eq!(app.task_download_directory(&app.db.task(&legacy.id)?)?, old);
        assert!(
            !app.research_error(&format!("{}: sample", selected.display()))
                .contains(selected.to_string_lossy().as_ref())
        );
        assert!(
            app.command("host.download_directory", json!({"path":old}))
                .await
                .is_err()
        );
        app.shutdown().await?;
        Ok(())
    }

    #[tokio::test]
    async fn new_downloads_snapshot_the_selected_folder_and_restart_existing_records() -> Result<()>
    {
        let temp = tempfile::tempdir()?;
        let tools = temp.path().join("tools");
        std::fs::create_dir(&tools)?;
        for name in ["yt-dlp", "ffmpeg", "ffprobe", "qjs"] {
            std::fs::write(
                tools.join(format!("{name}{}", std::env::consts::EXE_SUFFIX)),
                format!("fixture {name}"),
            )?;
        }
        let app = App::open(AppConfig {
            data_dir: temp.path().join("data"),
            download_dir: temp.path().join("old"),
            tool_dir: Some(tools),
        })
        .await?;
        let network = app.network.acquire_many(3).await?;
        let ids = app
            .command(
                "tasks.add",
                json!({"urls":["https://example.test/first.mp4"]}),
            )
            .await?;
        let id = ids[0].as_str().unwrap();
        let old = app.db.task(id)?.download_dir.unwrap();
        let selected = temp.path().join("new");
        std::fs::create_dir(&selected)?;
        let selected = app.set_download_directory(&selected)?;
        let ids = app
            .command(
                "tasks.add",
                json!({"urls":["https://example.test/second.mp4"]}),
            )
            .await?;
        assert_eq!(
            app.db.task(ids[0].as_str().unwrap())?.download_dir.as_ref(),
            Some(&selected)
        );
        assert_eq!(app.db.task(id)?.download_dir.as_ref(), Some(&old));
        app.command("tasks.cancel", json!({"id":id})).await?;
        let restarted = app
            .command(
                "tasks.add",
                json!({"urls":["https://example.test/first.mp4"]}),
            )
            .await?;
        assert_eq!(restarted[0], id);
        assert_eq!(app.db.task(id)?.download_dir.as_ref(), Some(&selected));
        let public = app
            .command("snapshot", json!({"includeTools":false}))
            .await?;
        assert!(
            !public
                .to_string()
                .contains(selected.to_string_lossy().as_ref())
        );
        drop(network);
        app.shutdown().await?;
        Ok(())
    }

    #[tokio::test]
    async fn saved_folder_keeps_startup_available_when_media_roots_are_unavailable() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let config = AppConfig {
            data_dir: temp.path().join("data"),
            download_dir: temp.path().join("old"),
            tool_dir: None,
        };
        let app = App::open(config.clone()).await?;
        let selected = temp.path().join("selected");
        std::fs::create_dir(&selected)?;
        let selected = app.set_download_directory(&selected)?;
        app.shutdown().await?;
        drop(app);
        std::fs::remove_dir(&config.download_dir)?;
        std::fs::write(&config.download_dir, b"default folder is unavailable")?;
        let app = App::open(config.clone()).await?;
        assert_eq!(app.download_directory(), selected);
        app.shutdown().await?;
        drop(app);
        std::fs::remove_dir(&selected)?;
        std::fs::write(&selected, b"selected folder is unavailable")?;
        let app = App::open(config).await?;
        assert_eq!(app.download_directory(), selected);
        let replacement = temp.path().join("replacement");
        std::fs::create_dir(&replacement)?;
        assert_eq!(
            app.set_download_directory(&replacement)?,
            replacement.canonicalize()?
        );
        app.shutdown().await?;
        Ok(())
    }

    #[tokio::test]
    async fn invalid_or_failed_folder_saves_keep_the_previous_selection() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let app = App::open(AppConfig {
            data_dir: temp.path().join("data"),
            download_dir: temp.path().join("old"),
            tool_dir: None,
        })
        .await?;
        let old = app.download_directory();
        let file = temp.path().join("file.txt");
        std::fs::write(&file, b"not a folder")?;
        for path in [PathBuf::from("relative"), temp.path().join("missing"), file] {
            assert!(app.set_download_directory(&path).is_err());
            assert_eq!(app.download_directory(), old);
        }
        let new = temp.path().join("new");
        std::fs::create_dir(&new)?;
        std::fs::create_dir(app.config.data_dir.join("download-directory.json"))?;
        assert!(app.set_download_directory(&new).is_err());
        assert_eq!(app.download_directory(), old);
        assert!(!app.owns_download_path(&new.canonicalize()?));
        app.shutdown().await?;
        Ok(())
    }
}

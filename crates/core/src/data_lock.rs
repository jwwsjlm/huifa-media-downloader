use anyhow::{Context, Result};
use std::{fs::File, path::Path};

pub(crate) fn acquire(directory: &Path) -> Result<File> {
    let path = directory.join(".application.lock");
    crate::reject_links(&path)?;
    let mut options = File::options();
    options.read(true).write(true).create(true).truncate(false);
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        options.share_mode(0);
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600).custom_flags(libc::O_NOFOLLOW);
    }
    let file = options.open(&path).with_context(|| {
        format!(
            "Cannot lock application data folder {}; close other Huifa instances using it",
            directory.display()
        )
    })?;
    #[cfg(unix)]
    {
        use std::os::fd::AsRawFd;
        if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
            return Err(std::io::Error::last_os_error()).with_context(|| {
                format!(
                    "Cannot lock application data folder {}; close other Huifa instances using it",
                    directory.display()
                )
            });
        }
    }
    Ok(file)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn only_one_app_can_open_a_data_directory() -> Result<()> {
        let root = tempfile::tempdir()?;
        let config = crate::AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("downloads"),
            tool_dir: None,
        };
        let app = crate::App::open(config.clone()).await?;
        let error = crate::App::open(config.clone()).await.err().unwrap();
        assert!(error.to_string().contains("close other Huifa instances"));
        app.shutdown().await?;
        assert!(crate::App::open(config.clone()).await.is_err());
        drop(app);
        let reopened = crate::App::open(config).await?;
        reopened.shutdown().await?;
        Ok(())
    }

    #[tokio::test]
    async fn startup_removes_interrupted_download_secrets_only_after_locking() -> Result<()> {
        let root = tempfile::tempdir()?;
        let config = crate::AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("downloads"),
            tool_dir: None,
        };
        std::fs::create_dir_all(config.data_dir.join("tasks/sources"))?;
        for name in [".cookie-Ab12Cd", ".download-info-Ef34Gh"] {
            std::fs::write(config.data_dir.join(name), b"interrupted secret")?;
        }
        for name in [
            ".cookie-notes.txt",
            "tasks/sources/download-checkpoint.json",
        ] {
            std::fs::write(config.data_dir.join(name), b"keep")?;
        }
        let directory = config.data_dir.join(".download-info-folder");
        std::fs::create_dir(&directory)?;
        let app = crate::App::open(config.clone()).await?;
        for name in [".cookie-Ab12Cd", ".download-info-Ef34Gh"] {
            assert!(!config.data_dir.join(name).exists());
        }
        for name in [
            ".cookie-notes.txt",
            "tasks/sources/download-checkpoint.json",
        ] {
            assert_eq!(std::fs::read(config.data_dir.join(name))?, b"keep");
        }
        assert!(directory.is_dir());
        let active = config.data_dir.join(".cookie-Live12");
        std::fs::write(&active, b"active secret")?;
        assert!(crate::App::open(config.clone()).await.is_err());
        assert_eq!(std::fs::read(&active)?, b"active secret");
        app.shutdown().await?;
        drop(app);
        let reopened = crate::App::open(config).await?;
        assert!(!active.exists());
        reopened.shutdown().await?;
        Ok(())
    }
}

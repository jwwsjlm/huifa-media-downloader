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
}

use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use tokio::sync::watch;

pub struct Initialization {
    receiver: watch::Receiver<Option<Result<(), String>>>,
    pub closing: AtomicBool,
}

impl Initialization {
    pub fn new() -> (Self, watch::Sender<Option<Result<(), String>>>) {
        let (sender, receiver) = watch::channel(None);
        (
            Self {
                receiver,
                closing: AtomicBool::new(false),
            },
            sender,
        )
    }

    pub async fn wait(&self) -> Result<(), String> {
        let mut receiver = self.receiver.clone();
        loop {
            if let Some(result) = receiver.borrow_and_update().clone() {
                return result;
            }
            if receiver.changed().await.is_err() {
                return receiver.borrow().clone().unwrap_or_else(|| {
                    Err("Application initialization stopped before completion".into())
                });
            }
        }
    }

    pub fn request_close(&self) -> bool {
        !self.closing.swap(true, Ordering::AcqRel)
    }

    pub fn is_closing(&self) -> bool {
        self.closing.load(Ordering::Acquire)
    }

    pub fn failed(&self) -> bool {
        match self.receiver.borrow().as_ref() {
            Some(result) => result.is_err(),
            None => self.receiver.has_changed().is_err(),
        }
    }
}

static STARTING: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(true);

pub fn install() {
    let previous = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |reason| {
        if STARTING.load(std::sync::atomic::Ordering::Relaxed) {
            report(&reason.to_string());
        }
        previous(reason);
    }));
}

pub fn ready() {
    STARTING.store(false, std::sync::atomic::Ordering::Relaxed);
}

fn write_report(directories: &[PathBuf], message: &str) -> Option<PathBuf> {
    let report = format!(
        "Huifa Media Downloader {} startup failed\n{}\n",
        env!("CARGO_PKG_VERSION"),
        huifa_core::process::redact(message)
    );
    for directory in directories {
        let path = directory.join("startup-error.log");
        if std::fs::create_dir_all(directory).is_ok() && std::fs::write(&path, &report).is_ok() {
            return Some(path);
        }
    }
    None
}

pub fn report(message: &str) {
    let message = huifa_core::process::redact(message);
    let mut directories = Vec::new();
    if let Some(directory) = std::env::var_os("HUIFA_DATA_DIR") {
        directories.push(PathBuf::from(directory));
    }
    if let Some(directory) = std::env::var_os("LOCALAPPDATA") {
        directories.push(PathBuf::from(directory).join("HuifaDownloaderRust"));
    }
    directories.push(std::env::temp_dir().join("HuifaDownloaderRust"));
    let log = write_report(&directories, &message)
        .map(|path| path.display().to_string())
        .unwrap_or_else(|| "无法写入日志 / Could not write the log".into());
    let text = format!(
        "启动失败，请将错误信息和日志提供给开发者。\nStartup failed. Please share this error and the log file.\n\n{message}\n\n日志 / Log: {log}"
    );
    eprintln!("{text}");
    #[cfg(windows)]
    {
        use windows::{
            Win32::UI::WindowsAndMessaging::{MB_ICONERROR, MB_OK, MessageBoxW},
            core::{PCWSTR, w},
        };
        let text: Vec<u16> = text.encode_utf16().chain(Some(0)).collect();
        // Startup can fail before a WebView exists, so use a native dialog.
        unsafe {
            MessageBoxW(
                None,
                PCWSTR(text.as_ptr()),
                w!("Huifa Media Downloader"),
                MB_OK | MB_ICONERROR,
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn initialization_waits_for_and_retains_the_terminal_result() {
        for result in [
            Ok(()),
            Err("Could not open the application database".into()),
        ] {
            let (initialization, sender) = Initialization::new();
            assert!(!initialization.failed());
            assert!(
                tokio::time::timeout(std::time::Duration::from_millis(10), initialization.wait())
                    .await
                    .is_err()
            );
            sender.send(Some(result.clone())).unwrap();
            assert_eq!(initialization.failed(), result.is_err());
            assert_eq!(initialization.wait().await, result);
            drop(sender);
            assert_eq!(initialization.failed(), result.is_err());
            assert_eq!(initialization.wait().await, result);
        }
    }

    #[tokio::test]
    async fn initialization_releases_every_concurrent_waiter() {
        let (initialization, sender) = Initialization::new();
        let initialization = std::sync::Arc::new(initialization);
        let (entered, mut entries) = tokio::sync::mpsc::channel(8);
        let mut waiters = Vec::new();
        for _ in 0..8 {
            let initialization = initialization.clone();
            let entered = entered.clone();
            waiters.push(tokio::spawn(async move {
                entered.send(()).await.unwrap();
                initialization.wait().await
            }));
        }
        drop(entered);
        for _ in 0..8 {
            entries.recv().await.unwrap();
        }
        assert!(waiters.iter().all(|waiter| !waiter.is_finished()));
        sender.send(Some(Ok(()))).unwrap();
        for waiter in waiters {
            assert_eq!(waiter.await.unwrap(), Ok(()));
        }
    }

    #[tokio::test]
    async fn initialization_reports_a_disconnected_initializer() {
        let (initialization, sender) = Initialization::new();
        drop(sender);
        assert!(initialization.failed());
        let result = tokio::time::timeout(std::time::Duration::from_secs(1), initialization.wait())
            .await
            .expect("A disconnected initializer must release its waiters");
        assert_eq!(
            result,
            Err("Application initialization stopped before completion".into())
        );
    }

    #[test]
    fn initialization_close_request_is_idempotent() {
        let (initialization, _sender) = Initialization::new();
        assert!(!initialization.is_closing());
        assert!(initialization.request_close());
        assert!(initialization.is_closing());
        assert!(!initialization.request_close());
        assert!(initialization.is_closing());
    }

    #[test]
    fn startup_report_keeps_cause_redacts_secrets_and_survives_invalid_data_path() {
        let directory = tempfile::tempdir().unwrap();
        let blocked = directory.path().join("data");
        std::fs::write(&blocked, b"original data").unwrap();
        let fallback = directory.path().join("fallback");
        let path = write_report(
            &[blocked.clone(), fallback.clone()],
            "Could not initialize database: HTTP 403 https://example.test?token=secret",
        )
        .unwrap();
        assert_eq!(path, fallback.join("startup-error.log"));
        let report = std::fs::read_to_string(path).unwrap();
        assert!(report.contains(env!("CARGO_PKG_VERSION")));
        assert!(report.contains("Could not initialize database: HTTP 403"));
        assert!(!report.contains("secret"));
        assert_eq!(std::fs::read(blocked).unwrap(), b"original data");
        assert!(write_report(&[directory.path().join("data/child")], "failure").is_none());
    }
}

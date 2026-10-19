use std::path::PathBuf;

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

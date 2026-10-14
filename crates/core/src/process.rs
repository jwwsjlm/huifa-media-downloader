use anyhow::{Context, Result, bail};
use std::{path::Path, process::Stdio, sync::Arc};
use tokio::{
    io::{AsyncBufReadExt, BufReader},
    process::Command,
};
use tokio_util::sync::CancellationToken;

pub type LineCallback = Arc<dyn Fn(bool, &str) + Send + Sync>;
pub struct ProcessOutput {
    pub stdout: String,
    pub stderr: String,
}

pub async fn run(
    executable: &Path,
    args: &[String],
    directory: Option<&Path>,
    cancel: &CancellationToken,
    callback: Option<LineCallback>,
) -> Result<ProcessOutput> {
    if cancel.is_cancelled() {
        bail!("Operation interrupted");
    }
    let mut command = Command::new(executable);
    command
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    if let Some(directory) = directory {
        command.current_dir(directory);
    }
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.as_std_mut().process_group(0);
    }
    #[cfg(windows)]
    {
        command.creation_flags(0x08000000);
    }
    let mut child = command.spawn().with_context(|| {
        format!(
            "Cannot start {}",
            executable.file_name().unwrap_or_default().to_string_lossy()
        )
    })?;
    let pid = child.id().context("Child process has no ID")?;
    let guard = ProcessTree::attach(pid).context("Cannot attach tool to its process supervisor")?;
    let stdout = child.stdout.take().unwrap();
    let stderr = child.stderr.take().unwrap();
    let out_callback = callback.clone();
    let out = tokio::spawn(async move { read_lines(stdout, false, out_callback).await });
    let err = tokio::spawn(async move { read_lines(stderr, true, callback).await });
    let status = tokio::select! {
        status=child.wait()=>status.context("Cannot wait for supervised tool")?,
        _=cancel.cancelled()=> { guard.terminate(); let _=child.kill().await; let _=child.wait().await; out.abort(); err.abort(); bail!("Operation interrupted"); }
    };
    // Descendants must be gone before draining pipes or shutdown can hang forever.
    guard.terminate();
    let stdout = out
        .await
        .context("Tool stdout reader stopped")?
        .context("Cannot read tool stdout")?;
    let stderr = err
        .await
        .context("Tool stderr reader stopped")?
        .context("Cannot read tool stderr")?;
    if !status.success() {
        bail!(
            "Tool exited with {}: {}",
            status.code().unwrap_or(-1),
            redact(&stderr)
        );
    }
    Ok(ProcessOutput {
        stdout,
        stderr: redact(&stderr),
    })
}

async fn read_lines<R: tokio::io::AsyncRead + Unpin>(
    reader: R,
    is_error: bool,
    callback: Option<LineCallback>,
) -> Result<String> {
    let mut reader = BufReader::new(reader);
    let mut bytes = Vec::new();
    let mut captured = String::new();
    while reader.read_until(b'\n', &mut bytes).await? != 0 {
        if bytes.last() == Some(&b'\n') {
            bytes.pop();
            if bytes.last() == Some(&b'\r') {
                bytes.pop();
            }
        }
        // Native diagnostics may use a Windows code page; structured stdout must stay strict.
        let line = if is_error {
            String::from_utf8_lossy(&bytes)
        } else {
            std::str::from_utf8(&bytes)?.into()
        };
        if let Some(callback) = &callback {
            callback(is_error, &line);
        }
        if captured.len() < 64 * 1024 * 1024 {
            captured.push_str(&line);
            captured.push('\n');
        }
        bytes.clear();
    }
    Ok(captured)
}

pub(crate) fn redact(message: &str) -> String {
    message
        .split_whitespace()
        .map(|word| {
            if word.contains("://") {
                let trimmed =
                    word.trim_matches(|c: char| matches!(c, '"' | '\'' | '(' | ')' | ','));
                crate::model::redact_url(trimmed)
            } else if word.to_ascii_lowercase().contains("cookie")
                || word.to_ascii_lowercase().contains("authorization")
                || word.to_ascii_lowercase().contains("token=")
            {
                "[redacted]".into()
            } else {
                word.to_string()
            }
        })
        .collect::<Vec<_>>()
        .join(" ")
        .chars()
        .take(2000)
        .collect()
}

#[cfg(unix)]
pub struct ProcessTree {
    pid: u32,
}
#[cfg(unix)]
impl ProcessTree {
    pub fn attach(pid: u32) -> Result<Self> {
        Ok(Self { pid })
    }
    pub fn terminate(&self) {
        unsafe {
            libc::kill(-(self.pid as i32), libc::SIGKILL);
        }
    }
}

#[cfg(windows)]
pub struct ProcessTree {
    job: isize,
}
#[cfg(windows)]
impl ProcessTree {
    pub fn attach(pid: u32) -> Result<Self> {
        use windows_sys::Win32::{
            Foundation::CloseHandle,
            System::{JobObjects::*, Threading::*},
        };
        unsafe {
            let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            if job.is_null() {
                return Err(std::io::Error::last_os_error().into());
            }
            let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            if SetInformationJobObject(
                job,
                JobObjectExtendedLimitInformation,
                &info as *const _ as *const _,
                std::mem::size_of_val(&info) as u32,
            ) == 0
            {
                CloseHandle(job);
                return Err(std::io::Error::last_os_error().into());
            }
            let handle = OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, false as i32, pid);
            if handle.is_null() {
                CloseHandle(job);
                return Err(std::io::Error::last_os_error().into());
            }
            let assigned = AssignProcessToJobObject(job, handle);
            CloseHandle(handle);
            if assigned == 0 {
                CloseHandle(job);
                return Err(std::io::Error::last_os_error().into());
            }
            Ok(Self { job: job as isize })
        }
    }
    pub fn terminate(&self) {
        unsafe {
            windows_sys::Win32::System::JobObjects::TerminateJobObject(self.job as _, 1);
        }
    }
}

impl Drop for ProcessTree {
    fn drop(&mut self) {
        self.terminate();
        #[cfg(windows)]
        unsafe {
            windows_sys::Win32::Foundation::CloseHandle(self.job as _);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn signed_urls_never_reach_errors() {
        let log = redact("failed https://example.com/a?token=secret Authorization cookie=secret");
        assert!(!log.contains("secret"));
    }
    #[tokio::test]
    async fn legacy_encoded_stderr_does_not_discard_valid_metadata() {
        #[cfg(windows)]
        let (exe, args) = (
            std::path::PathBuf::from("powershell.exe"),
            vec![
                "-NoProfile".into(),
                "-NonInteractive".into(),
                "-Command".into(),
                "$out = [byte[]](123,34,116,105,116,108,101,34,58,34,228,184,173,230,150,135,34,125,13,10); [Console]::OpenStandardOutput().Write($out,0,$out.Length); $err = [byte[]](119,97,114,110,58,32,178,226,202,212,13,10,100,111,110,101); [Console]::OpenStandardError().Write($err,0,$err.Length)".into(),
            ],
        );
        #[cfg(unix)]
        let (exe, args) = (
            std::path::PathBuf::from("sh"),
            vec![
                "-c".into(),
                "printf '{\"title\":\"\\344\\270\\255\\346\\226\\207\"}\\r\\n'; printf 'warn: \\262\\342\\312\\324\\r\\ndone' >&2".into(),
            ],
        );
        let lines = Arc::new(std::sync::Mutex::new(Vec::new()));
        let received = Arc::clone(&lines);
        let callback: LineCallback = Arc::new(move |is_error, line| {
            received.lock().unwrap().push((is_error, line.to_owned()));
        });
        let output = run(&exe, &args, None, &CancellationToken::new(), Some(callback))
            .await
            .expect("Non-UTF-8 tool warnings must not fail metadata parsing");
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(&output.stdout).unwrap()["title"],
            "中文"
        );
        {
            let lines = lines.lock().unwrap();
            assert!(
                lines
                    .iter()
                    .any(|(is_error, line)| !is_error && line == "{\"title\":\"中文\"}")
            );
            assert!(
                lines
                    .iter()
                    .any(|(is_error, line)| *is_error && line.starts_with("warn: "))
            );
            assert!(
                lines
                    .iter()
                    .any(|(is_error, line)| *is_error && line == "done")
            );
        }
        let mut failed_args = args;
        failed_args.last_mut().unwrap().push_str("; exit 7");
        let error = run(&exe, &failed_args, None, &CancellationToken::new(), None)
            .await
            .err()
            .expect("A failed tool must remain a failure despite non-UTF-8 stderr");
        assert!(error.to_string().starts_with("Tool exited with 7: warn: "));
    }
    #[tokio::test]
    async fn invalid_utf8_metadata_is_still_rejected() {
        assert!(
            read_lines(&b"{\"title\":\"\xff\"}\n"[..], false, None)
                .await
                .is_err()
        );
    }
    #[tokio::test]
    async fn cancellation_stops_supervised_process() {
        let cancel = CancellationToken::new();
        let stopped = cancel.clone();
        tokio::spawn(async move {
            tokio::time::sleep(std::time::Duration::from_millis(100)).await;
            stopped.cancel();
        });
        #[cfg(windows)]
        let (exe, args) = (
            std::path::PathBuf::from("powershell.exe"),
            vec![
                "-NoProfile".into(),
                "-NonInteractive".into(),
                "-Command".into(),
                "Start-Sleep -Seconds 30".into(),
            ],
        );
        #[cfg(unix)]
        let (exe, args) = (
            std::path::PathBuf::from("sh"),
            vec!["-c".into(), "sleep 30 & wait".into()],
        );
        assert!(
            tokio::time::timeout(
                std::time::Duration::from_secs(3),
                run(&exe, &args, None, &cancel, None)
            )
            .await
            .unwrap()
            .is_err()
        );
    }
}

use anyhow::{Context, Result, bail};
use std::{path::Path, process::Stdio, sync::Arc};
use tokio::{
    io::{AsyncBufReadExt, AsyncReadExt, BufReader},
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
    run_inner(executable, args, directory, cancel, callback, None).await
}

/// Metadata queries must bound bytes while reading, including a single JSON line.
pub async fn run_limited(
    executable: &Path,
    args: &[String],
    directory: Option<&Path>,
    cancel: &CancellationToken,
    max_stdout: usize,
    timeout: std::time::Duration,
) -> Result<ProcessOutput> {
    run_inner(
        executable,
        args,
        directory,
        cancel,
        None,
        Some((max_stdout, timeout, false)),
    )
    .await
}

/// Only human-readable engine inventories use the tool's legacy Windows code page.
pub async fn run_limited_text(
    executable: &Path,
    args: &[String],
    cancel: &CancellationToken,
    max_stdout: usize,
    timeout: std::time::Duration,
) -> Result<ProcessOutput> {
    run_inner(
        executable,
        args,
        None,
        cancel,
        None,
        Some((max_stdout, timeout, true)),
    )
    .await
}

async fn run_inner(
    executable: &Path,
    args: &[String],
    directory: Option<&Path>,
    cancel: &CancellationToken,
    callback: Option<LineCallback>,
    limit: Option<(usize, std::time::Duration, bool)>,
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
    let overflow = CancellationToken::new();
    let out_overflow = overflow.clone();
    let out = tokio::spawn(async move {
        match limit {
            Some((max, _, legacy_text)) => {
                read_limited_output(stdout, max, out_overflow, legacy_text).await
            }
            None => read_lines(stdout, false, out_callback).await,
        }
    });
    let err = tokio::spawn(async move {
        if limit.is_some() {
            read_diagnostics(stderr).await
        } else {
            read_lines(stderr, true, callback).await
        }
    });
    let status = tokio::select! {
        status=child.wait()=>status.context("Cannot wait for supervised tool")?,
        _=cancel.cancelled()=> { guard.terminate(); let _=child.kill().await; let _=child.wait().await; out.abort(); err.abort(); bail!("Operation interrupted"); }
        _=overflow.cancelled()=> { guard.terminate(); let _=child.kill().await; let _=child.wait().await; out.abort(); err.abort(); bail!("Metadata stdout exceeds its byte limit"); }
        _=tokio::time::sleep(limit.map_or(std::time::Duration::from_secs(86400), |(_, timeout, _)| timeout)), if limit.is_some()=> { guard.terminate(); let _=child.kill().await; let _=child.wait().await; out.abort(); err.abort(); bail!("Metadata query timed out"); }
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
            "{} exited with code {}: {}",
            executable.file_name().unwrap_or_default().to_string_lossy(),
            status.code().unwrap_or(-1),
            if stderr.trim().is_empty() {
                "No diagnostic output was returned".into()
            } else {
                redact(&stderr)
            }
        );
    }
    Ok(ProcessOutput {
        stdout,
        stderr: redact(&stderr),
    })
}

#[cfg(test)]
async fn read_limited<R: tokio::io::AsyncRead + Unpin>(
    reader: R,
    max: usize,
    overflow: CancellationToken,
) -> Result<String> {
    read_limited_output(reader, max, overflow, false).await
}

async fn read_limited_output<R: tokio::io::AsyncRead + Unpin>(
    mut reader: R,
    max: usize,
    overflow: CancellationToken,
    legacy_text: bool,
) -> Result<String> {
    let mut captured = Vec::new();
    let mut chunk = [0; 8192];
    loop {
        let count = reader.read(&mut chunk).await?;
        if count == 0 {
            break;
        }
        if count > max.saturating_sub(captured.len()) {
            overflow.cancel();
            bail!("Metadata stdout exceeds its byte limit");
        }
        captured.extend_from_slice(&chunk[..count]);
    }
    match String::from_utf8(captured) {
        Ok(value) => Ok(value),
        Err(error) if legacy_text => decode_inventory_text(error.as_bytes()),
        Err(error) => Err(error).context("Metadata stdout is not UTF-8"),
    }
}

fn decode_inventory_text(bytes: &[u8]) -> Result<String> {
    #[cfg(windows)]
    {
        use windows_sys::Win32::Globalization::{
            CP_ACP, MB_ERR_INVALID_CHARS, MultiByteToWideChar,
        };
        let length = i32::try_from(bytes.len())?;
        let size = unsafe {
            MultiByteToWideChar(
                CP_ACP,
                MB_ERR_INVALID_CHARS,
                bytes.as_ptr(),
                length,
                std::ptr::null_mut(),
                0,
            )
        };
        if size == 0 {
            return Err(std::io::Error::last_os_error())
                .context("Cannot decode the engine inventory's Windows code page");
        }
        let mut wide = vec![0u16; size as usize];
        ensure_decoded(
            unsafe {
                MultiByteToWideChar(
                    CP_ACP,
                    MB_ERR_INVALID_CHARS,
                    bytes.as_ptr(),
                    length,
                    wide.as_mut_ptr(),
                    size,
                )
            } == size,
        )?;
        String::from_utf16(&wide).context("Invalid Windows engine inventory text")
    }
    #[cfg(not(windows))]
    {
        std::str::from_utf8(bytes)
            .map(str::to_owned)
            .context("Engine inventory is not UTF-8")
    }
}

#[cfg(windows)]
fn ensure_decoded(success: bool) -> Result<()> {
    if !success {
        return Err(std::io::Error::last_os_error())
            .context("Cannot decode the engine inventory's Windows code page");
    }
    Ok(())
}

async fn read_diagnostics<R: tokio::io::AsyncRead + Unpin>(mut reader: R) -> Result<String> {
    let mut captured = Vec::new();
    let mut chunk = [0; 8192];
    loop {
        let count = reader.read(&mut chunk).await?;
        if count == 0 {
            break;
        }
        let take = count.min((1024 * 1024_usize).saturating_sub(captured.len()));
        captured.extend_from_slice(&chunk[..take]);
    }
    Ok(String::from_utf8_lossy(&captured).into_owned())
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

pub fn redact(message: &str) -> String {
    let sanitized = message
        .lines()
        .flat_map(|line| {
            let mut sensitive_header = false;
            line.split_whitespace().map(move |word| {
                let option = word
                    .trim_matches(|c: char| matches!(c, '"' | '\'' | '(' | ')' | ',' | '.' | '`'));
                let lower = option.to_ascii_lowercase();
                if matches!(
                    lower.as_str(),
                    "authorization:" | "proxy-authorization:" | "cookie:" | "set-cookie:"
                ) {
                    sensitive_header = true;
                }
                if sensitive_header {
                    "[redacted]".into()
                } else if matches!(
                    lower.as_str(),
                    "--cookies" | "--cookies-from-browser" | "cookie" | "cookies"
                ) {
                    word.to_string()
                } else if word.contains("://") {
                    let trimmed =
                        word.trim_matches(|c: char| matches!(c, '"' | '\'' | '(' | ')' | ','));
                    crate::model::redact_url(trimmed)
                } else if lower.contains("cookie")
                    || lower.contains("authorization")
                    || lower.contains("token=")
                {
                    "[redacted]".into()
                } else {
                    word.to_string()
                }
            })
        })
        .collect::<Vec<_>>()
        .join(" ");
    if sanitized.chars().nth(2000).is_none() {
        return sanitized;
    }
    // Tool banners can be long; keep both the operation context and final diagnostic.
    let head: String = sanitized.chars().take(900).collect();
    let tail: String = sanitized.chars().rev().take(1097).collect();
    format!("{head} … {}", tail.chars().rev().collect::<String>())
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
    pub fn is_running(&self) -> Result<bool> {
        if unsafe { libc::kill(-(self.pid as i32), 0) } == 0 {
            return Ok(true);
        }
        let reason = std::io::Error::last_os_error();
        match reason.raw_os_error() {
            Some(libc::ESRCH) => Ok(false),
            Some(libc::EPERM) => Ok(true),
            _ => Err(reason.into()),
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
    pub fn is_running(&self) -> Result<bool> {
        use windows_sys::Win32::System::JobObjects::*;
        let mut info: JOBOBJECT_BASIC_ACCOUNTING_INFORMATION = unsafe { std::mem::zeroed() };
        if unsafe {
            QueryInformationJobObject(
                self.job as _,
                JobObjectBasicAccountingInformation,
                &mut info as *mut _ as *mut _,
                std::mem::size_of_val(&info) as u32,
                std::ptr::null_mut(),
            )
        } == 0
        {
            return Err(std::io::Error::last_os_error().into());
        }
        Ok(info.ActiveProcesses != 0)
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
    use std::time::Duration;
    #[test]
    fn signed_urls_never_reach_errors() {
        let log = redact("failed https://example.com/a?token=secret Authorization cookie=secret");
        assert!(!log.contains("secret"));
    }
    #[test]
    fn cookie_advice_is_readable_but_credentials_stay_redacted() {
        let log = redact(
            "Sign in with --cookies or `--cookies-from-browser`. cookie=secret C:/data/.cookie-secret https://user:secret@example.com/a?token=secret",
        );
        assert!(log.contains("--cookies or `--cookies-from-browser`."));
        assert!(!log.contains("secret"));
    }
    #[test]
    fn long_diagnostics_keep_the_failure_tail_after_redacting_secrets() {
        let message = format!(
            "Failed to encode: {}\nAuthorization: Bearer auth-secret\nCookie: SID=cookie-secret; OTHER=other-secret\nInvalid argument https://example.test/media?token=query-secret",
            "编码信息 ".repeat(1000)
        );
        let log = redact(&message);
        assert!(log.starts_with("Failed to encode:"));
        assert!(log.ends_with("Invalid argument https://example.test/media"));
        assert!(!log.contains("secret"));
        assert_eq!(log.chars().count(), 2000);
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
        assert!(
            error
                .to_string()
                .starts_with(&format!("{} exited with code 7: warn: ", exe.display()))
        );
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
    async fn metadata_limit_rejects_a_single_oversized_line() {
        let overflow = CancellationToken::new();
        let bytes = vec![b'x'; 1024 * 1024];
        assert!(
            read_limited(bytes.as_slice(), 1024, overflow.clone())
                .await
                .is_err()
        );
        assert!(overflow.is_cancelled());
        assert_eq!(
            read_limited(&b"{}"[..], 2, CancellationToken::new())
                .await
                .unwrap(),
            "{}"
        );
    }

    #[tokio::test]
    async fn metadata_overflow_and_timeout_terminate_supervised_tools() {
        #[cfg(windows)]
        let (exe, prefix, loud, slow) = (
            std::path::PathBuf::from("powershell.exe"),
            vec![
                "-NoProfile".into(),
                "-NonInteractive".into(),
                "-Command".into(),
            ],
            "[Console]::OpenStandardOutput().Write([byte[]]::new(1048576),0,1048576); Start-Sleep -Seconds 30",
            "Start-Sleep -Seconds 30",
        );
        #[cfg(unix)]
        let (exe, prefix, loud, slow) = (
            std::path::PathBuf::from("sh"),
            vec!["-c".into()],
            "head -c 1048576 /dev/zero; sleep 30",
            "sleep 30 & wait",
        );
        for (script, limit, timeout, message) in [
            (loud, 1024, 3000, "byte limit"),
            (slow, 1024, 100, "timed out"),
        ] {
            let mut args = prefix.clone();
            args.push(script.into());
            let error = tokio::time::timeout(
                Duration::from_secs(6),
                run_limited(
                    &exe,
                    &args,
                    None,
                    &CancellationToken::new(),
                    limit,
                    Duration::from_millis(timeout),
                ),
            )
            .await
            .unwrap()
            .err()
            .unwrap();
            assert!(format!("{error:#}").contains(message), "{error:#}");
        }
    }

    #[tokio::test]
    async fn metadata_cancellation_remains_prompt() {
        let cancel = CancellationToken::new();
        let stopped = cancel.clone();
        tokio::spawn(async move {
            tokio::time::sleep(Duration::from_millis(100)).await;
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
        let result = tokio::time::timeout(
            Duration::from_secs(3),
            run_limited(&exe, &args, None, &cancel, 1024, Duration::from_secs(120)),
        )
        .await
        .unwrap();
        assert!(result.err().unwrap().to_string().contains("interrupted"));
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

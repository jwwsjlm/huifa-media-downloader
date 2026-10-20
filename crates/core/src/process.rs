use anyhow::{Context, Result, bail};
use std::{collections::VecDeque, path::Path, process::Stdio, sync::Arc};
use tokio::{io::AsyncReadExt, process::Command};
use tokio_util::sync::CancellationToken;

pub type LineCallback = Arc<dyn Fn(bool, &str) + Send + Sync>;
pub struct ProcessOutput {
    pub stdout: String,
    pub stderr: String,
}

const STDOUT_LIMIT: usize = 64 * 1024 * 1024;
const LINE_LIMIT: usize = 1024 * 1024;
const DIAGNOSTIC_LIMIT: usize = 64 * 1024;

struct ReaderTask(Option<tokio::task::JoinHandle<Result<String>>>);
impl ReaderTask {
    async fn finish(&mut self) -> Result<String> {
        let result = self
            .0
            .as_mut()
            .unwrap()
            .await
            .context("Tool output reader stopped");
        self.0.take();
        result?
    }
}
impl Drop for ReaderTask {
    fn drop(&mut self) {
        if let Some(handle) = &self.0 {
            handle.abort();
        }
    }
}

#[derive(Default)]
struct Diagnostics {
    head: Vec<u8>,
    tail: VecDeque<u8>,
    total: usize,
}
impl Diagnostics {
    fn push(&mut self, bytes: &[u8]) {
        self.total = self.total.saturating_add(bytes.len());
        let head_limit = DIAGNOSTIC_LIMIT / 2 - 32;
        let head = bytes.len().min(head_limit.saturating_sub(self.head.len()));
        self.head.extend_from_slice(&bytes[..head]);
        let remaining = &bytes[head..];
        let tail_limit = DIAGNOSTIC_LIMIT / 2 - 32;
        let discard = (self.tail.len() + remaining.len()).saturating_sub(tail_limit);
        self.tail.drain(..discard.min(self.tail.len()));
        self.tail.extend(
            remaining
                .iter()
                .skip(remaining.len().saturating_sub(tail_limit)),
        );
    }
    fn finish(self) -> String {
        let mut bytes = self.head;
        if self.total > bytes.len() + self.tail.len() {
            bytes.extend_from_slice(b"\n... [diagnostic output truncated] ...\n");
        }
        bytes.extend(self.tail);
        let mut text = String::from_utf8_lossy(&bytes).into_owned();
        if text.len() > DIAGNOSTIC_LIMIT {
            let mut end = DIAGNOSTIC_LIMIT;
            while !text.is_char_boundary(end) {
                end -= 1;
            }
            text.truncate(end);
        }
        text
    }
}

#[derive(Clone, Copy)]
enum OutputMode {
    Lines,
    Streamed,
    Limited(usize, Option<std::time::Duration>, bool),
}

pub(crate) async fn run_streamed(
    executable: &Path,
    args: &[String],
    cancel: &CancellationToken,
    callback: LineCallback,
) -> Result<()> {
    run_inner(
        executable,
        args,
        None,
        (cancel, None),
        Some(callback),
        OutputMode::Streamed,
        None,
    )
    .await
    .map(|_| ())
}

pub async fn run(
    executable: &Path,
    args: &[String],
    directory: Option<&Path>,
    cancel: &CancellationToken,
    callback: Option<LineCallback>,
) -> Result<ProcessOutput> {
    run_inner(
        executable,
        args,
        directory,
        (cancel, None),
        callback,
        OutputMode::Lines,
        None,
    )
    .await
}

pub(crate) async fn run_logged(
    executable: &Path,
    args: &[String],
    directory: Option<&Path>,
    cancel: &CancellationToken,
    callback: Option<LineCallback>,
    log: crate::task_logs::RawLog,
) -> Result<ProcessOutput> {
    run_inner(
        executable,
        args,
        directory,
        (cancel, None),
        callback,
        OutputMode::Lines,
        Some(log),
    )
    .await
}

pub(crate) async fn run_logged_streamed(
    executable: &Path,
    args: &[String],
    directory: Option<&Path>,
    cancel: &CancellationToken,
    callback: Option<LineCallback>,
    log: crate::task_logs::RawLog,
) -> Result<()> {
    run_inner(
        executable,
        args,
        directory,
        (cancel, None),
        callback,
        OutputMode::Streamed,
        Some(log),
    )
    .await
    .map(|_| ())
}

pub(crate) async fn run_capture(
    executable: &Path,
    args: &[String],
    directory: &Path,
    cancel: &CancellationToken,
    finish: &CancellationToken,
    callback: Option<LineCallback>,
    log: crate::task_logs::RawLog,
) -> Result<ProcessOutput> {
    run_inner(
        executable,
        args,
        Some(directory),
        (cancel, Some(finish)),
        callback,
        OutputMode::Lines,
        Some(log),
    )
    .await
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
        (cancel, None),
        None,
        OutputMode::Limited(max_stdout, Some(timeout), false),
        None,
    )
    .await
}

pub(crate) async fn run_limited_logged(
    executable: &Path,
    args: &[String],
    cancel: &CancellationToken,
    max_stdout: usize,
    timeout: Option<std::time::Duration>,
    log: Option<crate::task_logs::RawLog>,
) -> Result<ProcessOutput> {
    run_inner(
        executable,
        args,
        None,
        (cancel, None),
        None,
        OutputMode::Limited(max_stdout, timeout, false),
        log,
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
        (cancel, None),
        None,
        OutputMode::Limited(max_stdout, Some(timeout), true),
        None,
    )
    .await
}

async fn run_inner(
    executable: &Path,
    args: &[String],
    directory: Option<&Path>,
    control: (&CancellationToken, Option<&CancellationToken>),
    callback: Option<LineCallback>,
    mode: OutputMode,
    log: Option<crate::task_logs::RawLog>,
) -> Result<ProcessOutput> {
    let (cancel, finish) = control;
    if cancel.is_cancelled() {
        bail!("Operation interrupted");
    }
    if let Some(log) = &log {
        log.write(
            "command",
            serde_json::to_string(
                &serde_json::json!({"executable":executable,"args":args,"directory":directory}),
            )?
            .as_bytes(),
        )?;
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
    let out_log = log.clone();
    let err_log = log.clone();
    let overflow = CancellationToken::new();
    let out_overflow = overflow.clone();
    let out_failure = overflow.clone();
    let mut out = ReaderTask(Some(tokio::spawn(async move {
        let result = match mode {
            OutputMode::Limited(max, _, legacy_text) => {
                read_limited_output(stdout, max, out_overflow, legacy_text, out_log).await
            }
            OutputMode::Lines | OutputMode::Streamed => {
                read_lines(
                    stdout,
                    false,
                    out_callback,
                    out_log,
                    matches!(mode, OutputMode::Lines),
                    out_overflow,
                )
                .await
            }
        };
        if result.is_err() {
            out_failure.cancel();
        }
        result
    })));
    let err_failure = overflow.clone();
    let mut err = ReaderTask(Some(tokio::spawn(async move {
        let result = if matches!(mode, OutputMode::Limited(..)) {
            read_diagnostics(stderr, err_log).await
        } else {
            read_lines(stderr, true, callback, err_log, true, err_failure.clone()).await
        };
        if result.is_err() {
            err_failure.cancel();
        }
        result
    })));
    let mut stopped_capture = false;
    let status = tokio::select! {
        biased;
        _=cancel.cancelled()=> Err(anyhow::anyhow!("Operation interrupted")),
        _=async { match finish { Some(token) => token.cancelled().await, None => std::future::pending::<()>().await } }=> {
            stopped_capture = true;
            // Live captures use MPEG-TS; the normal media validator decides whether the saved part is usable.
            guard.terminate();
            child.wait().await.context("Cannot wait for stopped live capture")
        },
        status=child.wait()=>status.context("Cannot wait for supervised tool"),
        _=overflow.cancelled()=> Err(anyhow::anyhow!("Tool output exceeds its byte limit or cannot be read")),
        _=async {
            match mode {
                OutputMode::Limited(_, Some(timeout), _) => tokio::time::sleep(timeout).await,
                _ => std::future::pending().await,
            }
        }=> Err(anyhow::anyhow!("Metadata query timed out")),
    };
    // Descendants must be gone before draining pipes or shutdown can hang forever.
    guard.terminate();
    if status.is_err() {
        let _ = child.kill().await;
        let _ = child.wait().await;
    }
    let stdout = out.finish().await;
    let stderr = err.finish().await;
    if let Some(log) = &log {
        log.write(
            "exit",
            match &status {
                Ok(status) => format!("{status}"),
                Err(error) => format!("{error:#}"),
            }
            .as_bytes(),
        )?;
        log.flush()?;
    }
    let status = status?;
    let stdout = stdout.context("Cannot read tool stdout")?;
    let stderr = stderr.context("Cannot read tool stderr")?;
    if !status.success() && !stopped_capture {
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
    read_limited_output(reader, max, overflow, false, None).await
}

async fn read_limited_output<R: tokio::io::AsyncRead + Unpin>(
    mut reader: R,
    max: usize,
    overflow: CancellationToken,
    legacy_text: bool,
    log: Option<crate::task_logs::RawLog>,
) -> Result<String> {
    let mut captured = Vec::new();
    let mut chunk = [0; 8192];
    loop {
        let count = reader.read(&mut chunk).await?;
        if count == 0 {
            break;
        }
        if count > max.saturating_sub(captured.len()) {
            if let Some(log) = &log {
                captured.reserve_exact(count);
                captured.extend_from_slice(&chunk[..count]);
                log.write("stdout", &captured)?;
            }
            overflow.cancel();
            bail!("Metadata stdout exceeds its byte limit");
        }
        captured.extend_from_slice(&chunk[..count]);
    }
    if let Some(log) = &log {
        log.write("stdout", &captured)?;
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

async fn read_diagnostics<R: tokio::io::AsyncRead + Unpin>(
    reader: R,
    log: Option<crate::task_logs::RawLog>,
) -> Result<String> {
    read_lines(reader, true, None, log, true, CancellationToken::new()).await
}

async fn read_lines<R: tokio::io::AsyncRead + Unpin>(
    reader: R,
    is_error: bool,
    callback: Option<LineCallback>,
    log: Option<crate::task_logs::RawLog>,
    capture: bool,
    overflow: CancellationToken,
) -> Result<String> {
    use std::io::{Seek, Write};
    let mut reader = reader;
    let mut bytes = Vec::new();
    let mut captured = String::new();
    let mut diagnostics = Diagnostics::default();
    let mut spilled: Option<std::fs::File> = None;
    let mut oversized = false;
    let mut failure = None;
    let mut chunk = [0; 8192];
    loop {
        let count = reader.read(&mut chunk).await?;
        let eof = count == 0;
        for part in chunk[..count]
            .split_inclusive(|byte| *byte == b'\n')
            .chain(eof.then_some(&b""[..]))
        {
            if !oversized && bytes.len() + part.len() > LINE_LIMIT {
                if let Some(log) = &log {
                    let mut file = tempfile::tempfile_in(&log.directory)?;
                    file.write_all(&bytes)?;
                    spilled = Some(file);
                }
                bytes.clear();
                oversized = true;
                if !is_error {
                    failure.get_or_insert_with(|| {
                        anyhow::anyhow!("Tool stdout line exceeds its byte limit")
                    });
                    overflow.cancel();
                }
            }
            if oversized {
                if let Some(file) = &mut spilled {
                    file.write_all(part)?;
                }
            } else {
                bytes.extend_from_slice(part);
            }
            if !part.ends_with(b"\n") && !eof {
                continue;
            }
            if bytes.is_empty() && !oversized {
                continue;
            }
            if let Some(log) = &log {
                if let Some(file) = &mut spilled {
                    log.write_file(if is_error { "stderr" } else { "stdout" }, file)?;
                    file.set_len(0)?;
                    file.rewind()?;
                } else {
                    log.write(if is_error { "stderr" } else { "stdout" }, &bytes)?;
                }
            }
            if !oversized {
                if bytes.last() == Some(&b'\n') {
                    bytes.pop();
                    if bytes.last() == Some(&b'\r') {
                        bytes.pop();
                    }
                }
                // Native diagnostics may use a Windows code page; structured stdout stays strict.
                let line = if is_error {
                    Some(String::from_utf8_lossy(&bytes))
                } else {
                    match std::str::from_utf8(&bytes) {
                        Ok(line) => Some(line.into()),
                        Err(error) => {
                            failure.get_or_insert_with(|| error.into());
                            overflow.cancel();
                            None
                        }
                    }
                };
                if let Some(line) = line {
                    if is_error {
                        diagnostics.push(redact(&line).as_bytes());
                        diagnostics.push(b"\n");
                    }
                    if let Some(callback) = &callback {
                        callback(is_error, &line);
                    }
                    if capture && !is_error && failure.is_none() {
                        if line.len() + 1 > STDOUT_LIMIT.saturating_sub(captured.len()) {
                            failure = Some(anyhow::anyhow!("Tool stdout exceeds its byte limit"));
                            overflow.cancel();
                        } else {
                            captured.push_str(&line);
                            captured.push('\n');
                        }
                    }
                }
            } else if is_error {
                diagnostics.push(b"[oversized diagnostic line]\n");
                if let Some(callback) = &callback {
                    callback(true, "Tool emitted an oversized diagnostic line");
                }
            }
            bytes.clear();
            oversized = false;
            spilled = None;
        }
        if eof {
            break;
        }
    }
    if let Some(error) = failure {
        return Err(error);
    }
    Ok(if is_error {
        diagnostics.finish()
    } else {
        captured
    })
}

pub fn redact(message: &str) -> String {
    let mut head = String::new();
    let mut tail = VecDeque::with_capacity(1097);
    let mut count = 0usize;
    for line in message.lines() {
        let mut sensitive_header = false;
        for word in line.split_whitespace() {
            let sanitized = if word.len() > 8192 {
                "[oversized token]".to_string()
            } else {
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
            };
            for character in (count > 0)
                .then_some(' ')
                .into_iter()
                .chain(sanitized.chars())
            {
                if count < 900 {
                    head.push(character);
                } else {
                    if tail.len() == 1097 {
                        tail.pop_front();
                    }
                    tail.push_back(character);
                }
                count = count.saturating_add(1);
            }
        }
    }
    if count > 1997 {
        head.push_str(" … ");
    }
    head.extend(tail);
    head
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
        for capture in [true, false] {
            assert!(
                read_lines(
                    &b"{\"title\":\"\xff\"}\n"[..],
                    false,
                    None,
                    None,
                    capture,
                    CancellationToken::new()
                )
                .await
                .is_err()
            );
        }
    }

    #[tokio::test]
    async fn streamed_packet_callbacks_continue_past_the_capture_limit() {
        use std::sync::atomic::{AtomicUsize, Ordering};
        let records = 65 * 1024;
        let mut record = vec![b'x'; 1023];
        record.push(b'\n');
        let mut bytes = record.repeat(records);
        bytes.extend_from_slice(b"last packet\r\nunterminated packet");
        let observed = Arc::new(AtomicUsize::new(0));
        let count = observed.clone();
        let callback: LineCallback = Arc::new(move |is_error, line| {
            assert!(!is_error);
            let index = count.fetch_add(1, Ordering::Relaxed);
            if index < records {
                assert_eq!(line.len(), 1023);
            } else if index == records {
                assert_eq!(line, "last packet");
            } else {
                assert_eq!(line, "unterminated packet");
            }
        });
        let captured = read_lines(
            &bytes[..],
            false,
            Some(callback),
            None,
            false,
            CancellationToken::new(),
        )
        .await
        .unwrap();
        assert!(captured.is_empty());
        assert_eq!(observed.load(Ordering::Relaxed), records + 2);
    }

    #[tokio::test]
    async fn streamed_tool_failure_keeps_diagnostics_and_redacts_secrets() {
        #[cfg(windows)]
        let (exe, args) = (
            std::path::PathBuf::from("powershell.exe"),
            vec![
                "-NoProfile".into(),
                "-NonInteractive".into(),
                "-Command".into(),
                "[Console]::Out.WriteLine('packet-hash'); [Console]::Error.WriteLine('encode failed https://example.test/video?token=stream-secret'); exit 7".into(),
            ],
        );
        #[cfg(unix)]
        let (exe, args) = (
            std::path::PathBuf::from("sh"),
            vec!["-c".into(), "printf 'packet-hash\\n'; printf 'encode failed https://example.test/video?token=stream-secret\\n' >&2; exit 7".into()],
        );
        let lines = Arc::new(std::sync::Mutex::new(Vec::new()));
        let observed = lines.clone();
        let callback: LineCallback = Arc::new(move |is_error, line| {
            observed.lock().unwrap().push((is_error, line.to_string()));
        });
        let error = run_streamed(&exe, &args, &CancellationToken::new(), callback)
            .await
            .err()
            .unwrap();
        let error = format!("{error:#}");
        assert!(error.contains("code 7"), "{error}");
        assert!(error.contains("encode failed"), "{error}");
        assert!(!error.contains("stream-secret"), "{error}");
        let lines = lines.lock().unwrap();
        assert!(
            lines
                .iter()
                .any(|(is_error, line)| !is_error && line == "packet-hash")
        );
        assert!(
            lines
                .iter()
                .any(|(is_error, line)| *is_error && line.starts_with("encode failed"))
        );
    }

    #[tokio::test]
    async fn streamed_tool_cancellation_closes_the_supervised_pipes() {
        #[cfg(windows)]
        let (exe, args) = (
            std::path::PathBuf::from("powershell.exe"),
            vec![
                "-NoProfile".into(),
                "-NonInteractive".into(),
                "-Command".into(),
                "[Console]::Out.WriteLine('ready'); Start-Sleep -Seconds 30".into(),
            ],
        );
        #[cfg(unix)]
        let (exe, args) = (
            std::path::PathBuf::from("sh"),
            vec!["-c".into(), "sleep 30 & printf 'ready\\n'; wait".into()],
        );
        let cancel = CancellationToken::new();
        let stopped = cancel.clone();
        let callback: LineCallback = Arc::new(move |is_error, line| {
            if !is_error && line == "ready" {
                stopped.cancel();
            }
        });
        let error = tokio::time::timeout(
            Duration::from_secs(3),
            run_streamed(&exe, &args, &cancel, callback),
        )
        .await
        .expect("Stream cancellation must close the process pipes promptly")
        .err()
        .unwrap();
        assert!(error.to_string().contains("interrupted"), "{error:#}");
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
            (loud, 1024, Some(3000), "byte limit"),
            (loud, 1024, None, "byte limit"),
            (slow, 1024, Some(100), "timed out"),
        ] {
            let mut args = prefix.clone();
            args.push(script.into());
            let error = tokio::time::timeout(
                Duration::from_secs(6),
                run_limited_logged(
                    &exe,
                    &args,
                    &CancellationToken::new(),
                    limit,
                    timeout.map(Duration::from_millis),
                    None,
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
            run_limited_logged(&exe, &args, &cancel, 1024, None, None),
        )
        .await
        .unwrap();
        assert!(result.err().unwrap().to_string().contains("interrupted"));
    }
    #[tokio::test]
    async fn bounded_metadata_journal_keeps_utf8_and_complete_diagnostics() -> Result<()> {
        let root = tempfile::tempdir()?;
        let app = crate::App::open(crate::AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("downloads"),
            tool_dir: None,
        })
        .await?;
        let task = crate::tests::paused_task(Default::default());
        app.db.insert_task(&task)?;
        let payload = format!("{}中文", "x".repeat(8191));
        assert_eq!(
            read_limited_output(
                payload.as_bytes(),
                16 * 1024,
                CancellationToken::new(),
                false,
                Some(app.raw_task_log(&task.id)?)
            )
            .await?,
            payload
        );
        let diagnostics = format!("{payload}\n{}", "尾".repeat(350_000));
        let captured =
            read_diagnostics(diagnostics.as_bytes(), Some(app.raw_task_log(&task.id)?)).await?;
        assert!(captured.len() <= 1024 * 1024 + 3);
        app.task_log_writer.flush()?;
        let raw = std::fs::read_to_string(
            app.config
                .data_dir
                .join("task-logs")
                .join(format!("{}.log", task.id)),
        )?;
        assert!(raw.contains(&format!("stdout\n{payload}\n")));
        assert!(raw.contains(&format!("stderr\n{payload}\n")));
        assert!(raw.contains(&format!("stderr\n{}\n", "尾".repeat(350_000))));
        app.shutdown().await?;
        Ok(())
    }

    #[tokio::test]
    async fn oversized_lines_are_bounded_and_the_original_journal_remains_complete() -> Result<()> {
        let root = tempfile::tempdir()?;
        let app = crate::App::open(crate::AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("downloads"),
            tool_dir: None,
        })
        .await?;
        let task = crate::tests::paused_task(Default::default());
        app.db.insert_task(&task)?;
        let payload = format!(
            "first diagnostic\n{}\nfinal diagnostic token=secret-tail",
            "x".repeat(LINE_LIMIT + 8192)
        );
        let output = read_lines(
            payload.as_bytes(),
            true,
            None,
            Some(app.raw_task_log(&task.id)?),
            true,
            CancellationToken::new(),
        )
        .await?;
        assert!(output.len() <= DIAGNOSTIC_LIMIT);
        assert!(output.starts_with("first diagnostic"));
        assert!(output.contains("final diagnostic"));
        assert!(!output.contains("secret-tail"));
        let redacted = redact(&output);
        assert!(redacted.chars().count() <= 2000);
        assert!(!redacted.contains("secret-tail"));
        app.task_log_writer.flush()?;
        let raw = std::fs::read_to_string(
            app.config
                .data_dir
                .join("task-logs")
                .join(format!("{}.log", task.id)),
        )?;
        assert!(raw.contains(&"x".repeat(LINE_LIMIT + 8192)));
        assert!(raw.contains("final diagnostic token=secret-tail"));
        let overflow = CancellationToken::new();
        assert!(
            read_lines(
                &vec![b'x'; LINE_LIMIT + 1][..],
                false,
                None,
                None,
                false,
                overflow.clone()
            )
            .await
            .is_err()
        );
        assert!(overflow.is_cancelled());
        app.shutdown().await?;
        Ok(())
    }

    #[test]
    fn diagnostics_and_redaction_keep_a_bounded_failure_tail() {
        let mut diagnostics = Diagnostics::default();
        diagnostics.push(b"operation context\n");
        for _ in 0..2000 {
            diagnostics.push(&[b'x'; 1024]);
        }
        diagnostics.push(b"\nencoder failed https://example.test/media?token=hidden");
        assert!(diagnostics.head.len() + diagnostics.tail.len() <= DIAGNOSTIC_LIMIT);
        let output = diagnostics.finish();
        assert!(output.len() <= DIAGNOSTIC_LIMIT);
        assert!(output.starts_with("operation context"));
        assert!(output.ends_with("?token=hidden"));
        let output = redact(&output);
        assert!(output.chars().count() <= 2000);
        assert!(output.contains("encoder failed"));
        assert!(!output.contains("hidden"));
        assert_eq!(redact(&"x".repeat(2 * LINE_LIMIT)), "[oversized token]");
    }

    #[tokio::test]
    async fn diagnostic_limits_never_split_the_context_that_redacts_credentials() -> Result<()> {
        for header in ["Cookie:", "Authorization:", "Proxy-Authorization:"] {
            for size in [DIAGNOSTIC_LIMIT * 2, LINE_LIMIT + 8192] {
                let payload = format!(
                    "{header} {} ordinary-secret-tail\nencoder failed",
                    "padding ".repeat(size / 8)
                );
                let output = read_diagnostics(payload.as_bytes(), None).await?;
                assert!(
                    !output.contains("ordinary-secret-tail"),
                    "{header} size={size}: {output}"
                );
                assert!(!redact(&output).contains("ordinary-secret-tail"));
                assert!(output.contains("encoder failed"));
                assert!(output.len() <= DIAGNOSTIC_LIMIT);
            }
        }
        Ok(())
    }

    #[cfg(windows)]
    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    #[ignore = "opt-in isolated Windows process resource cycles; requires HUIFA_RESOURCE_CYCLES=1"]
    async fn process_resource_cycles() -> Result<()> {
        use serde_json::json;
        use std::{
            ffi::c_void,
            sync::Mutex,
            time::{Duration, Instant},
        };
        use windows_sys::Win32::{
            Foundation::CloseHandle,
            System::Threading::{
                GetCurrentProcess, GetExitCodeProcess, GetProcessHandleCount, OpenProcess,
                PROCESS_QUERY_LIMITED_INFORMATION,
            },
        };
        if std::env::var("HUIFA_RESOURCE_CYCLES").as_deref() != Ok("1") {
            return Ok(());
        }
        #[repr(C)]
        #[derive(Default)]
        struct Memory {
            cb: u32,
            faults: u32,
            peak_working_set: usize,
            working_set: usize,
            peak_paged: usize,
            paged: usize,
            peak_nonpaged: usize,
            nonpaged: usize,
            pagefile: usize,
            peak_pagefile: usize,
            private: usize,
        }
        #[repr(C)]
        #[derive(Default)]
        struct ThreadEntry {
            size: u32,
            usage: u32,
            id: u32,
            owner: u32,
            base_priority: i32,
            delta_priority: i32,
            flags: u32,
        }
        #[link(name = "psapi")]
        unsafe extern "system" {
            fn GetProcessMemoryInfo(process: *mut c_void, counters: *mut Memory, size: u32) -> i32;
        }
        #[link(name = "kernel32")]
        unsafe extern "system" {
            fn CreateToolhelp32Snapshot(flags: u32, process: u32) -> *mut c_void;
            fn Thread32First(snapshot: *mut c_void, entry: *mut ThreadEntry) -> i32;
            fn Thread32Next(snapshot: *mut c_void, entry: *mut ThreadEntry) -> i32;
        }
        fn snapshot() -> Result<serde_json::Value> {
            let mut memory = Memory {
                cb: std::mem::size_of::<Memory>() as u32,
                ..Default::default()
            };
            let mut handles = 0;
            let mut threads = 0;
            unsafe {
                anyhow::ensure!(
                    GetProcessMemoryInfo(
                        GetCurrentProcess(),
                        &mut memory,
                        std::mem::size_of::<Memory>() as u32
                    ) != 0,
                    "Cannot measure process memory"
                );
                anyhow::ensure!(
                    GetProcessHandleCount(GetCurrentProcess(), &mut handles) != 0,
                    "Cannot measure process handles"
                );
                let list = CreateToolhelp32Snapshot(4, 0);
                anyhow::ensure!(list as isize != -1, "Cannot measure process threads");
                let mut entry = ThreadEntry {
                    size: std::mem::size_of::<ThreadEntry>() as u32,
                    ..Default::default()
                };
                if Thread32First(list, &mut entry) != 0 {
                    loop {
                        if entry.owner == std::process::id() {
                            threads += 1;
                        }
                        if Thread32Next(list, &mut entry) == 0 {
                            break;
                        }
                    }
                }
                CloseHandle(list);
            }
            Ok(
                json!({"private_bytes":memory.private,"working_set_bytes":memory.working_set,"handles":handles,"threads":threads}),
            )
        }
        fn running(pid: u32) -> bool {
            unsafe {
                let process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
                if process.is_null() {
                    return false;
                }
                let mut code = 0;
                let active = GetExitCodeProcess(process, &mut code) != 0 && code == 259;
                CloseHandle(process);
                active
            }
        }
        let root = tempfile::tempdir()?;
        let app = crate::App::open(crate::AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("downloads"),
            tool_dir: None,
        })
        .await?;
        let task = crate::tests::paused_task(Default::default());
        app.db.insert_task(&task)?;
        let mut warmup = Vec::new();
        let mut measured = Vec::new();
        let mut baseline = None;
        let warmup_rounds = 4;
        for round in 0usize..warmup_rounds + 30 {
            let index = round.saturating_sub(warmup_rounds);
            let kind = if round == 3 || (round >= warmup_rounds && matches!(index, 10 | 25)) {
                "large_stderr_line"
            } else {
                ["complete", "cancel", "drop"][round % 3]
            };
            let cancel = CancellationToken::new();
            let stopped = cancel.clone();
            let observed = Arc::new(Mutex::new(Vec::new()));
            let pids = observed.clone();
            let (ready, started) = tokio::sync::oneshot::channel();
            let ready = Mutex::new(Some(ready));
            let callback: LineCallback = Arc::new(move |error, line| {
                if error {
                    return;
                }
                if let Some(pid) = line
                    .strip_prefix("child:")
                    .or_else(|| line.strip_prefix("ready:"))
                    .and_then(|pid| pid.parse::<u32>().ok())
                {
                    pids.lock().unwrap().push(pid);
                }
                if line.starts_with("ready:") {
                    if kind == "cancel" {
                        stopped.cancel();
                    }
                    if let Some(ready) = ready.lock().unwrap().take() {
                        let _ = ready.send(());
                    }
                }
            });
            let descendant = "$child = Start-Process -FilePath powershell.exe -ArgumentList '-NoProfile','-NonInteractive','-Command','Start-Sleep -Seconds 30' -WindowStyle Hidden -PassThru; [Console]::Out.WriteLine('child:' + $child.Id); ";
            let script = match kind {
                "complete" => "[Console]::Out.WriteLine('ready:' + $PID); [Console]::Error.WriteLine('fixture diagnostic'); [Console]::Out.WriteLine('completed')".to_string(),
                "large_stderr_line" => "[Console]::Out.WriteLine('ready:' + $PID); $chunk = [Text.Encoding]::ASCII.GetBytes(('x' * 65536)); $stream = [Console]::OpenStandardError(); for ($i = 0; $i -lt 1024; $i++) { $stream.Write($chunk, 0, $chunk.Length) }; [Console]::Error.WriteLine('large-line-finished')".to_string(),
                _ => format!("{descendant}[Console]::Out.WriteLine('ready:' + $PID); Start-Sleep -Seconds 30"),
            };
            let args = vec![
                "-NoProfile".into(),
                "-NonInteractive".into(),
                "-Command".into(),
                script,
            ];
            let log = app.raw_task_log(&task.id)?;
            let running_cancel = cancel.clone();
            let started_at = Instant::now();
            let handle = tokio::spawn(async move {
                run_logged_streamed(
                    Path::new("powershell.exe"),
                    &args,
                    None,
                    &running_cancel,
                    Some(callback),
                    log,
                )
                .await
            });
            tokio::time::timeout(Duration::from_secs(5), started).await??;
            if kind == "drop" {
                handle.abort();
                assert!(handle.await.unwrap_err().is_cancelled());
            } else {
                let result = tokio::time::timeout(Duration::from_secs(20), handle).await??;
                if kind == "cancel" {
                    assert!(result.unwrap_err().to_string().contains("interrupted"));
                } else {
                    result?;
                }
            }
            app.task_log_writer.flush()?;
            let children = observed.lock().unwrap().clone();
            let deadline = Instant::now() + Duration::from_secs(3);
            while children.iter().any(|pid| running(*pid)) && Instant::now() < deadline {
                tokio::time::sleep(Duration::from_millis(20)).await;
            }
            let remaining = children
                .iter()
                .copied()
                .filter(|pid| running(*pid))
                .collect::<Vec<_>>();
            anyhow::ensure!(
                remaining.is_empty(),
                "Owned fixture processes survived {kind}: {remaining:?}"
            );
            // Aborting an owning future schedules its pipe readers for cancellation;
            // allow overlapped I/O completions to settle before sampling handles.
            tokio::time::sleep(Duration::from_millis(100)).await;
            let mut sample = snapshot()?;
            sample["round"] = json!(round);
            sample["kind"] = json!(kind);
            sample["elapsed_ms"] = json!(started_at.elapsed().as_secs_f64() * 1000.0);
            sample["remaining_owned_processes"] = json!(remaining);
            if round < warmup_rounds {
                warmup.push(sample);
            } else {
                measured.push(sample);
            }
            if round + 1 == warmup_rounds {
                baseline = Some(snapshot()?);
            }
        }
        let baseline = baseline.unwrap();
        let final_sample = snapshot()?;
        let mut report = json!({"platform":std::env::consts::OS,"architecture":std::env::consts::ARCH,"profile":if cfg!(debug_assertions){"debug"}else{"release"},"pid":std::process::id(),"scope":"isolated local PowerShell fixtures; fixed application directory; samples after process-tree/journal drain and 100ms for overlapped I/O cancellation; allocator/OS caches are not forced","warmup_rounds":warmup_rounds,"measured_rounds":30,"stdout_limit":STDOUT_LIMIT,"line_limit":LINE_LIMIT,"diagnostic_limit":DIAGNOSTIC_LIMIT,"large_stderr_bytes_per_round":64*1024*1024,"baseline":baseline,"final":final_sample,"warmup":warmup,"samples":measured,"all_owned_processes_exited":true,"journal_bytes":std::fs::metadata(app.config.data_dir.join("task-logs").join(format!("{}.log",task.id)))?.len()});
        app.shutdown().await?;
        tokio::time::sleep(Duration::from_millis(100)).await;
        report["after_shutdown"] = snapshot()?;
        let output = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../output/performance-20261020/process-resource-cycles.json");
        std::fs::create_dir_all(output.parent().unwrap())?;
        std::fs::write(&output, serde_json::to_vec_pretty(&report)?)?;
        println!("Process resource cycles: {}", output.display());
        Ok(())
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

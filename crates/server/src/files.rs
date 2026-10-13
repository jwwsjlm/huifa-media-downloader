use anyhow::{Context, Result, ensure};
use axum::{
    body::Body,
    http::{HeaderMap, HeaderValue, StatusCode, header},
    response::Response,
};
use std::{
    fs::OpenOptions,
    path::{Component, Path, PathBuf},
};
use tokio::io::AsyncReadExt;
use tokio_util::io::ReaderStream;

pub fn open_owned(path: &Path, roots: &[PathBuf]) -> Result<std::fs::File> {
    let root = roots
        .iter()
        .find(|root| path.starts_with(root))
        .context("file is outside owned storage")?;
    let relative = path.strip_prefix(root)?;
    ensure!(
        !relative.as_os_str().is_empty(),
        "file ID does not identify a file"
    );
    let mut current = root.clone();
    ensure!(
        !std::fs::symlink_metadata(root)?.file_type().is_symlink(),
        "storage root must not be a link"
    );
    for component in relative.components() {
        let Component::Normal(name) = component else {
            anyhow::bail!("invalid file path")
        };
        current.push(name);
        let metadata = std::fs::symlink_metadata(&current)?;
        ensure!(
            !metadata.file_type().is_symlink(),
            "links cannot be downloaded"
        );
        #[cfg(windows)]
        {
            use std::os::windows::fs::MetadataExt;
            ensure!(
                metadata.file_attributes() & 0x400 == 0,
                "reparse points cannot be downloaded"
            );
        }
    }
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
    }
    let file = options.open(path)?;
    let metadata = file.metadata()?;
    ensure!(metadata.is_file(), "only regular files may be downloaded");
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        ensure!(
            metadata.uid() == unsafe { libc::geteuid() },
            "file must belong to the application user"
        );
    }
    // The opened handle is checked too, so a concurrent directory swap cannot escape storage.
    let opened = opened_path(&file, path)?;
    ensure!(
        opened.starts_with(root.canonicalize()?),
        "opened file is outside owned storage"
    );
    Ok(file)
}

#[cfg(target_os = "linux")]
fn opened_path(file: &std::fs::File, _path: &Path) -> Result<PathBuf> {
    use std::os::fd::AsRawFd;
    Ok(std::fs::canonicalize(format!(
        "/proc/self/fd/{}",
        file.as_raw_fd()
    ))?)
}

#[cfg(windows)]
fn opened_path(file: &std::fs::File, _path: &Path) -> Result<PathBuf> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Storage::FileSystem::GetFinalPathNameByHandleW;
    let mut buffer = vec![0u16; 32768];
    let length = unsafe {
        GetFinalPathNameByHandleW(
            file.as_raw_handle() as _,
            buffer.as_mut_ptr(),
            buffer.len() as u32,
            0,
        )
    };
    ensure!(
        length > 0 && length < buffer.len() as u32,
        "cannot validate opened file"
    );
    Ok(PathBuf::from(String::from_utf16(
        &buffer[..length as usize],
    )?))
}

#[cfg(not(any(target_os = "linux", windows)))]
fn opened_path(_file: &std::fs::File, path: &Path) -> Result<PathBuf> {
    Ok(path.canonicalize()?)
}

pub async fn download(
    file: std::fs::File,
    path: &Path,
    mime: &str,
    headers: &HeaderMap,
) -> Result<Response, StatusCode> {
    let length = file.metadata().map_err(|_| StatusCode::NOT_FOUND)?.len();
    let (start, end, partial) = match headers.get(header::RANGE) {
        Some(range) => {
            let range = range
                .to_str()
                .map_err(|_| StatusCode::RANGE_NOT_SATISFIABLE)?;
            let (start, end) =
                parse_range(range, length).ok_or(StatusCode::RANGE_NOT_SATISFIABLE)?;
            (start, end, true)
        }
        None => (0, length.saturating_sub(1), false),
    };
    let amount = if length == 0 { 0 } else { end - start + 1 };
    let mut file = tokio::fs::File::from_std(file);
    tokio::io::AsyncSeekExt::seek(&mut file, std::io::SeekFrom::Start(start))
        .await
        .map_err(|_| StatusCode::NOT_FOUND)?;
    let mut response = Response::new(Body::from_stream(ReaderStream::new(file.take(amount))));
    *response.status_mut() = if partial {
        StatusCode::PARTIAL_CONTENT
    } else {
        StatusCode::OK
    };
    let filename: String = path
        .file_name()
        .unwrap_or_default()
        .to_string_lossy()
        .chars()
        .filter(|c| c.is_ascii() && !c.is_control() && !matches!(c, '"' | '\\'))
        .take(200)
        .collect();
    let response_headers = response.headers_mut();
    response_headers.insert(
        header::CONTENT_TYPE,
        HeaderValue::from_str(mime).unwrap_or(HeaderValue::from_static("application/octet-stream")),
    );
    response_headers.insert(header::CONTENT_LENGTH, amount.into());
    response_headers.insert(header::ACCEPT_RANGES, HeaderValue::from_static("bytes"));
    response_headers.insert(
        header::CONTENT_DISPOSITION,
        HeaderValue::from_str(&format!("inline; filename=\"{filename}\"")).unwrap(),
    );
    if partial {
        response_headers.insert(
            header::CONTENT_RANGE,
            HeaderValue::from_str(&format!("bytes {start}-{end}/{length}")).unwrap(),
        );
    }
    Ok(response)
}

fn parse_range(range: &str, length: u64) -> Option<(u64, u64)> {
    if length == 0 {
        return None;
    }
    let (start, end) = range.strip_prefix("bytes=")?.split_once('-')?;
    if start.is_empty() {
        let suffix = end.parse::<u64>().ok()?;
        if suffix == 0 {
            return None;
        }
        return Some((length.saturating_sub(suffix), length - 1));
    }
    let start = start.parse::<u64>().ok()?;
    let end = if end.is_empty() {
        length - 1
    } else {
        end.parse::<u64>().ok()?.min(length - 1)
    };
    (start < length && end >= start).then_some((start, end))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn owned_paths_and_ranges() -> Result<()> {
        let owned = tempfile::tempdir()?;
        let outside = tempfile::NamedTempFile::new()?;
        let path = owned.path().join("video.mp4");
        std::fs::write(&path, b"media")?;
        assert!(open_owned(&path, &[owned.path().into()]).is_ok());
        assert!(open_owned(outside.path(), &[owned.path().into()]).is_err());
        assert!(open_owned(&owned.path().join("../escape"), &[owned.path().into()]).is_err());
        #[cfg(unix)]
        {
            std::os::unix::fs::symlink(outside.path(), owned.path().join("link"))?;
            assert!(open_owned(&owned.path().join("link"), &[owned.path().into()]).is_err());
        }
        assert_eq!(parse_range("bytes=2-4", 10), Some((2, 4)));
        assert_eq!(parse_range("bytes=-3", 10), Some((7, 9)));
        assert_eq!(parse_range("bytes=0-1,4-5", 10), None);
        assert_eq!(parse_range("bytes=10-", 10), None);
        Ok(())
    }
}

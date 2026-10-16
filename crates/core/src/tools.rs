use crate::App;
use anyhow::{Context, Result, bail};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, HashMap},
    io::{Read, Write},
    path::{Path, PathBuf},
};

fn executable(name: &str) -> String {
    format!("{name}{}", std::env::consts::EXE_SUFFIX)
}

fn file_digest(path: &Path) -> Result<String> {
    let mut file = std::fs::File::open(path)?;
    let metadata = file.metadata()?;
    anyhow::ensure!(
        metadata.is_file() && metadata.len() > 0 && metadata.len() <= 512 * 1024 * 1024,
        "Invalid runtime executable size"
    );
    let mut digest = Sha256::new();
    let mut buffer = [0u8; 65536];
    loop {
        let count = file.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        digest.update(&buffer[..count]);
    }
    Ok(hex::encode(digest.finalize()))
}

pub(crate) fn freeze(app: &App) -> Result<HashMap<String, PathBuf>> {
    let mut sources = BTreeMap::new();
    for name in ["yt-dlp", "ffmpeg", "ffprobe"] {
        sources.insert(name.to_string(), resolve(app, name)?);
    }
    if let Ok(path) = resolve(app, "deno") {
        sources.insert("deno".into(), path);
    }
    let hashes = sources
        .iter()
        .map(|(name, path)| Ok((name.clone(), file_digest(path)?)))
        .collect::<Result<BTreeMap<String, String>>>()?;
    let manifest = serde_json::to_vec(&hashes)?;
    let directory = app.config.data_dir.join("tools/frozen");
    std::fs::create_dir_all(&directory)?;
    let destination = directory.join(hex::encode(Sha256::digest(&manifest)));
    if !destination.exists() {
        let temporary = tempfile::tempdir_in(&directory)?;
        for (name, source) in &sources {
            let target = temporary.path().join(executable(name));
            let mut input = std::fs::File::open(source)?;
            let mut output = std::fs::File::create(&target)?;
            std::io::copy(&mut input, &mut output)?;
            output.sync_all()?;
            #[cfg(unix)]
            std::fs::set_permissions(&target, input.metadata()?.permissions())?;
            anyhow::ensure!(
                file_digest(&target)? == hashes[name],
                "Runtime changed while freezing {name}"
            );
        }
        let manifest_path = temporary.path().join("manifest.json");
        let mut manifest_file = std::fs::File::create(&manifest_path)?;
        manifest_file.write_all(&manifest)?;
        manifest_file.sync_all()?;
        drop(manifest_file);
        #[cfg(unix)]
        std::fs::File::open(temporary.path())?.sync_all()?;
        if let Err(error) = std::fs::rename(temporary.path(), &destination)
            && !destination.is_dir()
        {
            return Err(error.into());
        }
        #[cfg(unix)]
        std::fs::File::open(&directory)?.sync_all()?;
    }
    anyhow::ensure!(
        std::fs::read(destination.join("manifest.json"))? == manifest,
        "Existing frozen runtime set differs"
    );
    let mut paths = HashMap::new();
    for name in sources.keys() {
        let path = destination.join(executable(name)).canonicalize()?;
        validate_frozen(app, &path, name)?;
        paths.insert(name.clone(), path);
    }
    Ok(paths)
}

pub(crate) fn validate_frozen(app: &App, path: &Path, name: &str) -> Result<()> {
    anyhow::ensure!(
        matches!(name, "yt-dlp" | "ffmpeg" | "ffprobe" | "deno"),
        "Unknown frozen runtime"
    );
    let path = path.canonicalize()?;
    let directory = path.parent().context("Invalid frozen runtime path")?;
    let root = app.config.data_dir.join("tools/frozen").canonicalize()?;
    anyhow::ensure!(
        directory.parent() == Some(root.as_path())
            && path.file_name().and_then(|value| value.to_str()) == Some(executable(name).as_str()),
        "Task runtime is outside its immutable tool cache"
    );
    let manifest_path = directory.join("manifest.json");
    anyhow::ensure!(
        std::fs::metadata(&manifest_path)?.len() <= 1024,
        "Frozen runtime manifest is too large"
    );
    let manifest = std::fs::read(&manifest_path)?;
    let expected_directory = hex::encode(Sha256::digest(&manifest));
    anyhow::ensure!(
        directory.file_name().and_then(|value| value.to_str()) == Some(expected_directory.as_str()),
        "Frozen runtime manifest checksum mismatch"
    );
    let hashes: BTreeMap<String, String> = serde_json::from_slice(&manifest)?;
    anyhow::ensure!(
        hashes.get(name) == Some(&file_digest(&path)?),
        "Frozen {name} runtime checksum mismatch; restore its saved tool version before retrying"
    );
    Ok(())
}

pub(crate) fn resolve(app: &App, name: &str) -> Result<PathBuf> {
    if !matches!(name, "yt-dlp" | "ffmpeg" | "ffprobe" | "deno") {
        bail!("Unknown runtime tool");
    }
    let executable = executable(name);
    if let Some(path) = managed_path(&app.config.data_dir.join("tools"), name)? {
        return Ok(path);
    }
    if let Some(root) = &app.config.tool_dir {
        for path in [
            root.join(&executable),
            root.join(name).join("x64").join(&executable),
            root.join(name).join(&executable),
        ] {
            if path.is_file() {
                return Ok(path);
            }
        }
    }
    if let Ok(path) = std::env::var("PATH") {
        for directory in std::env::split_paths(&path) {
            let path = directory.join(&executable);
            if path.is_file() {
                return Ok(path);
            }
        }
    }
    bail!("{name} is not installed. Choose a runtime tools folder or install it on PATH")
}

fn managed_path(directory: &Path, name: &str) -> Result<Option<PathBuf>> {
    let pointer = directory.join("current.json");
    if !pointer.is_file() {
        return Ok(None);
    }
    let current: Value = serde_json::from_slice(&std::fs::read(&pointer)?)?;
    let Some(relative) = current[name].as_str() else {
        return Ok(None);
    };
    let relative = Path::new(relative);
    anyhow::ensure!(
        !relative.is_absolute()
            && relative
                .components()
                .all(|part| matches!(part, std::path::Component::Normal(_))),
        "Invalid managed tool reference"
    );
    let path = directory.join(relative);
    let canonical = path.canonicalize()?;
    anyhow::ensure!(
        canonical.starts_with(directory.canonicalize()?) && canonical.is_file(),
        "Managed runtime tool is outside its installation directory"
    );
    Ok(Some(canonical))
}

fn release_source(name: &str, channel: &str, version: &str) -> Result<(&'static str, String)> {
    let channel = if channel.is_empty() {
        "stable"
    } else {
        channel
    };
    let version = version.trim();
    anyhow::ensure!(
        version.len() <= 64
            && (version.is_empty()
                || version.starts_with(|c: char| c.is_ascii_alphanumeric())
                    && version
                        .chars()
                        .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '-' | '_'))
                    && !version.contains("..")),
        "Invalid runtime version; enter an official release tag, not a URL"
    );
    let repository = match (name, channel) {
        ("yt-dlp", "stable") => "yt-dlp/yt-dlp",
        ("yt-dlp", "nightly") => "yt-dlp/yt-dlp-nightly-builds",
        ("yt-dlp", _) => bail!("Unknown yt-dlp channel; choose stable or nightly"),
        ("ffmpeg" | "ffprobe", "stable") if version.is_empty() => "yt-dlp/FFmpeg-Builds",
        ("deno", "stable") if version.is_empty() => "denoland/deno",
        ("ffmpeg" | "ffprobe" | "deno", _) => {
            bail!("Channel and version selection is only supported for yt-dlp")
        }
        _ => bail!("Unknown runtime tool"),
    };
    let (origin, release) = if name == "yt-dlp" {
        (
            "https://github.com",
            if version.is_empty() {
                "latest/download".into()
            } else {
                format!("download/{version}")
            },
        )
    } else {
        ("https://api.github.com/repos", "latest".into())
    };
    Ok((
        repository,
        format!("{origin}/{repository}/releases/{release}"),
    ))
}

pub(crate) async fn update(app: &App, args: &Value) -> Result<Value> {
    let name = args["name"]
        .as_str()
        .context("Runtime tool name must be text")?;
    let channel = args
        .get("channel")
        .map(|v| v.as_str().context("Runtime channel must be text"))
        .transpose()?
        .unwrap_or("stable");
    let version = args
        .get("version")
        .map(|v| v.as_str().context("Runtime version must be text"))
        .transpose()?
        .unwrap_or("");
    let (repository, release_url) = release_source(name, channel, version)?;
    let _guard = tokio::select! {
        guard=app.tool_update_lock.lock()=>guard,
        _=app.shutdown_token.cancelled()=>bail!("Runtime update interrupted"),
    };
    tokio::select! {
        result=update_inner(app,name,repository,&release_url)=>result,
        _=app.shutdown_token.cancelled()=>bail!("Runtime update interrupted"),
    }
}

fn checksum_for(sums: &str, filename: &str) -> Result<String> {
    let mut digest = None;
    for line in sums.lines() {
        let mut fields = line.split_whitespace();
        let (Some(hash), Some(name)) = (fields.next(), fields.next()) else {
            continue;
        };
        if name.trim_start_matches('*') == filename {
            anyhow::ensure!(digest.is_none(), "Duplicate official runtime checksum");
            anyhow::ensure!(
                fields.next().is_none()
                    && hash.len() == 64
                    && hash.chars().all(|c| c.is_ascii_hexdigit()),
                "Invalid official runtime checksum"
            );
            digest = Some(hash.to_ascii_lowercase());
        }
    }
    digest.context("Official runtime checksum is missing for this platform; installation refused")
}

async fn update_inner(app: &App, name: &str, repository: &str, release_url: &str) -> Result<Value> {
    use anyhow::{Context, ensure};
    use std::io::Write;
    let platform = target()?;
    let asset_name = match (name, platform.as_str()) {
        ("yt-dlp", "windows-x86_64") => "yt-dlp.exe",
        ("yt-dlp", "linux-x86_64") => "yt-dlp_linux",
        ("yt-dlp", "linux-aarch64") => "yt-dlp_linux_aarch64",
        ("ffmpeg" | "ffprobe", "windows-x86_64") => "ffmpeg-master-latest-win64-gpl.zip",
        ("ffmpeg" | "ffprobe", "linux-x86_64") => "ffmpeg-master-latest-linux64-gpl.tar.xz",
        ("ffmpeg" | "ffprobe", "linux-aarch64") => "ffmpeg-master-latest-linuxarm64-gpl.tar.xz",
        ("deno", "windows-x86_64") => "deno-x86_64-pc-windows-msvc.zip",
        ("deno", "linux-x86_64") => "deno-x86_64-unknown-linux-gnu.zip",
        ("deno", "linux-aarch64") => "deno-aarch64-unknown-linux-gnu.zip",
        _ => bail!("Unsupported runtime architecture"),
    };
    let client = reqwest::Client::builder()
        .user_agent(concat!("HuifaDownloader/", env!("CARGO_PKG_VERSION")))
        .connect_timeout(std::time::Duration::from_secs(10))
        .timeout(std::time::Duration::from_secs(300))
        .build()?;
    let release: Value = if name == "yt-dlp" {
        // Official release checksum assets avoid the unauthenticated GitHub API quota.
        let base = release_url;
        let response = client
            .get(format!("{base}/SHA2-256SUMS"))
            .send()
            .await
            .map_err(|e| e.without_url())
            .context("Cannot download official yt-dlp checksums from GitHub")?;
        if response.status() == reqwest::StatusCode::NOT_FOUND {
            bail!("Runtime release not found for yt-dlp; check the channel and exact release tag");
        }
        ensure!(
            response.status().is_success(),
            "Cannot download official yt-dlp runtime checksums from GitHub: HTTP {}",
            response.status().as_u16()
        );
        let sums = String::from_utf8(bounded_response(response, 256 * 1024).await?)
            .context("Invalid official runtime checksum file")?;
        let digest = checksum_for(&sums, asset_name)?;
        json!({"assets":[{"name":asset_name,"digest":format!("sha256:{digest}"),"browser_download_url":format!("{base}/{asset_name}")}]})
    } else {
        let response = client
            .get(release_url)
            .send()
            .await
            .map_err(|e| e.without_url())
            .with_context(|| format!("Cannot connect to GitHub to query {name} release"))?;
        ensure!(
            response.status().is_success(),
            "Cannot query {name} runtime release from GitHub: HTTP {}",
            response.status().as_u16()
        );
        serde_json::from_slice(&bounded_response(response, 2 * 1024 * 1024).await?)?
    };
    let asset = release["assets"]
        .as_array()
        .context("Malformed runtime release")?
        .iter()
        .find(|asset| asset["name"].as_str() == Some(asset_name))
        .context("Runtime asset is unavailable for this platform")?;
    let digest = asset["digest"]
        .as_str()
        .and_then(|v| v.strip_prefix("sha256:"))
        .context("Runtime release lacks a trusted checksum; installation refused")?;
    ensure!(
        digest.len() == 64 && digest.chars().all(|c| c.is_ascii_hexdigit()),
        "Invalid runtime checksum"
    );
    let url = url::Url::parse(
        asset["browser_download_url"]
            .as_str()
            .context("Runtime download URL is missing")?,
    )?;
    ensure!(
        url.scheme() == "https"
            && url.host_str() == Some("github.com")
            && (url
                .path()
                .starts_with(&format!("/{repository}/releases/download/"))
                || name == "yt-dlp"
                    && url.path()
                        == format!("/{repository}/releases/latest/download/{asset_name}")),
        "Unexpected runtime download origin"
    );
    let response = client
        .get(url)
        .send()
        .await
        .map_err(|e| e.without_url())
        .with_context(|| format!("Cannot download {name} runtime from GitHub"))?;
    ensure!(
        response.status().is_success(),
        "Runtime download failed for {name}: HTTP {}",
        response.status().as_u16()
    );
    let archive = bounded_response(response, 512 * 1024 * 1024).await?;
    ensure!(
        hex::encode(Sha256::digest(&archive)) == digest.to_ascii_lowercase(),
        "Runtime archive checksum mismatch"
    );
    let directory = app.config.data_dir.join("tools");
    std::fs::create_dir_all(&directory)?;
    let group = if name == "ffprobe" { "ffmpeg" } else { name };
    let version_dir = directory.join(format!("{group}-{}", &digest[..16]));
    let temporary = tempfile::tempdir_in(&directory)?;
    let wanted: Vec<String> = if group == "ffmpeg" {
        vec!["ffmpeg".into(), "ffprobe".into()]
    } else {
        vec![name.into()]
    };
    let filenames = wanted
        .iter()
        .map(|name| format!("{name}{}", std::env::consts::EXE_SUFFIX))
        .collect::<Vec<_>>();
    let extraction_dir = temporary.path().to_path_buf();
    let format = asset_name.to_string();
    let extracted_names = filenames.clone();
    tokio::task::spawn_blocking(move || {
        extract_archive(&archive, &format, &extracted_names, &extraction_dir)
    })
    .await??;
    for (tool, filename) in wanted.iter().zip(&filenames) {
        let path = temporary.path().join(filename);
        let header = std::fs::read(&path)?;
        ensure!(
            if cfg!(windows) {
                header.starts_with(b"MZ")
            } else {
                header.starts_with(b"\x7fELF")
            },
            "Runtime payload is not a native executable"
        );
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755))?;
        }
        crate::process::run(
            &path,
            &[if tool == "ffmpeg" || tool == "ffprobe" {
                "-version".into()
            } else {
                "--version".into()
            }],
            None,
            &app.shutdown_token,
            None,
        )
        .await?;
        if tool == "yt-dlp" {
            let fixture = temporary.path().join("compatibility.json");
            std::fs::write(&fixture, br#"{"id":"compatibility","title":"Compatibility","extractor":"generic","webpage_url":"https://example.invalid/compatibility.mp4","url":"https://example.invalid/compatibility.mp4","ext":"mp4","format_id":"compatibility","protocol":"https","vcodec":"avc1","acodec":"mp4a.40.2"}"#)?;
            crate::process::run(&path, &[
                "--ignore-config".into(), "--no-plugin-dirs".into(), "--no-cache-dir".into(),
                "--no-js-runtimes".into(), "--encoding".into(), "utf-8".into(),
                "--simulate".into(), "--skip-download".into(), "--progress-template".into(), "download:%(progress)j".into(),
                "--print".into(), "after_move:%(filepath)j".into(), "--load-info-json".into(), fixture.to_string_lossy().into_owned(),
            ], Some(temporary.path()), &app.shutdown_token, None).await.context("This yt-dlp version is incompatible with required application options; choose a newer release")?;
            std::fs::remove_file(fixture)?;
        }
        std::fs::OpenOptions::new()
            .write(true)
            .open(&path)?
            .sync_all()?;
    }
    if version_dir.exists() {
        for filename in &filenames {
            ensure!(
                std::fs::read(version_dir.join(filename))?
                    == std::fs::read(temporary.path().join(filename))?,
                "Existing runtime version differs; refusing replacement"
            );
        }
    } else {
        std::fs::rename(temporary.path(), &version_dir)?;
    }
    let mut current = if directory.join("current.json").is_file() {
        serde_json::from_slice::<Value>(&std::fs::read(directory.join("current.json"))?)?
    } else {
        json!({})
    };
    for (tool, filename) in wanted.iter().zip(&filenames) {
        current[tool] = json!(format!(
            "{}/{}",
            version_dir.file_name().unwrap().to_string_lossy(),
            filename
        ));
    }
    let mut pointer = tempfile::NamedTempFile::new_in(&directory)?;
    pointer.write_all(&serde_json::to_vec(&current)?)?;
    pointer.as_file().sync_all()?;
    pointer
        .persist(directory.join("current.json"))
        .map_err(|e| e.error)?;
    #[cfg(unix)]
    std::fs::File::open(&directory)?.sync_all()?;
    let value = check(app).await?;
    app.emit("tools.updated", value.clone());
    Ok(value)
}

fn target() -> Result<String> {
    let os = if cfg!(windows) {
        "windows"
    } else if cfg!(target_os = "linux") {
        "linux"
    } else {
        bail!("Unsupported runtime platform")
    };
    let arch = match std::env::consts::ARCH {
        "x86_64" => "x86_64",
        "aarch64" => "aarch64",
        _ => bail!("Unsupported runtime architecture"),
    };
    Ok(format!("{os}-{arch}"))
}
async fn bounded_response(mut response: reqwest::Response, limit: usize) -> Result<Vec<u8>> {
    anyhow::ensure!(
        response.content_length().is_none_or(|n| n <= limit as u64),
        "Runtime response is too large"
    );
    let mut bytes = Vec::new();
    while let Some(chunk) = response.chunk().await.map_err(|e| e.without_url())? {
        anyhow::ensure!(
            bytes.len() + chunk.len() <= limit,
            "Runtime response is too large"
        );
        bytes.extend_from_slice(&chunk);
    }
    Ok(bytes)
}
fn extract_archive(
    archive: &[u8],
    name: &str,
    wanted: &[String],
    destination: &Path,
) -> Result<()> {
    use std::io::{Read, Write};
    let mut found = std::collections::HashSet::new();
    let mut save = |path: &Path, reader: &mut dyn Read, size: u64| -> Result<()> {
        anyhow::ensure!(
            path.components()
                .all(|part| matches!(part, std::path::Component::Normal(_))),
            "Unsafe runtime archive path"
        );
        let filename = path.file_name().and_then(|v| v.to_str()).unwrap_or("");
        if !wanted.iter().any(|wanted| wanted == filename) {
            return Ok(());
        }
        anyhow::ensure!(
            found.insert(filename.to_string()),
            "Duplicate runtime archive entry"
        );
        anyhow::ensure!(size <= 256 * 1024 * 1024, "Runtime binary is too large");
        let mut file = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(destination.join(filename))?;
        let written = std::io::copy(&mut reader.take(256 * 1024 * 1024 + 1), &mut file)?;
        anyhow::ensure!(written <= 256 * 1024 * 1024, "Runtime binary is too large");
        file.flush()?;
        Ok(())
    };
    if name.ends_with(".zip") {
        let mut archive = zip::ZipArchive::new(std::io::Cursor::new(archive))?;
        for i in 0..archive.len() {
            let mut entry = archive.by_index(i)?;
            let path = entry
                .enclosed_name()
                .context("Unsafe runtime ZIP entry")?
                .to_path_buf();
            anyhow::ensure!(!entry.is_symlink(), "Runtime ZIP contains a symbolic link");
            let size = entry.size();
            if entry.is_file() {
                save(&path, &mut entry, size)?;
            }
        }
    } else if name.ends_with(".tar.xz") {
        let mut archive =
            tar::Archive::new(xz2::read::XzDecoder::new(std::io::Cursor::new(archive)));
        for entry in archive.entries()? {
            let mut entry = entry?;
            let path = entry.path()?.into_owned();
            anyhow::ensure!(
                !entry.header().entry_type().is_symlink()
                    && !entry.header().entry_type().is_hard_link(),
                "Runtime archive contains a link"
            );
            if entry.header().entry_type().is_file() {
                let size = entry.size();
                save(&path, &mut entry, size)?;
            }
        }
    } else {
        anyhow::ensure!(wanted.len() == 1, "Invalid standalone runtime");
        let mut reader = std::io::Cursor::new(archive);
        save(Path::new(&wanted[0]), &mut reader, archive.len() as u64)?;
    }
    anyhow::ensure!(
        found.len() == wanted.len(),
        "Runtime archive does not contain every required binary"
    );
    Ok(())
}

pub(crate) async fn check(app: &App) -> Result<Value> {
    let mut tools = Vec::new();
    for name in ["yt-dlp", "ffmpeg", "ffprobe", "deno"] {
        match resolve(app, name) {
            Ok(path) => {
                let flag = if name == "ffmpeg" || name == "ffprobe" {
                    "-version"
                } else {
                    "--version"
                };
                let result =
                    crate::process::run(&path, &[flag.into()], None, &app.shutdown_token, None)
                        .await;
                tools.push(match result {Ok(output)=>json!({"name":name,"available":true,"version":output.stdout.lines().next().unwrap_or("")}),Err(error)=>json!({"name":name,"available":false,"error":crate::process::redact(&format!("{error:#}"))})});
            }
            Err(error) => {
                tools.push(json!({"name":name,"available":false,"error":crate::process::redact(&format!("{error:#}"))}))
            }
        }
    }
    Ok(json!(tools))
}

pub(crate) fn mime(path: &Path) -> &'static str {
    match path
        .extension()
        .and_then(|s| s.to_str())
        .unwrap_or("")
        .to_ascii_lowercase()
        .as_str()
    {
        "mp4" => "video/mp4",
        "mkv" => "video/x-matroska",
        "webm" => "video/webm",
        "m4a" => "audio/mp4",
        "mp3" => "audio/mpeg",
        "wav" => "audio/wav",
        "flac" => "audio/flac",
        "opus" | "ogg" => "audio/ogg",
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "webp" => "image/webp",
        "json" => "application/json",
        "vtt" => "text/vtt; charset=utf-8",
        "srt" | "ass" | "lrc" | "txt" => "text/plain; charset=utf-8",
        _ => "application/octet-stream",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn invalid_update_arguments_do_not_change_the_installed_tools() -> Result<()> {
        let directory = tempfile::tempdir()?;
        let app = App::open(crate::AppConfig {
            data_dir: directory.path().join("data"),
            download_dir: directory.path().join("downloads"),
            tool_dir: None,
        })
        .await?;
        let tools = app.config.data_dir.join("tools");
        std::fs::create_dir_all(&tools)?;
        let pointer = tools.join("current.json");
        std::fs::write(&pointer, b"{}")?;
        for args in [
            json!({"name":"yt-dlp","channel":1}),
            json!({"name":"yt-dlp","version":[]}),
            json!({"name":"yt-dlp","channel":"other/repo"}),
            json!({"name":"yt-dlp","version":"../latest"}),
        ] {
            assert!(update(&app, &args).await.is_err());
            assert_eq!(std::fs::read(&pointer)?, b"{}");
        }
        app.shutdown().await?;
        Ok(())
    }
    #[test]
    fn official_checksums_require_one_exact_valid_entry() -> Result<()> {
        let digest = "ab".repeat(32);
        let sums = format!("{}  yt-dlp_linux\n{digest} *yt-dlp.exe\n", "cd".repeat(32));
        assert_eq!(checksum_for(&sums, "yt-dlp.exe")?, digest);
        assert!(checksum_for(&sums, "yt-dlp").is_err());
        assert!(checksum_for(&format!("{sums}{digest} yt-dlp.exe\n"), "yt-dlp.exe").is_err());
        assert!(checksum_for("not-a-digest yt-dlp.exe", "yt-dlp.exe").is_err());
        assert!(checksum_for(&format!("{digest} yt-dlp.exe extra"), "yt-dlp.exe").is_err());
        Ok(())
    }
    #[test]
    fn runtime_selection_stays_in_official_repositories() -> Result<()> {
        assert_eq!(
            release_source("yt-dlp", "", "")?.1,
            "https://github.com/yt-dlp/yt-dlp/releases/latest/download"
        );
        assert_eq!(
            release_source("yt-dlp", "nightly", "")?.1,
            "https://github.com/yt-dlp/yt-dlp-nightly-builds/releases/latest/download"
        );
        assert_eq!(
            release_source("yt-dlp", "stable", " 2026.08.19 ")?.1,
            "https://github.com/yt-dlp/yt-dlp/releases/download/2026.08.19"
        );
        assert!(
            release_source("yt-dlp", "nightly", "2026.10.10.233141")?
                .1
                .ends_with("/download/2026.10.10.233141")
        );
        for version in [
            "../latest",
            "https://example.com/tool",
            "a/b",
            "a?x=b",
            "a#x",
            "a%2fb",
            "--exec",
            "a b",
        ] {
            assert!(
                release_source("yt-dlp", "stable", version).is_err(),
                "{version}"
            );
        }
        assert!(release_source("yt-dlp", "other/repository", "").is_err());
        assert!(release_source("ffmpeg", "nightly", "").is_err());
        assert!(release_source("deno", "stable", "2.9.7").is_err());
        Ok(())
    }
    #[test]
    fn managed_tool_references_cannot_escape_storage() -> Result<()> {
        let directory = tempfile::tempdir()?;
        std::fs::write(
            directory.path().join("current.json"),
            b"{\"yt-dlp\":\"../../outside\"}",
        )?;
        assert!(managed_path(directory.path(), "yt-dlp").is_err());
        Ok(())
    }
    #[test]
    fn verified_archive_requires_every_tool_and_ignores_unrelated_members() -> Result<()> {
        use std::io::Write;
        let directory = tempfile::tempdir()?;
        let mut writer = zip::ZipWriter::new(std::io::Cursor::new(Vec::new()));
        writer.start_file("bin/ffmpeg.exe", zip::write::SimpleFileOptions::default())?;
        writer.write_all(b"MZfixture")?;
        writer.start_file("readme.txt", zip::write::SimpleFileOptions::default())?;
        writer.write_all(b"notes")?;
        let archive = writer.finish()?.into_inner();
        assert!(
            extract_archive(
                &archive,
                "tool.zip",
                &["ffmpeg.exe".into(), "ffprobe.exe".into()],
                directory.path()
            )
            .is_err()
        );
        assert!(!directory.path().join("readme.txt").exists());
        Ok(())
    }
}

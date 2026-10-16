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

fn ffmpeg_dll(name: &str) -> bool {
    [
        "avcodec",
        "avdevice",
        "avfilter",
        "avformat",
        "avutil",
        "swresample",
        "swscale",
    ]
    .iter()
    .any(|prefix| {
        name.strip_prefix(&format!("{prefix}-"))
            .and_then(|s| s.strip_suffix(".dll"))
            .is_some_and(|s| !s.is_empty() && s.chars().all(|c| c.is_ascii_digit()))
    })
}

fn payload_name(name: &str) -> String {
    if ffmpeg_dll(name) {
        name.into()
    } else {
        executable(name)
    }
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
    if let Ok(path) = resolve(app, "qjs") {
        sources.insert("qjs".into(), path);
    } else if let Ok(path) = resolve(app, "deno") {
        sources.insert("deno".into(), path);
    }
    if cfg!(windows) {
        let parent = sources["ffmpeg"].parent().context("Invalid FFmpeg path")?;
        for entry in std::fs::read_dir(parent)? {
            let entry = entry?;
            let name = entry.file_name().to_string_lossy().into_owned();
            if ffmpeg_dll(&name) {
                anyhow::ensure!(
                    entry.file_type()?.is_file(),
                    "FFmpeg DLL must be a regular file"
                );
                sources.insert(name, entry.path());
            }
        }
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
            let object_dir = app
                .config
                .data_dir
                .join("tools/objects")
                .join(&hashes[name]);
            std::fs::create_dir_all(&object_dir)?;
            let object = object_dir.join(payload_name(name));
            if !object.exists() {
                let mut input = std::fs::File::open(source)?;
                let mut output = tempfile::NamedTempFile::new_in(&object_dir)?;
                std::io::copy(&mut input, &mut output)?;
                output.as_file().sync_all()?;
                #[cfg(unix)]
                std::fs::set_permissions(output.path(), input.metadata()?.permissions())?;
                anyhow::ensure!(
                    file_digest(output.path())? == hashes[name],
                    "Runtime changed while freezing {name}"
                );
                output.persist_noclobber(&object).map_err(|e| e.error)?;
            }
            anyhow::ensure!(
                file_digest(&object)? == hashes[name],
                "Cached runtime checksum mismatch: {name}"
            );
            let target = temporary.path().join(payload_name(name));
            // Link only immutable cached bytes; mutable installations never share task storage.
            if std::fs::hard_link(&object, &target).is_err() {
                std::fs::copy(&object, &target)?;
            }
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
    for name in sources.keys().filter(|name| !ffmpeg_dll(name)) {
        let path = destination.join(executable(name)).canonicalize()?;
        validate_frozen(app, &path, name)?;
        paths.insert(name.clone(), path);
    }
    Ok(paths)
}

pub(crate) fn validate_frozen(app: &App, path: &Path, name: &str) -> Result<()> {
    anyhow::ensure!(
        matches!(name, "yt-dlp" | "ffmpeg" | "ffprobe" | "deno" | "qjs"),
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
        std::fs::metadata(&manifest_path)?.len() <= 8192,
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
    if matches!(name, "ffmpeg" | "ffprobe") {
        for (filename, digest) in &hashes {
            if ffmpeg_dll(filename) {
                anyhow::ensure!(
                    file_digest(&directory.join(filename))? == *digest,
                    "Frozen FFmpeg DLL checksum mismatch: {filename}"
                );
            }
        }
        for entry in std::fs::read_dir(directory)? {
            let entry = entry?;
            let filename = entry.file_name().to_string_lossy().into_owned();
            if filename.to_ascii_lowercase().ends_with(".dll") {
                anyhow::ensure!(
                    entry.file_type()?.is_file() && hashes.contains_key(&filename),
                    "Unverified DLL in frozen runtime: {filename}"
                );
            }
        }
    }
    Ok(())
}

pub(crate) fn resolve(app: &App, name: &str) -> Result<PathBuf> {
    if !matches!(name, "yt-dlp" | "ffmpeg" | "ffprobe" | "deno" | "qjs") {
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
        ("ffmpeg" | "ffprobe", "stable") if version.is_empty() => "BtbN/FFmpeg-Builds",
        ("deno", "stable") if version.is_empty() => "denoland/deno",
        ("qjs", "stable") if version.is_empty() => "quickjs-ng/quickjs",
        ("ffmpeg" | "ffprobe" | "deno" | "qjs", _) => {
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
    let source = args
        .get("downloadSource")
        .map(|v| v.as_str().context("Runtime download source must be text"))
        .transpose()?
        .unwrap_or("auto");
    anyhow::ensure!(
        matches!(source, "auto" | "github" | "cdn"),
        "Unknown runtime download source"
    );
    anyhow::ensure!(
        name == "yt-dlp" || source != "cdn",
        "CDN downloads are only supported for yt-dlp"
    );
    let _guard = tokio::select! {
        guard=app.tool_update_lock.lock()=>guard,
        _=app.shutdown_token.cancelled()=>bail!("Runtime update interrupted"),
    };
    tokio::select! {
        result=update_inner(app,name,repository,&release_url,source)=>result,
        _=app.shutdown_token.cancelled()=>bail!("Runtime update interrupted"),
    }
}

fn runtime_client() -> Result<reqwest::Client> {
    Ok(reqwest::Client::builder()
        .user_agent(concat!("HuifaDownloader/", env!("CARGO_PKG_VERSION")))
        .https_only(true)
        .connect_timeout(std::time::Duration::from_secs(10))
        .timeout(std::time::Duration::from_secs(300))
        .build()?)
}

fn asset_name(name: &str, platform: &str) -> Result<&'static str> {
    Ok(match (name, platform) {
        ("yt-dlp", "windows-x86_64") => "yt-dlp.exe",
        ("yt-dlp", "windows-aarch64") => "yt-dlp_arm64.exe",
        ("yt-dlp", "linux-x86_64") => "yt-dlp_linux",
        ("yt-dlp", "linux-aarch64") => "yt-dlp_linux_aarch64",
        ("ffmpeg" | "ffprobe", "windows-x86_64") => "ffmpeg-master-latest-win64-gpl-shared.zip",
        ("ffmpeg" | "ffprobe", "linux-x86_64") => "ffmpeg-master-latest-linux64-gpl.tar.xz",
        ("ffmpeg" | "ffprobe", "linux-aarch64") => "ffmpeg-master-latest-linuxarm64-gpl.tar.xz",
        ("deno", "windows-x86_64") => "deno-x86_64-pc-windows-msvc.zip",
        ("deno", "linux-x86_64") => "deno-x86_64-unknown-linux-gnu.zip",
        ("deno", "linux-aarch64") => "deno-aarch64-unknown-linux-gnu.zip",
        ("qjs", "windows-x86_64") => "qjs-windows-x86_64.exe",
        ("qjs", "linux-x86_64") => "qjs-linux-x86_64",
        ("qjs", "linux-aarch64") => "qjs-linux-aarch64",
        _ => bail!("Unsupported runtime platform or architecture: {platform}"),
    })
}

fn version_numbers(version: &str) -> Option<Vec<u32>> {
    if version.len() > 64 || !version.chars().all(|c| c.is_ascii_digit() || c == '.') {
        return None;
    }
    let mut numbers = version
        .trim()
        .split('.')
        .map(str::parse)
        .collect::<std::result::Result<Vec<u32>, _>>()
        .ok()?;
    if !(3..=4).contains(&numbers.len()) {
        return None;
    }
    while numbers.last() == Some(&0) {
        numbers.pop();
    }
    Some(numbers)
}

fn release_tag(repository: &str, url: &url::Url) -> Result<String> {
    anyhow::ensure!(
        url.scheme() == "https"
            && url.host_str() == Some("github.com")
            && url.query().is_none()
            && url.fragment().is_none(),
        "Unexpected official runtime release URL"
    );
    let prefix = format!("/{repository}/releases/tag/");
    let tag = url
        .path()
        .strip_prefix(&prefix)
        .context("GitHub did not resolve the latest runtime release")?;
    anyhow::ensure!(
        tag.len() <= 64 && version_numbers(tag).is_some(),
        "Invalid official yt-dlp release version"
    );
    Ok(tag.to_string())
}

async fn latest_version(client: &reqwest::Client, repository: &str) -> Result<String> {
    // GitHub's release redirect reports the official tag without using the API quota.
    let response = client
        .head(format!("https://github.com/{repository}/releases/latest"))
        .timeout(std::time::Duration::from_secs(20))
        .send()
        .await
        .map_err(|e| e.without_url())
        .context("Cannot check latest yt-dlp release from GitHub")?;
    anyhow::ensure!(
        response.status().is_success(),
        "Cannot check latest yt-dlp release from GitHub: HTTP {}",
        response.status().as_u16()
    );
    release_tag(repository, response.url())
}

pub(crate) async fn latest(app: &App, args: &Value) -> Result<Value> {
    let name = args["name"]
        .as_str()
        .context("Runtime tool name must be text")?;
    anyhow::ensure!(
        name == "yt-dlp",
        "Remote version checks are only supported for yt-dlp"
    );
    let channel = args
        .get("channel")
        .map(|v| v.as_str().context("Runtime channel must be text"))
        .transpose()?
        .unwrap_or("stable");
    let (repository, _) = release_source(name, channel, "")?;
    let platform = target()?;
    let asset = asset_name(name, &platform)?;
    let client = runtime_client()?;
    let version = tokio::select! {
        result=latest_version(&client,repository)=>result?,
        _=app.shutdown_token.cancelled()=>bail!("Runtime update check interrupted"),
    };
    let current = if let Ok(path) = resolve(app, name) {
        crate::process::run(
            &path,
            &["--version".into()],
            None,
            &app.shutdown_token,
            None,
        )
        .await
        .ok()
        .map(|v| v.stdout.trim().to_string())
    } else {
        None
    };
    let update_available = current
        .as_deref()
        .and_then(version_numbers)
        .zip(version_numbers(&version))
        .is_none_or(|(current, latest)| current < latest);
    Ok(
        json!({"name":name,"channel":if channel.is_empty() {"stable"} else {channel},"current":current,"latest":version,"updateAvailable":update_available,"platform":platform,"asset":asset}),
    )
}

fn download_urls(official: &str, source: &str) -> Vec<String> {
    let cdn = format!("https://ghfast.top/{official}");
    match source {
        "cdn" => vec![cdn],
        "auto" => vec![official.to_string(), cdn],
        _ => vec![official.to_string()],
    }
}

async fn verified_download(
    client: &reqwest::Client,
    urls: &[String],
    digest: &str,
) -> Result<Vec<u8>> {
    let mut failures = Vec::new();
    for (index, url) in urls.iter().enumerate() {
        let result = async {
            let request = client.get(url);
            let request = if index == 0 && urls.len() > 1 {
                request.timeout(std::time::Duration::from_secs(30))
            } else {
                request
            };
            let response = request.send().await.map_err(|e| e.without_url())?;
            anyhow::ensure!(
                response.status().is_success(),
                "HTTP {}",
                response.status().as_u16()
            );
            let bytes = bounded_response(response, 512 * 1024 * 1024).await?;
            anyhow::ensure!(
                hex::encode(Sha256::digest(&bytes)) == digest.to_ascii_lowercase(),
                "Runtime archive checksum mismatch"
            );
            Ok::<_, anyhow::Error>(bytes)
        }
        .await;
        match result {
            Ok(bytes) => return Ok(bytes),
            Err(error) => failures.push(format!(
                "{}: {error:#}",
                url::Url::parse(url)?.host_str().unwrap_or("download")
            )),
        }
    }
    bail!("Runtime download failed: {}", failures.join("; "))
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

async fn update_inner(
    app: &App,
    name: &str,
    repository: &str,
    release_url: &str,
    source: &str,
) -> Result<Value> {
    use anyhow::{Context, ensure};
    use std::io::Write;
    let platform = target()?;
    let asset_name = asset_name(name, &platform)?;
    let client = runtime_client()?;
    let release: Value = if name == "yt-dlp" {
        // Official release checksum assets avoid the unauthenticated GitHub API quota.
        let base = if release_url.ends_with("/latest/download") {
            format!(
                "https://github.com/{repository}/releases/download/{}",
                latest_version(&client, repository).await?
            )
        } else {
            release_url.to_string()
        };
        let response = client
            .get(format!("{base}/SHA2-256SUMS"))
            .timeout(std::time::Duration::from_secs(20))
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
    let urls = download_urls(
        url.as_str(),
        if name == "yt-dlp" { source } else { "github" },
    );
    let archive = verified_download(&client, &urls, digest).await?;
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
    let extracted = tokio::task::spawn_blocking(move || {
        extract_archive(&archive, &format, &extracted_names, &extraction_dir)
    })
    .await??;
    for filename in extracted.iter().filter(|filename| ffmpeg_dll(filename)) {
        ensure!(
            std::fs::read(temporary.path().join(filename))?.starts_with(b"MZ"),
            "Runtime DLL is not a native library"
        );
    }
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
        for filename in &extracted {
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
) -> Result<Vec<String>> {
    use std::io::{Read, Write};
    let mut found = std::collections::HashSet::new();
    let mut save = |path: &Path, reader: &mut dyn Read, size: u64| -> Result<()> {
        anyhow::ensure!(
            path.components()
                .all(|part| matches!(part, std::path::Component::Normal(_))),
            "Unsafe runtime archive path"
        );
        let filename = path.file_name().and_then(|v| v.to_str()).unwrap_or("");
        if !wanted.iter().any(|wanted| wanted == filename)
            && !(wanted
                .iter()
                .any(|name| name == "ffmpeg.exe" || name == "ffprobe.exe")
                && ffmpeg_dll(filename))
        {
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
        wanted.iter().all(|name| found.contains(name)),
        "Runtime archive does not contain every required binary"
    );
    if name.ends_with("-gpl-shared.zip") {
        for prefix in [
            "avcodec",
            "avdevice",
            "avfilter",
            "avformat",
            "avutil",
            "swresample",
            "swscale",
        ] {
            anyhow::ensure!(
                found
                    .iter()
                    .filter(|filename| filename.starts_with(&format!("{prefix}-"))
                        && ffmpeg_dll(filename))
                    .count()
                    == 1,
                "Runtime archive is missing a unique {prefix} DLL"
            );
        }
    }
    Ok(found.into_iter().collect())
}

pub(crate) async fn check(app: &App) -> Result<Value> {
    let mut tools = Vec::new();
    let javascript = if resolve(app, "qjs").is_ok() || resolve(app, "deno").is_err() {
        "qjs"
    } else {
        "deno"
    };
    for name in ["yt-dlp", "ffmpeg", "ffprobe", javascript] {
        let mut tool = match resolve(app, name) {
            Ok(path) => {
                let flag = if name == "ffmpeg" || name == "ffprobe" {
                    "-version"
                } else {
                    "--version"
                };
                let result =
                    crate::process::run(&path, &[flag.into()], None, &app.shutdown_token, None)
                        .await;
                match result {
                    Ok(output) => {
                        json!({"name":name,"available":true,"version":output.stdout.lines().next().unwrap_or("")})
                    }
                    Err(error) => {
                        json!({"name":name,"available":false,"error":crate::process::redact(&format!("{error:#}"))})
                    }
                }
            }
            Err(error) => {
                json!({"name":name,"available":false,"error":crate::process::redact(&format!("{error:#}"))})
            }
        };
        if name == "yt-dlp"
            && let Ok(platform) = target()
            && let Ok(asset) = asset_name(name, &platform)
        {
            tool["platform"] = json!(platform);
            tool["asset"] = json!(asset);
        }
        tools.push(tool);
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
    async fn component_cache_reuses_unchanged_bytes_and_freezes_dlls() -> Result<()> {
        let root = tempfile::tempdir()?;
        let bundled = root.path().join("bundled");
        std::fs::create_dir_all(&bundled)?;
        for name in ["yt-dlp", "ffmpeg", "ffprobe", "qjs"] {
            std::fs::write(bundled.join(executable(name)), format!("{name}:v1"))?;
        }
        if cfg!(windows) {
            std::fs::write(bundled.join("avcodec-63.dll"), b"MZoriginal DLL")?;
        }
        let app = App::open(crate::AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("downloads"),
            tool_dir: Some(bundled.clone()),
        })
        .await?;
        let old = freeze(&app)?;
        std::fs::write(bundled.join(executable("yt-dlp")), b"yt-dlp:v2")?;
        let new = freeze(&app)?;
        assert_ne!(old["yt-dlp"], new["yt-dlp"]);
        assert_eq!(
            std::fs::read_dir(app.config.data_dir.join("tools/objects"))?.count(),
            if cfg!(windows) { 6 } else { 5 }
        );
        for name in ["ffmpeg", "ffprobe", "qjs"] {
            validate_frozen(&app, &old[name], name)?;
            validate_frozen(&app, &new[name], name)?;
        }
        let before = std::fs::read(&old["ffmpeg"])?;
        std::fs::write(bundled.join(executable("ffmpeg")), b"mutable update")?;
        assert_eq!(std::fs::read(&old["ffmpeg"])?, before);
        if cfg!(windows) {
            let dll = new["ffmpeg"].parent().unwrap().join("avcodec-63.dll");
            std::fs::write(&dll, b"MZtampered DLL")?;
            assert!(validate_frozen(&app, &new["ffmpeg"], "ffmpeg").is_err());
            assert!(validate_frozen(&app, &new["ffprobe"], "ffprobe").is_err());
        }
        app.shutdown().await?;
        Ok(())
    }
    #[test]
    fn packages_match_the_runtime_system_and_architecture() -> Result<()> {
        for (platform, asset) in [
            ("windows-x86_64", "yt-dlp.exe"),
            ("windows-aarch64", "yt-dlp_arm64.exe"),
            ("linux-x86_64", "yt-dlp_linux"),
            ("linux-aarch64", "yt-dlp_linux_aarch64"),
        ] {
            assert_eq!(asset_name("yt-dlp", platform)?, asset);
        }
        assert!(asset_name("yt-dlp", "unknown-x86_64").is_err());
        assert!(asset_name("yt-dlp", "linux-riscv64").is_err());
        assert!(asset_name("ffmpeg", "windows-aarch64").is_err());
        Ok(())
    }

    #[test]
    fn latest_version_requires_an_exact_official_release_redirect() -> Result<()> {
        assert_eq!(
            release_tag(
                "yt-dlp/yt-dlp",
                &url::Url::parse("https://github.com/yt-dlp/yt-dlp/releases/tag/2026.08.19")?
            )?,
            "2026.08.19"
        );
        for value in [
            "https://github.com.evil.test/yt-dlp/yt-dlp/releases/tag/2026.08.19",
            "http://github.com/yt-dlp/yt-dlp/releases/tag/2026.08.19",
            "https://github.com/other/project/releases/tag/2026.08.19",
            "https://github.com/yt-dlp/yt-dlp/releases/latest",
            "https://github.com/yt-dlp/yt-dlp/releases/tag/2026.08.19?tag=other",
            "https://github.com/yt-dlp/yt-dlp/releases/tag/%2f2026.08.19",
            "https://github.com/yt-dlp/yt-dlp/releases/tag/not-a-version",
        ] {
            assert!(
                release_tag("yt-dlp/yt-dlp", &url::Url::parse(value)?).is_err(),
                "{value}"
            );
        }
        Ok(())
    }

    #[test]
    fn version_comparison_handles_stable_nightly_and_padding() {
        assert!(version_numbers("2026.08.19") < version_numbers("2026.10.10.233141"));
        assert!(version_numbers("2026.08.19") < version_numbers("2026.08.19.233141"));
        assert_eq!(
            version_numbers("2026.08.19.0"),
            version_numbers("2026.8.19")
        );
        for invalid in [
            "",
            "unknown",
            "2026.08",
            "2026.8.19.1.2",
            "+2026.8.19",
            "2026.8.19-other",
        ] {
            assert!(version_numbers(invalid).is_none());
        }
    }

    #[test]
    fn cdn_downloads_use_the_fixed_official_version_url() {
        let url = "https://github.com/yt-dlp/yt-dlp/releases/download/2026.08.19/yt-dlp.exe";
        assert_eq!(download_urls(url, "github"), vec![url]);
        assert_eq!(
            download_urls(url, "cdn"),
            vec![format!("https://ghfast.top/{url}")]
        );
        assert_eq!(
            download_urls(url, "auto"),
            vec![url.to_string(), format!("https://ghfast.top/{url}")]
        );
    }

    async fn download_fixture(
        status: u16,
        body: &'static [u8],
    ) -> Result<(String, tokio::task::JoinHandle<Result<()>>)> {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
        let address = listener.local_addr()?;
        let handle = tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await?;
            let mut request = Vec::new();
            let mut buffer = [0u8; 1024];
            while !request.windows(4).any(|v| v == b"\r\n\r\n") {
                let count = stream.read(&mut buffer).await?;
                anyhow::ensure!(
                    count > 0 && request.len() + count <= 8192,
                    "Invalid fixture request"
                );
                request.extend_from_slice(&buffer[..count]);
            }
            stream.write_all(format!("HTTP/1.1 {status} Fixture\r\nContent-Length: {}\r\nConnection: close\r\n\r\n", body.len()).as_bytes()).await?;
            stream.write_all(body).await?;
            Ok(())
        });
        Ok((format!("http://{address}/yt-dlp.exe"), handle))
    }

    #[tokio::test]
    async fn rate_limited_or_corrupt_downloads_fall_back_but_keep_the_official_checksum()
    -> Result<()> {
        let client = reqwest::Client::builder()
            .no_proxy()
            .timeout(std::time::Duration::from_secs(3))
            .build()?;
        let expected = b"MZverified fixture";
        let digest = hex::encode(Sha256::digest(expected));
        for (status, bad) in [
            (403, b"rate limited".as_slice()),
            (429, b"try later".as_slice()),
            (200, b"MZwrong version".as_slice()),
        ] {
            let (direct, first) = download_fixture(status, bad).await?;
            let (cdn, second) = download_fixture(200, expected).await?;
            assert_eq!(
                verified_download(&client, &[direct, cdn], &digest).await?,
                expected
            );
            first.await??;
            second.await??;
        }
        let (cdn, fixture) = download_fixture(200, b"MZuntrusted replacement").await?;
        assert!(
            verified_download(&client, &[cdn], &digest)
                .await
                .unwrap_err()
                .to_string()
                .contains("checksum mismatch")
        );
        fixture.await??;
        Ok(())
    }

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
            json!({"name":"yt-dlp","downloadSource":"https://untrusted.test/"}),
            json!({"name":"yt-dlp","downloadSource":1}),
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
    #[test]
    fn verified_shared_archive_keeps_libraries_and_rejects_duplicate_payloads() -> Result<()> {
        let root = tempfile::tempdir()?;
        let archive = |duplicate: bool, missing: bool| -> Result<Vec<u8>> {
            let mut writer = zip::ZipWriter::new(std::io::Cursor::new(Vec::new()));
            for name in [
                "ffmpeg.exe",
                "ffprobe.exe",
                "avcodec-63.dll",
                "avutil-61.dll",
                "avdevice-63.dll",
                "avfilter-12.dll",
                "avformat-63.dll",
                "swresample-7.dll",
                "swscale-10.dll",
                "unrelated.dll",
            ] {
                if missing && name == "swscale-10.dll" {
                    continue;
                }
                writer.start_file(
                    format!("bin/{name}"),
                    zip::write::SimpleFileOptions::default(),
                )?;
                writer.write_all(b"MZfixture")?;
            }
            if duplicate {
                writer.start_file(
                    "other/avcodec-63.dll",
                    zip::write::SimpleFileOptions::default(),
                )?;
                writer.write_all(b"MZduplicate")?;
            }
            Ok(writer.finish()?.into_inner())
        };
        let wanted = ["ffmpeg.exe".into(), "ffprobe.exe".into()];
        let files = extract_archive(
            &archive(false, false)?,
            "ffmpeg-gpl-shared.zip",
            &wanted,
            root.path(),
        )?;
        assert_eq!(files.len(), 9);
        assert!(!root.path().join("unrelated.dll").exists());
        let other = tempfile::tempdir()?;
        assert!(
            extract_archive(
                &archive(true, false)?,
                "ffmpeg-gpl-shared.zip",
                &wanted,
                other.path()
            )
            .is_err()
        );
        let missing = tempfile::tempdir()?;
        assert!(
            extract_archive(
                &archive(false, true)?,
                "ffmpeg-gpl-shared.zip",
                &wanted,
                missing.path()
            )
            .is_err()
        );
        Ok(())
    }
}

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

fn native_executable(bytes: &[u8], platform: &str) -> bool {
    fn word(bytes: &[u8], offset: usize, little: bool) -> Option<u32> {
        let value: [u8; 4] = bytes.get(offset..offset.checked_add(4)?)?.try_into().ok()?;
        Some(if little {
            u32::from_le_bytes(value)
        } else {
            u32::from_be_bytes(value)
        })
    }
    fn wide(bytes: &[u8], offset: usize, little: bool) -> Option<usize> {
        let value: [u8; 8] = bytes.get(offset..offset.checked_add(8)?)?.try_into().ok()?;
        usize::try_from(if little {
            u64::from_le_bytes(value)
        } else {
            u64::from_be_bytes(value)
        })
        .ok()
    }
    fn thin(bytes: &[u8], cpu: u32) -> bool {
        bytes.len() >= 32
            && match word(bytes, 0, false) {
                Some(0xcffaedfe) => word(bytes, 4, true) == Some(cpu),
                Some(0xfeedfacf) => word(bytes, 4, false) == Some(cpu),
                _ => false,
            }
    }
    let arm = platform.ends_with("-aarch64");
    if platform.starts_with("windows-") {
        if !bytes.starts_with(b"MZ") {
            return false;
        }
        let Some(offset) = word(bytes, 60, true).map(|value| value as usize) else {
            return false;
        };
        return word(bytes, offset, true) == Some(0x4550)
            && bytes
                .get(offset + 4..offset + 6)
                .is_some_and(|value| value == if arm { [0x64, 0xaa] } else { [0x64, 0x86] });
    }
    if platform.starts_with("linux-") {
        return bytes.starts_with(b"\x7fELF\x02\x01")
            && bytes
                .get(18..20)
                .is_some_and(|value| value == if arm { [183, 0] } else { [62, 0] });
    }
    if !platform.starts_with("darwin-") {
        return false;
    }
    let cpu = if arm { 0x0100000c } else { 0x01000007 };
    if thin(bytes, cpu) {
        return true;
    }
    let Some(magic) = word(bytes, 0, false) else {
        return false;
    };
    let fat64 = matches!(magic, 0xcafebabf | 0xbfbafeca);
    let little = matches!(magic, 0xbebafeca | 0xbfbafeca);
    if !matches!(magic, 0xcafebabe | 0xbebafeca | 0xcafebabf | 0xbfbafeca) {
        return false;
    }
    let Some(count) = word(bytes, 4, little).filter(|value| (1..=64).contains(value)) else {
        return false;
    };
    let stride = if fat64 { 32 } else { 20 };
    let table_end = 8 + count as usize * stride;
    if table_end > bytes.len() {
        return false;
    }
    for index in 0..count as usize {
        let entry = 8 + index * stride;
        if word(bytes, entry, little) != Some(cpu) {
            continue;
        }
        let offset = if fat64 {
            wide(bytes, entry + 8, little)
        } else {
            word(bytes, entry + 8, little).map(|v| v as usize)
        };
        let size = if fat64 {
            wide(bytes, entry + 16, little)
        } else {
            word(bytes, entry + 12, little).map(|v| v as usize)
        };
        return offset.zip(size).is_some_and(|(offset, size)| {
            offset >= table_end
                && size >= 32
                && offset
                    .checked_add(size)
                    .and_then(|end| bytes.get(offset..end))
                    .is_some_and(|slice| thin(slice, cpu))
        });
    }
    false
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

pub(crate) fn unused_cache(app: &App) -> Result<Vec<PathBuf>> {
    let root = app.config.data_dir.join("tools");
    if !root.exists() {
        return Ok(vec![]);
    }
    crate::reject_links(&root)?;
    let mut kept = std::collections::HashSet::new();
    for task in app.db.tasks_including_retained()? {
        kept.extend(
            task.tools
                .values()
                .filter_map(|path| path.parent().map(Path::to_path_buf)),
        );
    }
    let pointer = root.join("current.json");
    if pointer.exists() {
        crate::reject_links(&pointer)?;
        let current: HashMap<String, String> = serde_json::from_slice(&std::fs::read(pointer)?)?;
        for value in current.values() {
            let path = root.join(value);
            crate::reject_links(&path)?;
            if let Some(parent) = path.parent() {
                kept.insert(parent.canonicalize()?);
            }
        }
    }
    let mut removable = Vec::new();
    let mut objects = std::collections::HashSet::new();
    let frozen = root.join("frozen");
    if frozen.exists() {
        for entry in std::fs::read_dir(&frozen)? {
            let entry = entry?;
            let path = entry.path();
            let name = entry.file_name().to_string_lossy().into_owned();
            if !entry.file_type()?.is_dir()
                || name.len() != 64
                || !name.chars().all(|c| c.is_ascii_hexdigit())
            {
                continue;
            }
            crate::reject_links(&path)?;
            if kept.contains(&path.canonicalize()?) {
                let manifest: BTreeMap<String, String> =
                    serde_json::from_slice(&std::fs::read(path.join("manifest.json"))?)?;
                objects.extend(manifest.into_values());
            } else {
                removable.push(path);
            }
        }
    }
    for entry in std::fs::read_dir(&root)? {
        let entry = entry?;
        let name = entry.file_name().to_string_lossy().into_owned();
        let version = ["yt-dlp-", "ffmpeg-", "deno-", "qjs-"]
            .iter()
            .find_map(|prefix| name.strip_prefix(prefix));
        if entry.file_type()?.is_dir()
            && version.is_some_and(|value| {
                value.len() == 16 && value.chars().all(|c| c.is_ascii_hexdigit())
            })
            && !kept.contains(&entry.path().canonicalize()?)
        {
            removable.push(entry.path());
        }
    }
    let object_dir = root.join("objects");
    if object_dir.exists() {
        for entry in std::fs::read_dir(object_dir)? {
            let entry = entry?;
            let name = entry.file_name().to_string_lossy().into_owned();
            if entry.file_type()?.is_dir()
                && name.len() == 64
                && name.chars().all(|c| c.is_ascii_hexdigit())
                && !objects.contains(&name)
            {
                removable.push(entry.path());
            }
        }
    }
    Ok(removable)
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
    } else if let Ok(path) = resolve(app, "qjs") {
        sources.insert("qjs".into(), path);
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
                    file_digest(&directory.join(filename)).with_context(|| format!("Frozen FFmpeg DLL is missing or unreadable: {filename}; restore its saved tool version before retrying"))? == *digest,
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
        ("ffmpeg" | "ffprobe", "stable") if version.is_empty() => {
            if cfg!(target_os = "macos") {
                "shaka-project/static-ffmpeg-binaries"
            } else if cfg!(all(windows, target_arch = "aarch64")) {
                "tordona/ffmpeg-win-arm64"
            } else {
                "BtbN/FFmpeg-Builds"
            }
        }
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
    } else if repository == "tordona/ffmpeg-win-arm64" {
        // shortcut: latest includes mutable nightlies; bump this stable tag with the next ARM64 tool update.
        ("https://api.github.com/repos", "tags/9.0.2".into())
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

fn runtime_client(proxy: &str) -> Result<reqwest::Client> {
    let mut builder = reqwest::Client::builder()
        .no_proxy()
        .user_agent(concat!("HuifaDownloader/", env!("CARGO_PKG_VERSION")))
        .https_only(true)
        .connect_timeout(std::time::Duration::from_secs(10))
        .timeout(std::time::Duration::from_secs(300));
    if !proxy.is_empty() {
        builder = builder.proxy(reqwest::Proxy::all(proxy)?);
    }
    Ok(builder.build()?)
}

fn asset_name(name: &str, platform: &str) -> Result<&'static str> {
    Ok(match (name, platform) {
        ("yt-dlp", "windows-x86_64") => "yt-dlp.exe",
        ("yt-dlp", "windows-aarch64") => "yt-dlp_arm64.exe",
        ("yt-dlp", "linux-x86_64") => "yt-dlp_linux",
        ("yt-dlp", "linux-aarch64") => "yt-dlp_linux_aarch64",
        ("yt-dlp", "darwin-x86_64" | "darwin-aarch64") => "yt-dlp_macos",
        ("ffmpeg" | "ffprobe", "windows-x86_64") => "ffmpeg-master-latest-win64-gpl-shared.zip",
        ("ffmpeg" | "ffprobe", "windows-aarch64") => "ffmpeg-9.0.2-essentials-static-win-arm64.7z",
        ("ffmpeg" | "ffprobe", "linux-x86_64") => "ffmpeg-master-latest-linux64-gpl.tar.xz",
        ("ffmpeg" | "ffprobe", "linux-aarch64") => "ffmpeg-master-latest-linuxarm64-gpl.tar.xz",
        ("ffmpeg", "darwin-x86_64") => "ffmpeg-osx-x64",
        ("ffmpeg", "darwin-aarch64") => "ffmpeg-osx-arm64",
        ("ffprobe", "darwin-x86_64") => "ffprobe-osx-x64",
        ("ffprobe", "darwin-aarch64") => "ffprobe-osx-arm64",
        ("deno", "windows-x86_64") => "deno-x86_64-pc-windows-msvc.zip",
        ("deno", "windows-aarch64") => "deno-aarch64-pc-windows-msvc.zip",
        ("deno", "linux-x86_64") => "deno-x86_64-unknown-linux-gnu.zip",
        ("deno", "linux-aarch64") => "deno-aarch64-unknown-linux-gnu.zip",
        ("deno", "darwin-x86_64") => "deno-x86_64-apple-darwin.zip",
        ("deno", "darwin-aarch64") => "deno-aarch64-apple-darwin.zip",
        ("qjs", "windows-x86_64") => "qjs-windows-x86_64.exe",
        ("qjs", "linux-x86_64") => "qjs-linux-x86_64",
        ("qjs", "linux-aarch64") => "qjs-linux-aarch64",
        ("qjs", "darwin-x86_64") => "qjs-darwin-x86_64",
        ("qjs", "darwin-aarch64") => "qjs-darwin-arm64",
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
    let client = runtime_client(&app.download_proxy()?)?;
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

fn release_asset(
    release: &Value,
    repository: &str,
    filename: &str,
    latest_download: bool,
) -> Result<(url::Url, String)> {
    let asset = release["assets"]
        .as_array()
        .context("Malformed runtime release")?
        .iter()
        .find(|asset| asset["name"].as_str() == Some(filename))
        .context("Runtime asset is unavailable for this platform")?;
    let digest = asset["digest"]
        .as_str()
        .and_then(|value| value.strip_prefix("sha256:"))
        .context("Runtime release lacks a trusted checksum; installation refused")?;
    anyhow::ensure!(
        digest.len() == 64 && digest.chars().all(|value| value.is_ascii_hexdigit()),
        "Invalid runtime checksum"
    );
    let url = url::Url::parse(
        asset["browser_download_url"]
            .as_str()
            .context("Runtime download URL is missing")?,
    )?;
    anyhow::ensure!(
        url.scheme() == "https"
            && url.host_str() == Some("github.com")
            && (url
                .path()
                .starts_with(&format!("/{repository}/releases/download/"))
                || latest_download
                    && url.path() == format!("/{repository}/releases/latest/download/{filename}")),
        "Unexpected runtime download origin"
    );
    Ok((url, digest.to_string()))
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
    let initial_asset_name = asset_name(name, &platform)?;
    let client = runtime_client(&app.download_proxy()?)?;
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
        let digest = checksum_for(&sums, initial_asset_name)?;
        json!({"assets":[{"name":initial_asset_name,"digest":format!("sha256:{digest}"),"browser_download_url":format!("{base}/{initial_asset_name}")}]})
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
    let split_ffmpeg = platform.starts_with("darwin-") && matches!(name, "ffmpeg" | "ffprobe");
    let asset_names = if split_ffmpeg {
        vec![
            asset_name("ffmpeg", &platform)?,
            asset_name("ffprobe", &platform)?,
        ]
    } else {
        vec![initial_asset_name]
    };
    let mut downloads = Vec::new();
    let mut digests = Vec::new();
    for filename in asset_names {
        let (url, digest) = release_asset(&release, repository, filename, name == "yt-dlp")?;
        let urls = download_urls(
            url.as_str(),
            if name == "yt-dlp" { source } else { "github" },
        );
        let archive = verified_download(&client, &urls, &digest).await?;
        downloads.push((archive, filename.to_string()));
        digests.push(digest);
    }
    let digest = if digests.len() == 1 {
        digests.remove(0)
    } else {
        hex::encode(Sha256::digest(digests.join("\n")))
    };
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
    let extracted_names = filenames.clone();
    let extracted = tokio::task::spawn_blocking(move || {
        let mut extracted = Vec::new();
        for (index, (archive, format)) in downloads.into_iter().enumerate() {
            let wanted = if split_ffmpeg {
                &extracted_names[index..index + 1]
            } else {
                &extracted_names[..]
            };
            extracted.extend(extract_archive(&archive, &format, wanted, &extraction_dir)?);
        }
        Ok::<_, anyhow::Error>(extracted)
    })
    .await??;
    for filename in extracted.iter().filter(|filename| ffmpeg_dll(filename)) {
        ensure!(
            native_executable(&std::fs::read(temporary.path().join(filename))?, &platform),
            "Runtime DLL is not a native library"
        );
    }
    for (tool, filename) in wanted.iter().zip(&filenames) {
        let path = temporary.path().join(filename);
        let header = std::fs::read(&path)?;
        ensure!(
            native_executable(&header, &platform),
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
        if tool == "ffmpeg" {
            let encoders = crate::process::run(
                &path,
                &["-hide_banner".into(), "-encoders".into()],
                None,
                &app.shutdown_token,
                None,
            )
            .await?;
            ensure!(
                ["libx264", "libvpx-vp9", "libopus"]
                    .iter()
                    .all(|codec| encoders
                        .stdout
                        .lines()
                        .any(|line| line.split_whitespace().nth(1) == Some(codec))),
                "FFmpeg is missing a required H.264, VP9 or Opus encoder"
            );
        }
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
    } else if cfg!(target_os = "macos") {
        "darwin"
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
    if name.ends_with(".7z") {
        return extract_system_archive(archive, wanted, destination);
    }
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

#[cfg(not(windows))]
fn extract_system_archive(_: &[u8], _: &[String], _: &Path) -> Result<Vec<String>> {
    bail!("This runtime archive requires Windows system tar")
}

#[cfg(windows)]
fn extract_system_archive(
    archive: &[u8],
    wanted: &[String],
    destination: &Path,
) -> Result<Vec<String>> {
    use std::{
        os::windows::process::CommandExt,
        process::{Command, Stdio},
        time::{Duration, Instant},
    };
    fn output(tar: &Path, archive: &Path, member: Option<&str>, limit: u64) -> Result<Vec<u8>> {
        let file = tempfile::NamedTempFile::new()?;
        let mut command = Command::new(tar);
        command
            .arg(if member.is_some() { "-xOf" } else { "-tf" })
            .arg(archive);
        if let Some(member) = member {
            command.args(["--", member]);
        }
        let mut child = command
            .stdin(Stdio::null())
            .stdout(file.reopen()?)
            .stderr(Stdio::null())
            .creation_flags(0x08000000)
            .spawn()
            .context("Cannot start Windows system tar")?;
        let started = Instant::now();
        loop {
            if file.as_file().metadata()?.len() > limit
                || started.elapsed() > Duration::from_secs(60)
            {
                let _ = child.kill();
                let _ = child.wait();
                bail!("Runtime archive extraction exceeded its size or time limit");
            }
            if let Some(status) = child.try_wait()? {
                anyhow::ensure!(
                    status.success(),
                    "Windows system tar could not read the verified runtime archive"
                );
                let size = file.as_file().metadata()?.len();
                anyhow::ensure!(size <= limit, "Runtime archive output is too large");
                return Ok(std::fs::read(file.path())?);
            }
            std::thread::sleep(Duration::from_millis(20));
        }
    }
    let tar = PathBuf::from(
        std::env::var_os("SystemRoot").context("Windows system directory is unavailable")?,
    )
    .join("System32/tar.exe");
    let mut file = tempfile::Builder::new().suffix(".7z").tempfile()?;
    file.write_all(archive)?;
    file.flush()?;
    let listed = String::from_utf8(output(&tar, file.path(), None, 4 * 1024 * 1024)?)
        .context("Invalid runtime archive listing")?;
    let members = listed.lines().collect::<Vec<_>>();
    let mut found = Vec::new();
    for filename in wanted {
        anyhow::ensure!(
            Path::new(filename).components().count() == 1
                && matches!(filename.as_str(), "ffmpeg.exe" | "ffprobe.exe"),
            "Invalid runtime archive payload name"
        );
        let matches = members
            .iter()
            .filter(|member| {
                let path = Path::new(member);
                path.file_name().and_then(|name| name.to_str()) == Some(filename.as_str())
            })
            .collect::<Vec<_>>();
        anyhow::ensure!(
            matches.len() == 1,
            "Runtime archive must contain one {filename}"
        );
        let member = matches[0];
        anyhow::ensure!(
            !member.contains('\\')
                && member.split('/').all(|part| !part.is_empty()
                    && part != "."
                    && part != ".."
                    && !part.contains(':')),
            "Unsafe runtime archive path"
        );
        let bytes = output(&tar, file.path(), Some(member), 256 * 1024 * 1024)?;
        let mut saved = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(destination.join(filename))?;
        saved.write_all(&bytes)?;
        saved.flush()?;
        found.push(filename.clone());
    }
    Ok(found)
}

pub(crate) async fn check(app: &App) -> Result<Value> {
    let mut tools = Vec::new();
    let javascript = if resolve(app, "deno").is_ok() {
        "deno"
    } else {
        "qjs"
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

    #[test]
    fn native_payload_matches_the_target_cpu() {
        let mut pe = vec![0; 128];
        pe[..2].copy_from_slice(b"MZ");
        pe[60..64].copy_from_slice(&64u32.to_le_bytes());
        pe[64..68].copy_from_slice(b"PE\0\0");
        pe[68..70].copy_from_slice(&0xaa64u16.to_le_bytes());
        assert!(native_executable(&pe, "windows-aarch64"));
        assert!(!native_executable(&pe, "windows-x86_64"));
        pe[60..64].copy_from_slice(&u32::MAX.to_le_bytes());
        assert!(!native_executable(&pe, "windows-aarch64"));
        let mut elf = vec![0; 32];
        elf[..6].copy_from_slice(b"\x7fELF\x02\x01");
        elf[18..20].copy_from_slice(&183u16.to_le_bytes());
        assert!(native_executable(&elf, "linux-aarch64"));
        assert!(!native_executable(&elf, "linux-x86_64"));
        let mut macho = vec![0; 32];
        macho[..4].copy_from_slice(&0xfeedfacfu32.to_le_bytes());
        macho[4..8].copy_from_slice(&0x0100000cu32.to_le_bytes());
        assert!(native_executable(&macho, "darwin-aarch64"));
        assert!(!native_executable(&macho, "darwin-x86_64"));
        let mut fat = vec![0; 60];
        fat[..4].copy_from_slice(&0xcafebabeu32.to_be_bytes());
        fat[4..8].copy_from_slice(&1u32.to_be_bytes());
        fat[8..12].copy_from_slice(&0x0100000cu32.to_be_bytes());
        fat[16..20].copy_from_slice(&28u32.to_be_bytes());
        fat[20..24].copy_from_slice(&32u32.to_be_bytes());
        fat[28..].copy_from_slice(&macho);
        assert!(native_executable(&fat, "darwin-aarch64"));
        assert!(!native_executable(&fat, "darwin-x86_64"));
        fat[20..24].copy_from_slice(&u32::MAX.to_be_bytes());
        assert!(!native_executable(&fat, "darwin-aarch64"));
    }

    #[cfg(windows)]
    #[test]
    fn system_tar_extracts_only_exact_runtime_members_and_rejects_duplicates() -> Result<()> {
        let fixture = |duplicate: bool| -> Result<Vec<u8>> {
            let mut writer = zip::ZipWriter::new(std::io::Cursor::new(Vec::new()));
            for filename in ["bin/ffmpeg.exe", "bin/ffprobe.exe", "docs/readme.txt"] {
                writer.start_file(filename, zip::write::SimpleFileOptions::default())?;
                writer.write_all(filename.as_bytes())?;
            }
            if duplicate {
                writer.start_file("other/ffmpeg.exe", zip::write::SimpleFileOptions::default())?;
                writer.write_all(b"duplicate")?;
            }
            Ok(writer.finish()?.into_inner())
        };
        let directory = tempfile::tempdir()?;
        let wanted = vec!["ffmpeg.exe".into(), "ffprobe.exe".into()];
        // Windows tar detects the archive format; this local ZIP fixture exercises the same
        // bounded member-to-stdout path used for the verified upstream 7z asset.
        assert_eq!(
            extract_archive(&fixture(false)?, "runtime.7z", &wanted, directory.path())?,
            wanted
        );
        assert_eq!(
            std::fs::read(directory.path().join("ffmpeg.exe"))?,
            b"bin/ffmpeg.exe"
        );
        assert!(!directory.path().join("docs").exists());
        let duplicate = tempfile::tempdir()?;
        assert!(extract_archive(&fixture(true)?, "runtime.7z", &wanted, duplicate.path()).is_err());
        assert_eq!(std::fs::read_dir(duplicate.path())?.count(), 0);
        Ok(())
    }

    #[test]
    fn runtime_assets_reject_unexpected_origins() -> Result<()> {
        let release = |url: &str| json!({"assets":[{"name":"ffmpeg-osx-arm64","digest":format!("sha256:{}", "f".repeat(64)),"browser_download_url":url}]});
        let repository = "shaka-project/static-ffmpeg-binaries";
        assert!(release_asset(&release("https://github.com/shaka-project/static-ffmpeg-binaries/releases/download/n8.1.2-1/ffmpeg-osx-arm64"), repository, "ffmpeg-osx-arm64", false).is_ok());
        for url in [
            "https://evil.example/ffmpeg-osx-arm64",
            "https://github.com/untrusted/ffmpeg/releases/download/1/ffmpeg-osx-arm64",
            "http://github.com/shaka-project/static-ffmpeg-binaries/releases/download/1/ffmpeg-osx-arm64",
        ] {
            assert!(release_asset(&release(url), repository, "ffmpeg-osx-arm64", false).is_err());
        }
        Ok(())
    }

    #[tokio::test]
    async fn runtime_downloads_use_configured_proxy_connect() -> Result<()> {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
        let proxy = format!("http://{}", listener.local_addr()?);
        let fixture = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await?;
            let mut bytes = [0; 8192];
            let size = socket.read(&mut bytes).await?;
            let request = String::from_utf8_lossy(&bytes[..size]).to_string();
            socket
                .write_all(
                    b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
                )
                .await?;
            Ok::<_, anyhow::Error>(request)
        });
        assert!(
            runtime_client(&proxy)?
                .get("https://runtime-proxy.invalid/tool")
                .send()
                .await
                .is_err()
        );
        let request = tokio::time::timeout(std::time::Duration::from_secs(5), fixture).await???;
        assert!(request.starts_with("CONNECT runtime-proxy.invalid:443 HTTP/1.1"));
        Ok(())
    }
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
            std::fs::remove_file(&dll)?;
            assert!(
                format!(
                    "{:#}",
                    validate_frozen(&app, &new["ffmpeg"], "ffmpeg").unwrap_err()
                )
                .contains("DLL is missing or unreadable: avcodec-63.dll")
            );
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
        assert_eq!(
            asset_name("ffmpeg", "windows-aarch64")?,
            "ffmpeg-9.0.2-essentials-static-win-arm64.7z"
        );
        assert_eq!(asset_name("yt-dlp", "darwin-aarch64")?, "yt-dlp_macos");
        assert_eq!(asset_name("yt-dlp", "darwin-x86_64")?, "yt-dlp_macos");
        assert_eq!(
            asset_name("deno", "windows-aarch64")?,
            "deno-aarch64-pc-windows-msvc.zip"
        );
        assert_eq!(
            asset_name("deno", "darwin-aarch64")?,
            "deno-aarch64-apple-darwin.zip"
        );
        assert_eq!(asset_name("ffmpeg", "darwin-x86_64")?, "ffmpeg-osx-x64");
        assert_eq!(
            asset_name("ffprobe", "darwin-aarch64")?,
            "ffprobe-osx-arm64"
        );
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

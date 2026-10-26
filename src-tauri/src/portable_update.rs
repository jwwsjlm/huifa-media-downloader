// This Windows-only module clears FILE_ATTRIBUTE_READONLY during staging cleanup.
#![allow(clippy::permissions_set_readonly_false)]

use crate::updater::{Package, verify};
use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::HashSet,
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    os::windows::{fs::OpenOptionsExt, process::CommandExt},
    path::{Path, PathBuf},
    process::Command,
    time::{Duration, Instant},
};
use windows_sys::Win32::{
    Foundation::{CloseHandle, WAIT_OBJECT_0},
    Storage::FileSystem::ReplaceFileW,
    System::Threading::{OpenProcess, PROCESS_SYNCHRONIZE, WaitForSingleObject},
};

const WORK: &str = ".huifa-update";
const MANAGED: [&str; 5] = [
    "tools",
    "huifa-mcp-bridge.exe",
    "README.txt",
    "portable.flag",
    "HuifaDownloader.exe",
];

#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
enum Stage {
    Prepared,
    Replacing,
    Committed,
    RolledBack,
}

#[derive(Debug, Serialize, Deserialize)]
struct Journal {
    package: Package,
    stage: Stage,
    original: Vec<String>,
    error: Option<String>,
}

pub(crate) struct Prepared {
    directory: PathBuf,
    launched: bool,
    lock: Option<File>,
}

impl Drop for Prepared {
    fn drop(&mut self) {
        self.lock.take();
        if !self.launched {
            let _ = remove_tree(&self.directory);
        }
    }
}

impl Prepared {
    pub fn launch(mut self, executable: &Path) -> Result<()> {
        spawn_helper(executable, &self.directory, false)?;
        self.launched = true;
        Ok(())
    }
}

pub fn prepare(
    executable: &Path,
    bytes: &[u8],
    package: &Package,
    pubkey: &str,
) -> Result<Prepared> {
    verify(bytes, package, pubkey)?;
    ensure!(
        executable.file_name().and_then(|name| name.to_str()) == Some("HuifaDownloader.exe"),
        "Keep the portable executable named HuifaDownloader.exe before updating"
    );
    let root = executable
        .parent()
        .context("Portable application directory is unavailable")?;
    huifa_core::reject_links(root)?;
    ensure!(
        root.join("portable.flag").is_file(),
        "Portable marker is missing"
    );
    let directory = root.join(WORK);
    huifa_core::reject_links(&directory)?;
    fs::create_dir(&directory).context(
        "A portable update is already pending. Close and reopen the application to recover it",
    )?;
    let prepared = Prepared {
        lock: Some(lock(&directory)?),
        directory,
        launched: false,
    };
    fs::create_dir(prepared.directory.join("payload"))?;
    fs::create_dir(prepared.directory.join("backup"))?;
    extract(bytes, &prepared.directory.join("payload"), &package.target)?;
    let mut archive = File::create(prepared.directory.join("package.zip"))?;
    archive.write_all(bytes)?;
    archive.sync_all()?;
    let mut original = Vec::new();
    for name in MANAGED {
        let path = root.join(name);
        huifa_core::reject_links(&path)?;
        if path.try_exists()? {
            validate_kind(&path, name == "tools")?;
            original.push(name.into());
        }
    }
    write_journal(
        &prepared.directory,
        &Journal {
            package: package.clone(),
            stage: Stage::Prepared,
            original,
            error: None,
        },
    )?;
    Ok(prepared)
}

fn valid_tool(name: &str) -> bool {
    if [
        "yt-dlp.exe",
        "ffmpeg.exe",
        "ffprobe.exe",
        "qjs.exe",
        "deno.exe",
    ]
    .contains(&name)
    {
        return true;
    }
    name.strip_suffix(".dll")
        .and_then(|name| name.rsplit_once('-'))
        .is_some_and(|(prefix, version)| {
            [
                "avcodec",
                "avdevice",
                "avfilter",
                "avformat",
                "avutil",
                "postproc",
                "swresample",
                "swscale",
            ]
            .contains(&prefix)
                && !version.is_empty()
                && version.bytes().all(|byte| byte.is_ascii_digit())
        })
}

fn extract(bytes: &[u8], destination: &Path, target: &str) -> Result<()> {
    ensure!(
        Some(target) == crate::updater::target(true).as_deref(),
        "Portable update architecture does not match this application"
    );
    let mut archive = zip::ZipArchive::new(std::io::Cursor::new(bytes))?;
    ensure!(
        archive.len() <= 256,
        "Portable archive contains too many entries"
    );
    let mut names = HashSet::new();
    let mut expanded = 0u64;
    for index in 0..archive.len() {
        let entry = archive.by_index(index)?;
        let name = entry.name().to_owned();
        ensure!(
            !name.contains(['\\', ':', '\0']) && entry.enclosed_name().is_some(),
            "Unsafe portable archive path"
        );
        ensure!(
            names.insert(name.to_ascii_lowercase()),
            "Duplicate portable archive path"
        );
        ensure!(
            entry
                .unix_mode()
                .is_none_or(|mode| mode & 0o170000 != 0o120000),
            "Portable archive cannot contain symbolic links"
        );
        if entry.is_dir() {
            ensure!(name == "tools/", "Unexpected portable archive directory");
            continue;
        }
        ensure!(
            [
                "HuifaDownloader.exe",
                "huifa-mcp-bridge.exe",
                "README.txt",
                "portable.flag",
                "tools/manifest.json"
            ]
            .contains(&name.as_str())
                || name.strip_prefix("tools/").is_some_and(valid_tool),
            "Unexpected portable archive file"
        );
        expanded = expanded
            .checked_add(entry.size())
            .context("Portable archive size overflow")?;
        ensure!(
            expanded <= 2 * 1024 * 1024 * 1024,
            "Portable archive exceeds 2 GiB when extracted"
        );
        let path = destination.join(&name);
        fs::create_dir_all(path.parent().unwrap())?;
        let mut file = File::create(&path)?;
        let size = entry.size();
        let copied = std::io::copy(&mut entry.take(size + 1), &mut file)?;
        ensure!(copied == size, "Incomplete portable archive file");
        file.sync_all()?;
    }
    for name in MANAGED {
        validate_kind(&destination.join(name), name == "tools")?;
    }
    for name in ["HuifaDownloader.exe", "huifa-mcp-bridge.exe"] {
        verify_pe(&destination.join(name))?;
    }
    let receipt_path = destination.join("tools/manifest.json");
    ensure!(
        fs::metadata(&receipt_path)?.len() <= 1024 * 1024,
        "Portable tool manifest is too large"
    );
    let receipt: serde_json::Value = serde_json::from_slice(&fs::read(receipt_path)?)?;
    ensure!(
        receipt["target"].as_str() == target.strip_suffix("-portable"),
        "Portable tools target does not match the application"
    );
    let files = receipt["files"]
        .as_object()
        .context("Portable tools manifest is missing")?;
    for required in ["yt-dlp.exe", "ffmpeg.exe", "ffprobe.exe"] {
        ensure!(
            files.contains_key(required),
            "Required portable tool is missing"
        );
    }
    ensure!(
        files.contains_key("qjs.exe") || files.contains_key("deno.exe"),
        "Portable JavaScript runtime is missing"
    );
    for (name, digest) in files {
        ensure!(
            valid_tool(name),
            "Unexpected file in portable tool manifest"
        );
        let actual = format!(
            "{:x}",
            Sha256::digest(fs::read(destination.join("tools").join(name))?)
        );
        ensure!(
            digest.as_str() == Some(actual.as_str()),
            "Portable tool checksum mismatch"
        );
    }
    ensure!(
        fs::read_dir(destination.join("tools"))?.count() == files.len() + 1,
        "Portable tools differ from the signed manifest"
    );
    Ok(())
}

fn verify_pe(path: &Path) -> Result<()> {
    let bytes = fs::read(path)?;
    ensure!(
        bytes.len() >= 64 && &bytes[..2] == b"MZ",
        "Portable executable is not a Windows program"
    );
    let header = u32::from_le_bytes(bytes[60..64].try_into().unwrap()) as usize;
    let header = bytes
        .get(header..header.saturating_add(6))
        .context("Invalid Windows executable header")?;
    let machine = if cfg!(target_arch = "aarch64") {
        0xaa64u16
    } else {
        0x8664u16
    };
    ensure!(
        &header[..4] == b"PE\0\0"
            && u16::from_le_bytes(header[4..6].try_into().unwrap()) == machine,
        "Portable executable architecture does not match this application"
    );
    Ok(())
}

fn validate_kind(path: &Path, directory: bool) -> Result<()> {
    huifa_core::reject_links(path)?;
    let metadata = fs::metadata(path)?;
    ensure!(
        if directory {
            metadata.is_dir()
        } else {
            metadata.is_file()
        },
        "Invalid portable package layout"
    );
    if directory {
        for entry in fs::read_dir(path)? {
            let path = entry?.path();
            validate_kind(&path, fs::symlink_metadata(&path)?.is_dir())?;
        }
    }
    Ok(())
}

fn write_journal(directory: &Path, journal: &Journal) -> Result<()> {
    huifa_core::reject_links(directory)?;
    let path = directory.join("journal.json");
    huifa_core::reject_links(&path)?;
    let mut temporary = tempfile::NamedTempFile::new_in(directory)?;
    temporary.write_all(&serde_json::to_vec(journal)?)?;
    temporary.as_file().sync_all()?;
    temporary.persist(path).map_err(|reason| reason.error)?;
    Ok(())
}

fn read_journal(directory: &Path) -> Result<Journal> {
    let path = directory.join("journal.json");
    huifa_core::reject_links(&path)?;
    ensure!(
        fs::metadata(&path)?.len() <= 32 * 1024,
        "Portable update record is too large"
    );
    let journal: Journal = serde_json::from_slice(&fs::read(path)?)?;
    ensure!(
        journal
            .original
            .iter()
            .all(|name| MANAGED.contains(&name.as_str())),
        "Invalid portable update record"
    );
    ensure!(
        Some(journal.package.target.as_str()) == crate::updater::target(true).as_deref(),
        "Portable update record belongs to another architecture"
    );
    Ok(journal)
}

fn lock(directory: &Path) -> Result<File> {
    let path = directory.join("operation.lock");
    huifa_core::reject_links(&path)?;
    Ok(OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .share_mode(0)
        .open(path)?)
}

fn spawn_helper(executable: &Path, directory: &Path, recover: bool) -> Result<()> {
    let helper = directory.join("helper.exe");
    huifa_core::reject_links(&helper)?;
    fs::copy(executable, &helper)?;
    Command::new(helper)
        .args([if recover {
            "--huifa-portable-recover"
        } else {
            "--huifa-portable-update"
        }])
        .arg(directory)
        .arg(std::process::id().to_string())
        .current_dir(directory)
        .creation_flags(0x08000000)
        .spawn()
        .context("Could not start the portable update helper")?;
    Ok(())
}

fn wait_for_parent(pid: u32) -> Result<()> {
    ensure!(
        pid != 0 && pid != std::process::id(),
        "Invalid update parent process"
    );
    let handle = unsafe { OpenProcess(PROCESS_SYNCHRONIZE, 0, pid) };
    if handle.is_null() {
        let reason = std::io::Error::last_os_error();
        ensure!(
            reason.raw_os_error() == Some(87),
            "Could not wait for the application to exit: {reason}"
        );
        return Ok(());
    }
    let result = unsafe { WaitForSingleObject(handle, 60_000) };
    unsafe {
        CloseHandle(handle);
    }
    ensure!(
        result == WAIT_OBJECT_0,
        "The application did not exit; portable files were not replaced"
    );
    Ok(())
}

fn retry<T>(mut operation: impl FnMut() -> std::io::Result<T>) -> std::io::Result<T> {
    let started = Instant::now();
    loop {
        match operation() {
            Err(reason)
                if matches!(reason.raw_os_error(), Some(5 | 32 | 33))
                    && started.elapsed() < Duration::from_secs(5) =>
            {
                std::thread::sleep(Duration::from_millis(100))
            }
            result => return result,
        }
    }
}

fn replace_file(target: &Path, source: &Path, backup: &Path) -> Result<()> {
    use std::os::windows::ffi::OsStrExt;
    let wide = |path: &Path| {
        path.as_os_str()
            .encode_wide()
            .chain(Some(0))
            .collect::<Vec<_>>()
    };
    let target = wide(target);
    let source = wide(source);
    let backup = wide(backup);
    retry(|| {
        if unsafe {
            ReplaceFileW(
                target.as_ptr(),
                source.as_ptr(),
                backup.as_ptr(),
                0,
                std::ptr::null(),
                std::ptr::null(),
            )
        } != 0
        {
            Ok(())
        } else {
            Err(std::io::Error::last_os_error())
        }
    })?;
    Ok(())
}

fn copy_extra(source: &Path, destination: &Path) -> Result<()> {
    huifa_core::reject_links(source)?;
    huifa_core::reject_links(destination)?;
    if source.is_dir() {
        fs::create_dir(destination)?;
        for entry in fs::read_dir(source)? {
            let entry = entry?;
            copy_extra(&entry.path(), &destination.join(entry.file_name()))?;
        }
    } else {
        fs::copy(source, destination)?;
    }
    Ok(())
}

fn replace(root: &Path, directory: &Path, journal: &mut Journal) -> Result<()> {
    journal.stage = Stage::Replacing;
    write_journal(directory, journal)?;
    for name in MANAGED {
        let target = root.join(name);
        let source = directory.join("payload").join(name);
        let backup = directory.join("backup").join(name);
        huifa_core::reject_links(&target)?;
        validate_kind(&source, name == "tools")?;
        if target.try_exists()? {
            validate_kind(&target, name == "tools")?;
            if name == "tools" {
                for entry in fs::read_dir(&target)? {
                    let entry = entry?;
                    let name = entry.file_name();
                    let managed = name.to_str().is_some_and(|name| {
                        let name = name.to_ascii_lowercase();
                        name == "manifest.json" || valid_tool(&name)
                    });
                    if !managed {
                        copy_extra(&entry.path(), &source.join(name))?;
                    }
                }
                retry(|| fs::rename(&target, &backup))?;
                retry(|| fs::rename(&source, &target))?;
            } else {
                // ReplaceFile keeps the program path present if power fails during the final swap.
                replace_file(&target, &source, &backup)?;
            }
        } else {
            retry(|| fs::rename(&source, &target))?;
        }
    }
    journal.stage = Stage::Committed;
    write_journal(directory, journal)?;
    Ok(())
}

fn rollback(root: &Path, directory: &Path, journal: &mut Journal) -> Result<()> {
    journal.stage = Stage::Replacing;
    write_journal(directory, journal)?;
    fs::create_dir_all(directory.join("discard"))?;
    for name in MANAGED.into_iter().rev() {
        let target = root.join(name);
        let backup = directory.join("backup").join(name);
        huifa_core::reject_links(&backup)?;
        huifa_core::reject_links(&target)?;
        if backup.try_exists()? {
            validate_kind(&backup, name == "tools")?;
            if name != "tools" && target.try_exists()? {
                let discarded = directory.join("discard").join(name);
                huifa_core::reject_links(&discarded)?;
                if discarded.try_exists()? {
                    fs::remove_file(&discarded)?;
                }
                replace_file(&target, &backup, &discarded)?;
            } else {
                if target.try_exists()? {
                    remove_tree(&target)?;
                }
                retry(|| fs::rename(&backup, &target))?;
            }
        } else if !journal.original.iter().any(|original| original == name)
            && target.try_exists()?
        {
            if name == "tools" {
                remove_tree(&target)?;
            } else {
                retry(|| fs::remove_file(&target))?;
            }
        }
    }
    journal.stage = Stage::RolledBack;
    write_journal(directory, journal)?;
    Ok(())
}

fn remove_tree(path: &Path) -> Result<()> {
    huifa_core::reject_links(path)?;
    if !path.try_exists()? {
        return Ok(());
    }
    for entry in fs::read_dir(path)? {
        let path = entry?.path();
        huifa_core::reject_links(&path)?;
        let metadata = fs::metadata(&path)?;
        if metadata.is_dir() {
            remove_tree(&path)?;
        } else {
            let mut permissions = metadata.permissions();
            if permissions.readonly() {
                permissions.set_readonly(false);
                fs::set_permissions(&path, permissions)?;
            }
            retry(|| fs::remove_file(&path))?;
        }
    }
    fs::remove_dir(path)?;
    Ok(())
}

fn run_helper(directory: &Path, parent: u32, recover: bool) -> Result<()> {
    huifa_core::reject_links(directory)?;
    ensure!(
        directory.file_name().and_then(|name| name.to_str()) == Some(WORK),
        "Invalid portable update directory"
    );
    let root = directory
        .parent()
        .context("Portable application directory is unavailable")?;
    let started = Instant::now();
    let _lock = loop {
        match lock(directory) {
            Ok(guard) => break guard,
            Err(_) if started.elapsed() < Duration::from_secs(5) => {
                std::thread::sleep(Duration::from_millis(100))
            }
            Err(reason) => return Err(reason.context("A portable update is already running")),
        }
    };
    let mut journal = read_journal(directory)?;
    wait_for_parent(parent)?;
    let result = if recover {
        journal.error = Some("An interrupted portable update was rolled back. The previous version and saved tasks were kept.".into());
        rollback(root, directory, &mut journal)
    } else {
        let result = (|| {
            ensure!(
                journal.stage == Stage::Prepared,
                "Portable update has already started"
            );
            let archive = directory.join("package.zip");
            huifa_core::reject_links(&archive)?;
            ensure!(
                fs::metadata(&archive)?.len() <= 1024 * 1024 * 1024,
                "Portable archive exceeds 1 GiB"
            );
            let bytes = fs::read(archive)?;
            verify(&bytes, &journal.package, &crate::updater::public_key()?)?;
            // Re-extract verified bytes so modified staging files never become executable.
            remove_tree(&directory.join("payload"))?;
            fs::create_dir(directory.join("payload"))?;
            extract(&bytes, &directory.join("payload"), &journal.package.target)?;
            replace(root, directory, &mut journal)
        })();
        if let Err(reason) = result {
            journal.error = Some(format!(
                "Portable update failed and the previous version was restored: {}",
                crate::error(reason)
            ));
            rollback(root, directory, &mut journal)?;
        }
        Ok(())
    };
    if let Err(reason) = result {
        journal.error = Some(format!(
            "Portable update recovery failed: {}. Keep the .huifa-update folder and retry opening the application.",
            crate::error(&reason)
        ));
        let _ = write_journal(directory, &journal);
        return Err(reason);
    }
    let executable = root.join("HuifaDownloader.exe");
    if let Err(reason) = Command::new(&executable).current_dir(root).spawn() {
        if journal.stage == Stage::Committed {
            journal.error = Some(format!(
                "The new version could not be started: {reason}. The previous version was restored."
            ));
            rollback(root, directory, &mut journal)?;
            Command::new(executable).current_dir(root).spawn()?;
        } else {
            return Err(reason.into());
        }
    }
    Ok(())
}

pub fn startup() -> Result<Option<String>> {
    let args: Vec<_> = std::env::args_os().collect();
    if args
        .get(1)
        .is_some_and(|arg| arg == "--huifa-portable-update" || arg == "--huifa-portable-recover")
    {
        ensure!(args.len() == 4, "Invalid portable update arguments");
        let pid = args[3]
            .to_str()
            .context("Invalid update parent process")?
            .parse()?;
        run_helper(
            Path::new(&args[2]),
            pid,
            args[1] == "--huifa-portable-recover",
        )?;
        std::process::exit(0);
    }
    recover(&std::env::current_exe()?)
}

fn recover(executable: &Path) -> Result<Option<String>> {
    let root = executable
        .parent()
        .context("Application directory is unavailable")?;
    if !root.join("portable.flag").is_file() {
        return Ok(None);
    }
    let directory = root.join(WORK);
    huifa_core::reject_links(&directory)?;
    if !directory.try_exists()? {
        return Ok(None);
    }
    let guard = lock(&directory);
    if !directory.join("journal.json").try_exists()? {
        let guard = guard.context("A portable update is already running")?;
        drop(guard);
        let _ = remove_tree(&directory);
        return Ok(None);
    }
    let journal = read_journal(&directory)?;
    if matches!(journal.stage, Stage::Prepared | Stage::Replacing) {
        let _guard = guard.context("A portable update is already running")?;
        spawn_helper(executable, &directory, true)?;
        std::process::exit(0);
    }
    if let Ok(guard) = guard {
        drop(guard);
        // A finished update remains usable while locked staging files await cleanup.
        let _ = remove_tree(&directory);
    }
    Ok(journal.error)
}

#[cfg(test)]
mod tests {
    use super::*;
    use zip::{ZipWriter, write::SimpleFileOptions};

    fn pe() -> Vec<u8> {
        let mut bytes = vec![0u8; 70];
        bytes[..2].copy_from_slice(b"MZ");
        bytes[60..64].copy_from_slice(&64u32.to_le_bytes());
        bytes[64..68].copy_from_slice(b"PE\0\0");
        bytes[68..70].copy_from_slice(
            &(if cfg!(target_arch = "aarch64") {
                0xaa64u16
            } else {
                0x8664u16
            })
            .to_le_bytes(),
        );
        bytes
    }

    fn archive(extra: Option<(&str, bool)>, wrong_hash: bool, wrong_arch: bool) -> Result<Vec<u8>> {
        let mut writer = ZipWriter::new(std::io::Cursor::new(Vec::new()));
        let options = SimpleFileOptions::default();
        let mut executable = pe();
        if wrong_arch {
            executable[68..70].copy_from_slice(&0x014cu16.to_le_bytes());
        }
        let files = ["yt-dlp.exe", "ffmpeg.exe", "ffprobe.exe", "qjs.exe"]
            .into_iter()
            .map(|name| {
                (
                    name.to_string(),
                    serde_json::json!(format!("{:x}", Sha256::digest(b"fixture tool"))),
                )
            })
            .collect::<serde_json::Map<_, _>>();
        let mut receipt = serde_json::json!({"target":crate::updater::target(true).unwrap().trim_end_matches("-portable"),"files":files});
        if wrong_hash {
            receipt["files"]["ffmpeg.exe"] = serde_json::json!("0".repeat(64));
        }
        for (name, bytes) in [
            ("HuifaDownloader.exe", executable.clone()),
            ("huifa-mcp-bridge.exe", executable),
            ("portable.flag", Vec::new()),
            ("README.txt", b"New version".to_vec()),
            ("tools/manifest.json", serde_json::to_vec(&receipt)?),
            ("tools/yt-dlp.exe", b"fixture tool".to_vec()),
            ("tools/ffmpeg.exe", b"fixture tool".to_vec()),
            ("tools/ffprobe.exe", b"fixture tool".to_vec()),
            ("tools/qjs.exe", b"fixture tool".to_vec()),
        ] {
            writer.start_file(name, options)?;
            writer.write_all(&bytes)?;
        }
        if let Some((name, link)) = extra {
            if link {
                writer.add_symlink(name, "../outside", options)?;
            } else {
                writer.start_file(name, options)?;
                writer.write_all(b"unexpected")?;
            }
        }
        Ok(writer.finish()?.into_inner())
    }

    fn transaction() -> Result<(tempfile::TempDir, PathBuf, PathBuf, Journal)> {
        let temporary = tempfile::tempdir()?;
        let root = temporary.path().join("中文 download folder");
        fs::create_dir(&root)?;
        for name in MANAGED {
            if name == "tools" {
                fs::create_dir(root.join(name))?;
                fs::write(root.join("tools/avcodec-60.dll"), b"old tool")?;
                fs::write(root.join("tools/my-note.txt"), b"keep note")?;
                fs::create_dir(root.join("tools/custom"))?;
                fs::write(root.join("tools/custom/settings.json"), b"keep settings")?;
            } else {
                fs::write(root.join(name), b"old program")?;
            }
        }
        fs::write(root.join("my-video.mp4"), b"keep media")?;
        fs::create_dir(root.join("my-settings"))?;
        fs::write(root.join("my-settings/data.txt"), b"keep settings")?;
        let directory = root.join(WORK);
        fs::create_dir(&directory)?;
        fs::create_dir(directory.join("payload"))?;
        fs::create_dir(directory.join("backup"))?;
        let target = crate::updater::target(true).unwrap();
        extract(
            &archive(None, false, false)?,
            &directory.join("payload"),
            &target,
        )?;
        let journal = Journal {
            package: Package {
                version: "0.6.0".into(),
                target,
                url: "https://example.test/update.zip".into(),
                signature: "fixture".into(),
            },
            stage: Stage::Prepared,
            original: MANAGED.into_iter().map(str::to_owned).collect(),
            error: None,
        };
        write_journal(&directory, &journal)?;
        Ok((temporary, root, directory, journal))
    }

    fn assert_old(root: &Path) -> Result<()> {
        for name in MANAGED.into_iter().filter(|name| *name != "tools") {
            assert_eq!(fs::read(root.join(name))?, b"old program");
        }
        assert_eq!(fs::read(root.join("tools/avcodec-60.dll"))?, b"old tool");
        assert_eq!(fs::read(root.join("tools/my-note.txt"))?, b"keep note");
        assert_eq!(
            fs::read(root.join("tools/custom/settings.json"))?,
            b"keep settings"
        );
        assert_eq!(fs::read(root.join("my-video.mp4"))?, b"keep media");
        assert_eq!(
            fs::read(root.join("my-settings/data.txt"))?,
            b"keep settings"
        );
        Ok(())
    }

    #[test]
    fn portable_archives_reject_escape_links_duplicates_wrong_tools_and_architecture() -> Result<()>
    {
        for extra in [
            Some(("../outside", false)),
            Some(("tools/../outside", false)),
            Some(("C:/outside", false)),
            Some(("huifadownloader.exe", false)),
            Some(("extra.txt", false)),
            Some(("tools/link.exe", true)),
        ] {
            let directory = tempfile::tempdir()?;
            assert!(
                extract(
                    &archive(extra, false, false)?,
                    directory.path(),
                    &crate::updater::target(true).unwrap()
                )
                .is_err(),
                "{extra:?}"
            );
        }
        for (wrong_hash, wrong_arch) in [(true, false), (false, true)] {
            let directory = tempfile::tempdir()?;
            assert!(
                extract(
                    &archive(None, wrong_hash, wrong_arch)?,
                    directory.path(),
                    &crate::updater::target(true).unwrap()
                )
                .is_err()
            );
        }
        Ok(())
    }

    #[test]
    fn portable_transaction_replaces_only_managed_files_and_can_restore_the_previous_program()
    -> Result<()> {
        let (_temporary, root, directory, mut journal) = transaction()?;
        let note = root.join("tools/my-note.txt");
        let mut permissions = fs::metadata(&note)?.permissions();
        permissions.set_readonly(true);
        fs::set_permissions(&note, permissions)?;
        replace(&root, &directory, &mut journal)?;
        assert_eq!(read_journal(&directory)?.stage, Stage::Committed);
        assert_eq!(fs::read(root.join("HuifaDownloader.exe"))?, pe());
        assert!(!root.join("tools/avcodec-60.dll").exists());
        assert_eq!(fs::read(root.join("tools/my-note.txt"))?, b"keep note");
        assert!(fs::metadata(&note)?.permissions().readonly());
        assert_eq!(
            fs::read(root.join("tools/custom/settings.json"))?,
            b"keep settings"
        );
        assert_eq!(fs::read(root.join("my-video.mp4"))?, b"keep media");
        assert_eq!(
            fs::read(directory.join("backup/HuifaDownloader.exe"))?,
            b"old program"
        );
        rollback(&root, &directory, &mut journal)?;
        assert_eq!(read_journal(&directory)?.stage, Stage::RolledBack);
        assert_old(&root)?;
        rollback(&root, &directory, &mut journal)?;
        assert_old(&root)?;
        assert!(fs::metadata(&note)?.permissions().readonly());
        let mut permissions = fs::metadata(&note)?.permissions();
        permissions.set_readonly(false);
        fs::set_permissions(note, permissions)?;
        Ok(())
    }

    #[test]
    fn portable_replacement_does_not_preserve_case_variants_of_managed_files() -> Result<()> {
        let (_temporary, root, directory, mut journal) = transaction()?;
        let manifest = fs::read(directory.join("payload/tools/manifest.json"))?;
        for name in ["FFMPEG.EXE", "QjS.ExE", "AVCODEC-59.DLL", "MANIFEST.JSON"] {
            fs::write(root.join("tools").join(name), b"previous version")?;
        }
        replace(&root, &directory, &mut journal)?;
        for name in ["ffmpeg.exe", "qjs.exe"] {
            assert_eq!(fs::read(root.join("tools").join(name))?, b"fixture tool");
        }
        assert_eq!(fs::read(root.join("tools/manifest.json"))?, manifest);
        assert!(!root.join("tools/avcodec-59.dll").try_exists()?);
        rollback(&root, &directory, &mut journal)?;
        for name in ["FFMPEG.EXE", "QjS.ExE", "AVCODEC-59.DLL", "MANIFEST.JSON"] {
            assert_eq!(
                fs::read(root.join("tools").join(name))?,
                b"previous version"
            );
        }
        assert_old(&root)?;
        Ok(())
    }

    #[test]
    fn finished_update_cleanup_can_be_retried_without_blocking_startup() -> Result<()> {
        for stage in [Some(Stage::Committed), Some(Stage::RolledBack), None] {
            let (_temporary, root, directory, mut journal) = transaction()?;
            if let Some(stage) = stage {
                replace(&root, &directory, &mut journal)?;
                if stage == Stage::RolledBack {
                    rollback(&root, &directory, &mut journal)?;
                    journal.error = Some("Previous version restored".into());
                    write_journal(&directory, &journal)?;
                }
            } else {
                fs::remove_file(directory.join("journal.json"))?;
            }
            let program = fs::read(root.join("HuifaDownloader.exe"))?;
            let archive = directory.join("package.zip");
            fs::write(&archive, b"finished update")?;
            let held = OpenOptions::new().read(true).share_mode(0).open(&archive)?;
            assert_eq!(recover(&root.join("HuifaDownloader.exe"))?, journal.error);
            assert!(directory.try_exists()?);
            assert_eq!(fs::read(root.join("HuifaDownloader.exe"))?, program);
            assert_eq!(fs::read(root.join("my-video.mp4"))?, b"keep media");
            drop(held);
            recover(&root.join("HuifaDownloader.exe"))?;
            assert!(!directory.try_exists()?);
        }
        Ok(())
    }

    #[test]
    fn a_locked_program_file_rolls_back_files_already_replaced() -> Result<()> {
        let (_temporary, root, directory, mut journal) = transaction()?;
        let locked = OpenOptions::new()
            .read(true)
            .share_mode(0)
            .open(root.join("huifa-mcp-bridge.exe"))?;
        assert!(replace(&root, &directory, &mut journal).is_err());
        drop(locked);
        rollback(&root, &directory, &mut journal)?;
        assert_old(&root)?;
        Ok(())
    }

    #[test]
    fn interrupted_replacement_is_restored_from_the_durable_journal() -> Result<()> {
        let (_temporary, root, directory, mut journal) = transaction()?;
        journal.stage = Stage::Replacing;
        write_journal(&directory, &journal)?;
        fs::rename(root.join("tools"), directory.join("backup/tools"))?;
        fs::rename(directory.join("payload/tools"), root.join("tools"))?;
        let mut reopened = read_journal(&directory)?;
        rollback(&root, &directory, &mut reopened)?;
        assert_old(&root)?;
        assert_eq!(read_journal(&directory)?.stage, Stage::RolledBack);
        Ok(())
    }

    #[test]
    fn helper_lock_prevents_parallel_replacement_and_records_cannot_name_user_files() -> Result<()>
    {
        let (_temporary, root, directory, mut journal) = transaction()?;
        let guard = lock(&directory)?;
        assert!(lock(&directory).is_err());
        drop(guard);
        journal.original.push("my-video.mp4".into());
        write_journal(&directory, &journal)?;
        assert!(read_journal(&directory).is_err());
        assert_old(&root)?;
        let prepared = Prepared {
            lock: Some(lock(&directory)?),
            directory: directory.clone(),
            launched: false,
        };
        assert!(lock(&directory).is_err());
        drop(prepared);
        assert!(!directory.try_exists()?);
        assert_old(&root)?;
        Ok(())
    }
}

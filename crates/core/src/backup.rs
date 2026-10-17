use anyhow::{Context, Result, ensure};
use serde_json::json;
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::{Read, Write},
    path::{Path, PathBuf},
};

const VERSION_FILE: &str = "application-version.json";

pub fn before_upgrade(data_dir: &Path) -> Result<()> {
    let version = env!("CARGO_PKG_VERSION");
    let marker = data_dir.join(VERSION_FILE);
    crate::reject_links(&marker)?;
    let previous = if marker.try_exists()? {
        ensure!(
            fs::metadata(&marker)?.len() <= 4096,
            "Invalid application version record"
        );
        let value: serde_json::Value = serde_json::from_slice(&fs::read(&marker)?)?;
        value["version"].as_str().map(str::to_owned)
    } else {
        None
    };
    if previous.as_deref() == Some(version) {
        return Ok(());
    }
    if data_dir.join("rust-core.sqlite3").try_exists()?
        || data_dir.join("credentials").try_exists()?
    {
        create(data_dir, "before-upgrade")?;
    }
    let mut temporary = tempfile::NamedTempFile::new_in(data_dir)?;
    temporary.write_all(&serde_json::to_vec(&json!({"version":version}))?)?;
    temporary.as_file().sync_all()?;
    temporary.persist(marker).map_err(|reason| reason.error)?;
    Ok(())
}

pub fn create(data_dir: &Path, reason: &str) -> Result<PathBuf> {
    ensure!(
        matches!(
            reason,
            "before-upgrade" | "before-update" | "credentials-migration"
        ),
        "Invalid backup reason"
    );
    let root = data_dir.join("backups");
    crate::reject_links(&root)?;
    fs::create_dir_all(&root)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&root, fs::Permissions::from_mode(0o700))?;
    }
    let temporary = tempfile::tempdir_in(&root)?;
    let directory = temporary.path();
    let database = data_dir.join("rust-core.sqlite3");
    if database.try_exists()? {
        crate::reject_links(&database)?;
        let connection = rusqlite::Connection::open_with_flags(
            &database,
            rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
        )?;
        connection.busy_timeout(std::time::Duration::from_secs(5))?;
        connection.execute(
            "VACUUM INTO ?1",
            [directory
                .join("rust-core.sqlite3")
                .to_str()
                .context("Invalid backup path")?],
        )?;
    }
    for name in [
        "access.json",
        "download-directory.json",
        VERSION_FILE,
        ".credentials.key",
    ] {
        let source = data_dir.join(name);
        if source.try_exists()? {
            copy_file(&source, &directory.join(name))?;
        }
    }
    let credentials = data_dir.join("credentials");
    if credentials.try_exists()? {
        crate::reject_links(&credentials)?;
        fs::create_dir(directory.join("credentials"))?;
        for entry in fs::read_dir(credentials)? {
            let entry = entry?;
            copy_file(
                &entry.path(),
                &directory.join("credentials").join(entry.file_name()),
            )?;
        }
    }
    let preferences = data_dir.join("login-browser/preferences.json");
    if preferences.try_exists()? {
        fs::create_dir(directory.join("login-browser"))?;
        copy_file(
            &preferences,
            &directory.join("login-browser/preferences.json"),
        )?;
    }
    let mut files = serde_json::Map::new();
    manifest(directory, directory, &mut files)?;
    let metadata = json!({"version":env!("CARGO_PKG_VERSION"),"createdAt":chrono::Utc::now().to_rfc3339(),"reason":reason,"files":files});
    write_file(
        &directory.join("backup.json"),
        &serde_json::to_vec_pretty(&metadata)?,
    )?;
    write_file(&directory.join("RESTORE.txt"), b"Huifa configuration backup\n\nStop all Huifa processes or the container first. Keep a copy of the current data directory. Move its credentials directory aside, then restore these backed-up files into the same data directory, preserving subdirectories. Remove rust-core.sqlite3-wal and rust-core.sqlite3-shm from the destination before restoring the database. For a rollback, use the previous application version recorded in application-version.json when present.\n\nIncludes a consistent SQLite catalog, settings, access configuration, credentials, legacy credential key and login preferences. Does not include downloaded media, task workspaces, runtime tools or browser profiles. Keep those original directories for paused tasks and browser sessions. backup.json lists SHA-256 checksums. Treat this folder as sensitive account data.\n")?;
    #[cfg(unix)]
    fs::File::open(directory)?.sync_all()?;
    let destination = root.join(format!(
        "{}-{}-{}",
        chrono::Utc::now().format("%Y%m%dT%H%M%SZ"),
        reason,
        uuid::Uuid::new_v4()
    ));
    fs::rename(directory, &destination)?;
    #[cfg(unix)]
    fs::File::open(&root)?.sync_all()?;
    Ok(destination)
}

fn copy_file(source: &Path, target: &Path) -> Result<()> {
    crate::reject_links(source)?;
    ensure!(
        fs::metadata(source)?.is_file(),
        "Backup source must be a regular file"
    );
    let mut input = fs::File::open(source)?;
    let mut output = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(target)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        output.set_permissions(fs::Permissions::from_mode(0o600))?;
    }
    std::io::copy(&mut input, &mut output)?;
    output.sync_all()?;
    Ok(())
}

fn write_file(path: &Path, bytes: &[u8]) -> Result<()> {
    let mut file = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)?;
    file.write_all(bytes)?;
    file.sync_all()?;
    Ok(())
}

fn manifest(
    root: &Path,
    directory: &Path,
    files: &mut serde_json::Map<String, serde_json::Value>,
) -> Result<()> {
    for entry in fs::read_dir(directory)? {
        let path = entry?.path();
        if path.is_dir() {
            manifest(root, &path, files)?;
        } else {
            let mut file = fs::File::open(&path)?;
            let mut hash = Sha256::new();
            let mut buffer = [0u8; 65536];
            loop {
                let count = file.read(&mut buffer)?;
                if count == 0 {
                    break;
                }
                hash.update(&buffer[..count]);
            }
            files.insert(
                path.strip_prefix(root)?
                    .to_string_lossy()
                    .replace('\\', "/"),
                json!(hex::encode(hash.finalize())),
            );
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn upgrade_backup_is_consistent_restorable_and_not_repeated() -> Result<()> {
        let root = tempfile::tempdir()?;
        let connection = rusqlite::Connection::open(root.path().join("rust-core.sqlite3"))?;
        connection.execute_batch("PRAGMA journal_mode=WAL; CREATE TABLE example(value TEXT); INSERT INTO example VALUES('before');")?;
        fs::create_dir(root.path().join("credentials"))?;
        fs::write(root.path().join("credentials/old.enc"), b"encrypted-record")?;
        fs::write(root.path().join(".credentials.key"), [7u8; 32])?;
        fs::write(root.path().join(VERSION_FILE), br#"{"version":"0.0.1"}"#)?;
        before_upgrade(root.path())?;
        let backup = fs::read_dir(root.path().join("backups"))?
            .next()
            .unwrap()?
            .path();
        let restored = rusqlite::Connection::open(backup.join("rust-core.sqlite3"))?;
        assert_eq!(
            restored.query_row("SELECT value FROM example", [], |row| row
                .get::<_, String>(0))?,
            "before"
        );
        assert_eq!(
            fs::read(backup.join("credentials/old.enc"))?,
            b"encrypted-record"
        );
        assert_eq!(fs::read(backup.join(".credentials.key"))?, [7u8; 32]);
        assert!(backup.join("RESTORE.txt").is_file());
        assert!(!backup.join("rust-core.sqlite3-wal").exists());
        connection.execute("UPDATE example SET value='after'", [])?;
        assert_eq!(
            restored.query_row("SELECT value FROM example", [], |row| row
                .get::<_, String>(0))?,
            "before"
        );
        before_upgrade(root.path())?;
        assert_eq!(fs::read_dir(root.path().join("backups"))?.count(), 1);
        Ok(())
    }

    #[test]
    fn backup_failure_does_not_mark_upgrade_or_remove_originals() -> Result<()> {
        let root = tempfile::tempdir()?;
        fs::create_dir(root.path().join("credentials"))?;
        fs::create_dir(root.path().join("credentials/not-a-record"))?;
        fs::write(root.path().join(".credentials.key"), b"keep")?;
        assert!(before_upgrade(root.path()).is_err());
        assert!(!root.path().join(VERSION_FILE).exists());
        assert_eq!(fs::read(root.path().join(".credentials.key"))?, b"keep");
        assert_eq!(fs::read_dir(root.path().join("backups"))?.count(), 0);
        Ok(())
    }
}

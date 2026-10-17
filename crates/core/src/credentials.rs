use anyhow::{Context, Result, ensure};
use chacha20poly1305::{
    ChaCha20Poly1305, KeyInit, Nonce,
    aead::{Aead, Payload},
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::Write,
    path::{Path, PathBuf},
    sync::Mutex,
};
use uuid::Uuid;

const MAX_COOKIES: usize = 2 * 1024 * 1024;
const MAX_RECORD: usize = MAX_COOKIES * 6 + 4096;
const HEADER: &[u8] = b"HUIFA-CREDENTIALS-1\0";

pub struct CredentialStore {
    directory: PathBuf,
    lock: Mutex<()>,
}

#[derive(Serialize, Deserialize)]
struct Cookie {
    name: String,
    text: String,
}

impl CredentialStore {
    pub fn open(data_dir: &Path) -> Result<Self> {
        private_directory(data_dir)?;
        let directory = data_dir.join("credentials");
        private_directory(&directory)?;
        let store = Self {
            directory,
            lock: Mutex::new(()),
        };
        store.migrate_encrypted(data_dir)?;
        Ok(store)
    }

    pub fn put(&self, name: &str, text: &str) -> Result<Value> {
        validate_name(name)?;
        validate_cookies(text)?;
        let _guard = self
            .lock
            .lock()
            .map_err(|_| anyhow::anyhow!("credential store unavailable"))?;
        let id = Uuid::new_v4().to_string();
        self.write(
            &id,
            &serde_json::to_vec(&Cookie {
                name: name.trim().into(),
                text: text.into(),
            })?,
        )?;
        self.metadata(&id, name.trim(), text)
    }

    pub fn sync_browser(
        &self,
        profile: &str,
        name: &str,
        text: Option<&str>,
    ) -> Result<Option<(Value, bool)>> {
        ensure!(
            matches!(profile, "embedded" | "edge" | "chrome" | "chromium"),
            "Invalid login profile"
        );
        validate_name(name)?;
        if let Some(text) = text {
            validate_cookies(text)?;
        }
        let _guard = self
            .lock
            .lock()
            .map_err(|_| anyhow::anyhow!("credential store unavailable"))?;
        // A browser profile updates one record; task identities already have their own snapshots.
        let hash = Sha256::digest(format!("browser-login:{profile}"));
        let id = Uuid::from_bytes(hash[..16].try_into().unwrap()).to_string();
        if let Some(text) = text {
            let created = !self.path(&id).exists();
            let cookie = Cookie {
                name: name.trim().into(),
                text: text.into(),
            };
            let bytes = serde_json::to_vec(&cookie)?;
            match self.read(&id) {
                Ok(previous) if previous == bytes => {}
                Ok(_) => self.write(&id, &bytes)?,
                Err(reason) if self.path(&id).exists() => return Err(reason),
                Err(_) => self.write(&id, &bytes)?,
            }
            Ok(Some((self.metadata(&id, &cookie.name, text)?, created)))
        } else {
            let path = self.path(&id);
            if path.exists() {
                ensure_regular(&path)?;
                fs::remove_file(path)?;
            }
            Ok(None)
        }
    }

    pub fn matching(&self, url: &str) -> Result<Option<String>> {
        let parsed = url::Url::parse(url)?;
        let host = parsed
            .host_str()
            .context("Cookie target has no host")?
            .to_ascii_lowercase();
        let host = match host.as_str() {
            "youtu.be" | "www.youtu.be" | "youtube-nocookie.com" | "www.youtube-nocookie.com" => {
                "www.youtube.com"
            }
            "b23.tv" | "www.b23.tv" => "www.bilibili.com",
            "v.douyin.com" => "www.douyin.com",
            _ => &host,
        };
        let _guard = self
            .lock
            .lock()
            .map_err(|_| anyhow::anyhow!("credential store unavailable"))?;
        let mut records = Vec::new();
        for entry in fs::read_dir(&self.directory)? {
            let entry = entry?;
            let filename = entry.file_name();
            if let Some(id) = filename.to_str().and_then(|s| s.strip_suffix(".json"))
                && validate_id(id).is_ok()
            {
                records.push((entry.metadata()?.modified()?, id.to_owned()));
            }
        }
        records.sort();
        let now = chrono::Utc::now().timestamp();
        for (_, id) in records.into_iter().rev() {
            let cookie: Cookie = serde_json::from_slice(&self.read(&id)?)?;
            validate_cookies(&cookie.text)?;
            let mut matching = Vec::new();
            let mut matched_domain = false;
            for row in cookie_rows(&cookie.text) {
                let fields: Vec<_> = row.split('\t').collect();
                let domain = fields[0]
                    .strip_prefix("#HttpOnly_")
                    .unwrap_or(fields[0])
                    .trim_start_matches('.')
                    .to_ascii_lowercase();
                let expires = fields[4].parse::<i64>()?;
                if host == domain
                    || fields[1] == "TRUE"
                        && host
                            .strip_suffix(&domain)
                            .is_some_and(|prefix| prefix.ends_with('.'))
                {
                    matched_domain = true;
                    if expires == 0 || expires > now {
                        matching.push(row);
                    }
                }
            }
            // Keep one complete identity for the site; merging profiles can mix accounts or restore logged-out cookies.
            if matched_domain {
                if matching.is_empty() {
                    return Ok(None);
                }
                let text = format!("# Netscape HTTP Cookie File\n{}\n", matching.join("\n"));
                validate_cookies(&text)?;
                return Ok(Some(text));
            }
        }
        Ok(None)
    }

    pub fn get(&self, id: &str) -> Result<String> {
        validate_id(id)?;
        let _guard = self
            .lock
            .lock()
            .map_err(|_| anyhow::anyhow!("credential store unavailable"))?;
        let cookie: Cookie = serde_json::from_slice(&self.read(id)?)?;
        validate_cookies(&cookie.text)?;
        Ok(cookie.text)
    }

    pub fn list(&self) -> Result<Value> {
        let _guard = self
            .lock
            .lock()
            .map_err(|_| anyhow::anyhow!("credential store unavailable"))?;
        let mut result = Vec::new();
        for entry in fs::read_dir(&self.directory)? {
            let entry = entry?;
            let filename = entry.file_name();
            let Some(id) = filename.to_str().and_then(|s| s.strip_suffix(".json")) else {
                continue;
            };
            if validate_id(id).is_err() {
                continue;
            }
            let cookie: Cookie = serde_json::from_slice(&self.read(id)?)?;
            validate_name(&cookie.name)?;
            validate_cookies(&cookie.text)?;
            result.push(self.metadata(id, &cookie.name, &cookie.text)?);
        }
        result.sort_by(|a, b| a["name"].as_str().cmp(&b["name"].as_str()));
        Ok(Value::Array(result))
    }

    pub fn delete(&self, id: &str) -> Result<()> {
        validate_id(id)?;
        let _guard = self
            .lock
            .lock()
            .map_err(|_| anyhow::anyhow!("credential store unavailable"))?;
        let path = self.path(id);
        ensure_regular(&path)?;
        ensure!(
            fs::metadata(&path)?.len() <= MAX_RECORD as u64,
            "credential is too large"
        );
        fs::remove_file(path)?;
        Ok(())
    }

    pub fn put_secret(&self, name: &str, text: &str) -> Result<()> {
        validate_name(name)?;
        ensure!(text.len() <= MAX_COOKIES, "secret is too large");
        let _guard = self
            .lock
            .lock()
            .map_err(|_| anyhow::anyhow!("credential store unavailable"))?;
        self.write(&secret_id(name), text.as_bytes())
    }

    pub fn secret(&self, name: &str) -> Result<Option<String>> {
        validate_name(name)?;
        let _guard = self
            .lock
            .lock()
            .map_err(|_| anyhow::anyhow!("credential store unavailable"))?;
        let id = secret_id(name);
        match fs::symlink_metadata(self.path(&id)) {
            Ok(_) => {
                let bytes = self.read(&id)?;
                ensure!(bytes.len() <= MAX_COOKIES, "secret is too large");
                Ok(Some(String::from_utf8(bytes)?))
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(error) => Err(error.into()),
        }
    }

    pub fn delete_secret(&self, name: &str) -> Result<()> {
        validate_name(name)?;
        let _guard = self
            .lock
            .lock()
            .map_err(|_| anyhow::anyhow!("credential store unavailable"))?;
        let path = self.path(&secret_id(name));
        match fs::symlink_metadata(&path) {
            Ok(_) => {
                ensure_regular(&path)?;
                fs::remove_file(path)?;
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(error.into()),
        }
        Ok(())
    }

    fn metadata(&self, id: &str, name: &str, text: &str) -> Result<Value> {
        let mut value = cookie_metadata(id, name, text);
        let updated: chrono::DateTime<chrono::Utc> =
            fs::metadata(self.path(id))?.modified()?.into();
        value["updatedAt"] = json!(updated.to_rfc3339());
        Ok(value)
    }

    fn path(&self, id: &str) -> PathBuf {
        let extension = if id.starts_with("secret-") {
            "txt"
        } else {
            "json"
        };
        self.directory.join(format!("{id}.{extension}"))
    }

    fn write(&self, id: &str, plaintext: &[u8]) -> Result<()> {
        ensure!(plaintext.len() <= MAX_RECORD, "credential is too large");
        let mut temporary = tempfile::NamedTempFile::new_in(&self.directory)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            temporary
                .as_file()
                .set_permissions(fs::Permissions::from_mode(0o600))?;
        }
        temporary.write_all(plaintext)?;
        temporary.as_file().sync_all()?;
        let target = self.path(id);
        if target.exists() {
            ensure_regular(&target)?;
        }
        temporary.persist(&target).map_err(|error| error.error)?;
        #[cfg(unix)]
        std::fs::File::open(&self.directory)?.sync_all()?;
        Ok(())
    }

    fn read(&self, id: &str) -> Result<Vec<u8>> {
        let path = self.path(id);
        ensure_regular(&path)?;
        ensure!(
            fs::metadata(&path)?.len() <= MAX_RECORD as u64,
            "credential is too large"
        );
        Ok(fs::read(path)?)
    }

    fn migrate_encrypted(&self, data_dir: &Path) -> Result<()> {
        let mut legacy = Vec::new();
        for entry in fs::read_dir(&self.directory)? {
            let entry = entry?;
            let filename = entry.file_name();
            if let Some(id) = filename.to_str().and_then(|name| name.strip_suffix(".enc")) {
                ensure!(
                    validate_id(id).is_ok()
                        || id.strip_prefix("secret-").is_some_and(|hash| {
                            hash.len() == 64 && hash.bytes().all(|byte| byte.is_ascii_hexdigit())
                        }),
                    "invalid legacy credential ID"
                );
                legacy.push((id.to_owned(), entry.path()));
            }
        }
        if legacy.is_empty() {
            return Ok(());
        }
        let key_path = data_dir.join(".credentials.key");
        ensure_regular(&key_path)
            .context("Restore .credentials.key to migrate legacy credentials")?;
        ensure!(
            fs::metadata(&key_path)?.len() == 32,
            "invalid legacy credential key"
        );
        let key = fs::read(&key_path)?;
        let cipher = ChaCha20Poly1305::new_from_slice(&key)
            .map_err(|_| anyhow::anyhow!("invalid legacy credential key"))?;
        crate::backup::create(data_dir, "credentials-migration")
            .context("Could not back up legacy credentials; migration was not started")?;
        for (id, path) in legacy {
            ensure_regular(&path)?;
            ensure!(
                fs::metadata(&path)?.len() <= MAX_RECORD as u64,
                "legacy credential is too large"
            );
            let bytes = fs::read(&path)?;
            ensure!(
                bytes.len() >= HEADER.len() + 28 && bytes.starts_with(HEADER),
                "invalid legacy credential"
            );
            let plaintext = cipher.decrypt(
                Nonce::from_slice(&bytes[HEADER.len()..HEADER.len() + 12]),
                Payload { msg: &bytes[HEADER.len() + 12..], aad: id.as_bytes() },
            ).map_err(|_| anyhow::anyhow!("Legacy credential migration failed; the original .credentials.key is required"))?;
            if id.starts_with("secret-") {
                ensure!(plaintext.len() <= MAX_COOKIES, "secret is too large");
                std::str::from_utf8(&plaintext)?;
            } else {
                let cookie: Cookie = serde_json::from_slice(&plaintext)?;
                validate_name(&cookie.name)?;
                validate_cookies(&cookie.text)?;
            }
            if self.path(&id).exists() {
                ensure!(
                    self.read(&id)? == plaintext,
                    "Plaintext and legacy credentials differ; preserve both files before retrying migration"
                );
            } else {
                self.write(&id, &plaintext)?;
            }
            fs::remove_file(path)?;
        }
        if !fs::read_dir(&self.directory)?.any(|entry| {
            entry.map_or(true, |entry| {
                entry
                    .path()
                    .extension()
                    .is_some_and(|extension| extension == "enc")
            })
        }) {
            fs::remove_file(key_path)?;
        }
        Ok(())
    }
}

fn cookie_rows(text: &str) -> impl Iterator<Item = &str> {
    text.lines().filter(|line| {
        !line.is_empty() && (!line.starts_with('#') || line.starts_with("#HttpOnly_"))
    })
}

fn cookie_metadata(id: &str, name: &str, text: &str) -> Value {
    let mut domains: Vec<_> = cookie_rows(text)
        .filter_map(|row| row.split('\t').next())
        .map(|domain| {
            domain
                .strip_prefix("#HttpOnly_")
                .unwrap_or(domain)
                .trim_start_matches('.')
        })
        .collect();
    domains.sort_unstable();
    domains.dedup();
    json!({"id":id,"name":name,"domains":domains,"count":cookie_rows(text).count()})
}

pub fn validate_cookies(text: &str) -> Result<()> {
    ensure!(
        !text.is_empty() && text.len() <= MAX_COOKIES,
        "cookies must be nonempty and at most 2 MiB"
    );
    ensure!(
        text.starts_with("# Netscape HTTP Cookie File") || text.starts_with("# HTTP Cookie File"),
        "expected a Netscape cookie file"
    );
    let mut count = 0;
    for line in text.lines() {
        let line = line.trim_end_matches('\r');
        if line.is_empty() || (line.starts_with('#') && !line.starts_with("#HttpOnly_")) {
            continue;
        }
        let columns: Vec<_> = line.split('\t').collect();
        ensure!(columns.len() == 7, "invalid Netscape cookie row");
        ensure!(
            !columns[0].is_empty()
                && !columns[2].is_empty()
                && (!columns[5].is_empty() || !columns[6].is_empty()),
            "invalid Netscape cookie row"
        );
        ensure!(
            matches!(columns[1], "TRUE" | "FALSE")
                && matches!(columns[3], "TRUE" | "FALSE")
                && columns[4].parse::<i64>().is_ok(),
            "invalid Netscape cookie row"
        );
        ensure!(
            !line.chars().any(|c| c.is_control() && c != '\t'),
            "invalid control character in cookies"
        );
        count += 1;
    }
    ensure!(
        count > 0 && count <= 10000,
        "cookie file must contain 1 to 10000 cookies"
    );
    Ok(())
}

fn validate_name(name: &str) -> Result<()> {
    ensure!(
        !name.trim().is_empty() && name.len() <= 128 && !name.chars().any(char::is_control),
        "invalid credential name"
    );
    Ok(())
}

fn validate_id(id: &str) -> Result<()> {
    ensure!(
        Uuid::parse_str(id)?.to_string() == id,
        "invalid credential ID"
    );
    Ok(())
}

fn secret_id(name: &str) -> String {
    format!("secret-{:x}", Sha256::digest(name.as_bytes()))
}

fn private_directory(path: &Path) -> Result<()> {
    fs::create_dir_all(path)?;
    let metadata = fs::symlink_metadata(path)?;
    ensure!(
        metadata.is_dir() && !metadata.file_type().is_symlink(),
        "credential directory must be an owned directory"
    );
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
    }
    Ok(())
}

fn ensure_regular(path: &Path) -> Result<()> {
    let metadata = fs::symlink_metadata(path)?;
    ensure!(
        metadata.is_file() && !metadata.file_type().is_symlink(),
        "credential must be a regular file"
    );
    #[cfg(unix)]
    {
        use std::os::unix::fs::{MetadataExt, PermissionsExt};
        let directory = fs::metadata(path.parent().context("missing credential directory")?)?;
        ensure!(
            metadata.uid() == directory.uid() && metadata.permissions().mode() & 0o077 == 0,
            "credential permissions must be private"
        );
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    const COOKIES: &str =
        "# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tTRUE\t0\tsession\ttop-secret\n";

    #[test]
    fn nameless_cookies_roundtrip_but_empty_or_control_content_is_rejected() -> Result<()> {
        let root = tempfile::tempdir()?;
        let store = CredentialStore::open(root.path())?;
        let cookies =
            "# Netscape HTTP Cookie File\nwww.example.com\tFALSE\t/\tTRUE\t0\t\tanonymous\n";
        let saved = store.put("Nameless", cookies)?;
        assert_eq!(store.get(saved["id"].as_str().unwrap())?, cookies);
        assert_eq!(
            store.matching("https://www.example.com/video")?.as_deref(),
            Some(cookies)
        );
        for value in ["", "bad\nrow", "bad\trow", "bad\rrow", "bad\0row"] {
            assert!(validate_cookies(&cookies.replace("anonymous", value)).is_err());
        }
        Ok(())
    }

    #[test]
    fn douyin_short_links_keep_canonical_host_cookies() -> Result<()> {
        let root = tempfile::tempdir()?;
        let store = CredentialStore::open(root.path())?;
        let cookies = "# Netscape HTTP Cookie File\n#HttpOnly_www.douyin.com\tFALSE\t/\tTRUE\t0\tsession\tdouyin-login\n";
        store.put("Douyin", cookies)?;
        assert_eq!(
            store.matching("https://v.douyin.com/example/")?.as_deref(),
            Some(cookies)
        );
        assert!(
            store
                .matching("https://www.bilibili.com/video/demo")?
                .is_none()
        );
        assert!(
            store
                .matching("https://v.douyin.com.attacker.invalid/example/")?
                .is_none()
        );
        assert!(store.matching("https://notdouyin.com/example/")?.is_none());
        Ok(())
    }

    #[test]
    fn automatic_matching_is_scoped_and_keeps_profiles_separate() -> Result<()> {
        let root = tempfile::tempdir()?;
        let store = CredentialStore::open(root.path())?;
        let jar = "# Netscape HTTP Cookie File\n#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tyoutube-login\nwww.youtube.com\tFALSE\t/private\tTRUE\t0\thost\thost-login\n.youtube.com\tTRUE\t/\tTRUE\t1\texpired\tignore\n.bilibili.com\tTRUE\t/\tTRUE\t0\tSESSDATA\tbilibili-login\n";
        store.put("Both websites", jar)?;
        let youtube = store
            .matching("https://www.youtube.com/watch?v=demo")?
            .unwrap();
        assert!(youtube.contains("youtube-login") && youtube.contains("host-login"));
        assert!(!youtube.contains("bilibili-login") && !youtube.contains("expired"));
        assert!(
            store
                .matching("https://m.youtube.com/")?
                .unwrap()
                .contains("youtube-login")
        );
        assert!(
            !store
                .matching("https://m.youtube.com/")?
                .unwrap()
                .contains("host-login")
        );
        assert!(
            store
                .matching("https://youtu.be/demo")?
                .unwrap()
                .contains("youtube-login")
        );
        assert!(
            store
                .matching("https://b23.tv/demo")?
                .unwrap()
                .contains("bilibili-login")
        );
        assert!(store.matching("https://notyoutube.com/")?.is_none());
        assert!(
            store
                .matching("https://youtube.com.attacker.invalid/")?
                .is_none()
        );
        let newer =
            "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tconsent\tnew-profile\n";
        let entry = store.put("New profile without login", newer)?;
        std::fs::File::options()
            .write(true)
            .open(store.path(entry["id"].as_str().unwrap()))?
            .set_modified(std::time::SystemTime::now() + std::time::Duration::from_secs(1))?;
        assert_eq!(
            store.matching("https://www.youtube.com/")?.as_deref(),
            Some(newer)
        );
        Ok(())
    }

    #[test]
    fn browser_sync_reuses_its_record_and_failed_updates_keep_previous_cookies() -> Result<()> {
        let root = tempfile::tempdir()?;
        let store = CredentialStore::open(root.path())?;
        let (first, created) = store
            .sync_browser("embedded", "Browser", Some(COOKIES))?
            .unwrap();
        assert!(created);
        let id = first["id"].as_str().unwrap();
        let modified = fs::metadata(store.path(id))?.modified()?;
        let (same, created) = store
            .sync_browser("embedded", "Browser", Some(COOKIES))?
            .unwrap();
        assert_eq!(first, same);
        assert!(!created);
        assert_eq!(fs::metadata(store.path(id))?.modified()?, modified);
        let updated = COOKIES.replace("top-secret", "new-login");
        let (second, _) = store
            .sync_browser("embedded", "Browser", Some(&updated))?
            .unwrap();
        assert_eq!(first["id"], second["id"]);
        assert_eq!(store.list()?.as_array().unwrap().len(), 1);
        assert!(
            store
                .sync_browser("embedded", "Browser", Some("invalid cookies"))
                .is_err()
        );
        assert_eq!(store.get(id)?, updated);
        assert_eq!(CredentialStore::open(root.path())?.get(id)?, updated);
        store.sync_browser("embedded", "Browser", None)?;
        assert!(store.list()?.as_array().unwrap().is_empty());
        Ok(())
    }

    fn legacy_record(store: &CredentialStore, id: &str, plaintext: &[u8]) -> Result<PathBuf> {
        let key = [17u8; 32];
        let nonce = [9u8; 12];
        let cipher = ChaCha20Poly1305::new((&key).into());
        let ciphertext = cipher
            .encrypt(
                Nonce::from_slice(&nonce),
                Payload {
                    msg: plaintext,
                    aad: id.as_bytes(),
                },
            )
            .map_err(|_| anyhow::anyhow!("legacy fixture encryption failed"))?;
        let mut bytes = HEADER.to_vec();
        bytes.extend_from_slice(&nonce);
        bytes.extend_from_slice(&ciphertext);
        let key_path = store.directory.parent().unwrap().join(".credentials.key");
        let path = store.directory.join(format!("{id}.enc"));
        fs::write(&key_path, key)?;
        fs::write(&path, bytes)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            for file in [&key_path, &path] {
                fs::set_permissions(file, fs::Permissions::from_mode(0o600))?;
            }
        }
        Ok(path)
    }

    #[test]
    fn legacy_migration_preserves_ids_and_snapshots_and_resumes_partial_work() -> Result<()> {
        let root = tempfile::tempdir()?;
        let store = CredentialStore::open(root.path())?;
        let converted = store.put("Already converted", COOKIES)?;
        let converted_id = converted["id"].as_str().unwrap();
        legacy_record(&store, converted_id, &store.read(converted_id)?)?;
        let id = Uuid::new_v4().to_string();
        legacy_record(
            &store,
            &id,
            &serde_json::to_vec(&Cookie {
                name: "Legacy login".into(),
                text: COOKIES.into(),
            })?,
        )?;
        legacy_record(&store, &secret_id("task-cookie:frozen"), COOKIES.as_bytes())?;
        legacy_record(&store, &secret_id("aiApiKey"), b"old-api-key")?;
        drop(store);

        let store = CredentialStore::open(root.path())?;
        assert_eq!(store.get(&id)?, COOKIES);
        assert_eq!(store.get(converted_id)?, COOKIES);
        assert_eq!(
            store.secret("task-cookie:frozen")?.as_deref(),
            Some(COOKIES)
        );
        assert_eq!(store.secret("aiApiKey")?.as_deref(), Some("old-api-key"));
        assert_eq!(store.list()?.as_array().unwrap().len(), 2);
        assert!(!root.path().join(".credentials.key").exists());
        assert!(!fs::read_dir(&store.directory)?.any(|entry| {
            entry
                .unwrap()
                .path()
                .extension()
                .is_some_and(|extension| extension == "enc")
        }));
        assert!(fs::read_to_string(store.path(&id))?.contains("top-secret"));
        let backup = fs::read_dir(root.path().join("backups"))?
            .next()
            .unwrap()?
            .path();
        assert_eq!(fs::read(backup.join(".credentials.key"))?, [17u8; 32]);
        assert!(
            backup
                .join("credentials")
                .join(format!("{id}.enc"))
                .is_file()
        );
        assert!(
            !backup
                .join("credentials")
                .join(format!("{id}.json"))
                .exists()
        );
        let restored = tempfile::tempdir()?;
        fs::create_dir(restored.path().join("credentials"))?;
        fs::copy(
            backup.join(".credentials.key"),
            restored.path().join(".credentials.key"),
        )?;
        fs::copy(
            backup.join("credentials").join(format!("{id}.enc")),
            restored
                .path()
                .join("credentials")
                .join(format!("{id}.enc")),
        )?;
        assert_eq!(CredentialStore::open(restored.path())?.get(&id)?, COOKIES);
        drop(store);
        assert_eq!(CredentialStore::open(root.path())?.get(&id)?, COOKIES);
        Ok(())
    }

    #[test]
    fn failed_legacy_migration_preserves_original_files_and_key() -> Result<()> {
        let root = tempfile::tempdir()?;
        let store = CredentialStore::open(root.path())?;
        let id = Uuid::new_v4().to_string();
        let path = legacy_record(
            &store,
            &id,
            &serde_json::to_vec(&Cookie {
                name: "Legacy".into(),
                text: COOKIES.into(),
            })?,
        )?;
        let mut bytes = fs::read(&path)?;
        let last = bytes.len() - 1;
        bytes[last] ^= 1;
        fs::write(&path, &bytes)?;
        assert!(CredentialStore::open(root.path()).is_err());
        assert_eq!(fs::read(&path)?, bytes);
        assert!(root.path().join(".credentials.key").is_file());
        assert!(!store.path(&id).exists());
        Ok(())
    }

    #[test]
    fn legacy_migration_does_not_overwrite_different_plaintext() -> Result<()> {
        let root = tempfile::tempdir()?;
        let store = CredentialStore::open(root.path())?;
        let saved = store.put("Current", COOKIES)?;
        let id = saved["id"].as_str().unwrap();
        let path = legacy_record(
            &store,
            id,
            &serde_json::to_vec(&Cookie {
                name: "Old".into(),
                text: COOKIES.replace("top-secret", "old-secret"),
            })?,
        )?;
        assert!(CredentialStore::open(root.path()).is_err());
        assert_eq!(store.get(id)?, COOKIES);
        assert!(path.is_file());
        assert!(root.path().join(".credentials.key").is_file());
        Ok(())
    }
    #[test]
    fn plaintext_persistence_and_metadata_redaction() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let store = CredentialStore::open(dir.path())?;
        let meta = store.put("Example", COOKIES)?;
        let id = meta["id"].as_str().unwrap();
        store.put_secret("aiApiKey", "key-secret")?;
        assert!(!format!("{}", store.list()?).contains("top-secret"));
        assert!(fs::read_to_string(store.path(id))?.contains("top-secret"));
        assert_eq!(
            fs::read_to_string(store.path(&secret_id("aiApiKey")))?,
            "key-secret"
        );
        assert!(!dir.path().join(".credentials.key").exists());
        drop(store);
        let store = CredentialStore::open(dir.path())?;
        assert_eq!(store.get(id)?, COOKIES);
        assert_eq!(store.secret("aiApiKey")?.as_deref(), Some("key-secret"));
        let snapshot = "x".repeat(MAX_COOKIES);
        store.put_secret("task-cookie:test", &snapshot)?;
        assert_eq!(store.secret("task-cookie:test")?, Some(snapshot));
        assert!(
            store
                .put_secret("task-cookie:test", &"x".repeat(MAX_COOKIES + 1))
                .is_err()
        );
        store.delete(id)?;
        assert!(store.get(id).is_err());
        Ok(())
    }
    #[test]
    fn rejects_invalid_records_and_escape() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let store = CredentialStore::open(dir.path())?;
        let meta = store.put("Example", COOKIES)?;
        let id = meta["id"].as_str().unwrap();
        fs::write(store.path(id), b"not a cookie record")?;
        assert!(store.get(id).is_err());
        fs::write(
            store.path(id),
            serde_json::to_vec(&Cookie {
                name: "Example".into(),
                text: "invalid cookies".into(),
            })?,
        )?;
        assert!(store.get(id).is_err());
        assert!(store.list().is_err());
        assert!(store.get("../.credentials.key").is_err());
        assert!(store.put("Example", "not cookies").is_err());
        Ok(())
    }
}

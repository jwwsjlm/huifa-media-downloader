use anyhow::{Context, Result, ensure};
use chacha20poly1305::{
    ChaCha20Poly1305, KeyInit, Nonce,
    aead::{Aead, Payload},
};
use rand_core::{OsRng, RngCore};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    fs::{self, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
    sync::Mutex,
};
use uuid::Uuid;

const MAX_COOKIES: usize = 2 * 1024 * 1024;
const HEADER: &[u8] = b"HUIFA-CREDENTIALS-1\0";

pub struct CredentialStore {
    directory: PathBuf,
    cipher: ChaCha20Poly1305,
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
        let key_path = data_dir.join(".credentials.key");
        let mut key = [0u8; 32];
        match private_file(&key_path).create_new(true).open(&key_path) {
            Ok(mut file) => {
                OsRng.fill_bytes(&mut key);
                file.write_all(&key)?;
                file.sync_all()?;
            }
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                ensure_regular(&key_path)?;
                let bytes = fs::read(&key_path).context("cannot read credential key")?;
                ensure!(
                    bytes.len() == key.len(),
                    "invalid credential key; preserve the data volume and restore its key"
                );
                key.copy_from_slice(&bytes);
            }
            Err(error) => return Err(error.into()),
        }
        Ok(Self {
            directory,
            cipher: ChaCha20Poly1305::new((&key).into()),
            lock: Mutex::new(()),
        })
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
        Ok(json!({"id": id, "name": name.trim()}))
    }

    pub fn get(&self, id: &str) -> Result<String> {
        validate_id(id)?;
        let _guard = self
            .lock
            .lock()
            .map_err(|_| anyhow::anyhow!("credential store unavailable"))?;
        let cookie: Cookie = serde_json::from_slice(&self.read(id)?)?;
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
            let Some(id) = filename.to_str().and_then(|s| s.strip_suffix(".enc")) else {
                continue;
            };
            if validate_id(id).is_err() {
                continue;
            }
            let cookie: Cookie = serde_json::from_slice(&self.read(id)?)?;
            result.push(json!({"id": id, "name": cookie.name}));
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
            fs::metadata(&path)?.len() <= (MAX_COOKIES + 4096) as u64,
            "encrypted credential is too large"
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
            Ok(_) => Ok(Some(String::from_utf8(self.read(&id)?)?)),
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

    fn path(&self, id: &str) -> PathBuf {
        self.directory.join(format!("{id}.enc"))
    }

    fn write(&self, id: &str, plaintext: &[u8]) -> Result<()> {
        let mut nonce = [0u8; 12];
        OsRng.fill_bytes(&mut nonce);
        let encrypted = self
            .cipher
            .encrypt(
                Nonce::from_slice(&nonce),
                Payload {
                    msg: plaintext,
                    aad: id.as_bytes(),
                },
            )
            .map_err(|_| anyhow::anyhow!("credential encryption failed"))?;
        let mut temporary = tempfile::NamedTempFile::new_in(&self.directory)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            temporary
                .as_file()
                .set_permissions(fs::Permissions::from_mode(0o600))?;
        }
        temporary.write_all(HEADER)?;
        temporary.write_all(&nonce)?;
        temporary.write_all(&encrypted)?;
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
        let bytes = fs::read(path)?;
        ensure!(
            bytes.len() >= HEADER.len() + 12 + 16 && bytes.starts_with(HEADER),
            "invalid encrypted credential"
        );
        self.cipher
            .decrypt(
                Nonce::from_slice(&bytes[HEADER.len()..HEADER.len() + 12]),
                Payload {
                    msg: &bytes[HEADER.len() + 12..],
                    aad: id.as_bytes(),
                },
            )
            .map_err(|_| {
                anyhow::anyhow!(
                    "credential authentication failed; the original data-volume key is required"
                )
            })
    }
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
            !columns[0].is_empty() && !columns[2].is_empty() && !columns[5].is_empty(),
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

fn private_file(_path: &Path) -> OpenOptions {
    let mut options = OpenOptions::new();
    options.write(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    options
}

#[cfg(test)]
mod tests {
    use super::*;
    const COOKIES: &str =
        "# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tTRUE\t0\tsession\ttop-secret\n";
    #[test]
    fn encrypted_persistence_and_redaction() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let store = CredentialStore::open(dir.path())?;
        let meta = store.put("Example", COOKIES)?;
        let id = meta["id"].as_str().unwrap();
        store.put_secret("aiApiKey", "key-secret")?;
        assert!(!format!("{}", store.list()?).contains("top-secret"));
        assert!(
            !fs::read(store.path(id))?
                .windows(10)
                .any(|x| x == b"top-secret")
        );
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
    fn rejects_tampering_and_escape() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let store = CredentialStore::open(dir.path())?;
        let meta = store.put("Example", COOKIES)?;
        let id = meta["id"].as_str().unwrap();
        let mut bytes = fs::read(store.path(id))?;
        let last = bytes.len() - 1;
        bytes[last] ^= 1;
        fs::write(store.path(id), bytes)?;
        assert!(store.get(id).is_err());
        assert!(store.get("../.credentials.key").is_err());
        assert!(store.put("Example", "not cookies").is_err());
        Ok(())
    }
}

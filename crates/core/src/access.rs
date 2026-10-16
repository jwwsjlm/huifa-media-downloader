use anyhow::{Context, Result, ensure};
use argon2::{Argon2, PasswordHash, PasswordHasher, PasswordVerifier, password_hash::SaltString};
use rand_core::OsRng;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::HashMap,
    io::Write,
    net::IpAddr,
    path::{Path, PathBuf},
    sync::{Mutex, RwLock},
    time::{Duration, Instant},
};
use subtle::ConstantTimeEq;

const LOGIN_WINDOW: Duration = Duration::from_secs(15 * 60);

#[derive(Serialize, Deserialize)]
struct Credentials {
    account: String,
    password_hash: String,
    default_password: bool,
}
struct State {
    credentials: Credentials,
    generation: u64,
    bearer_digest: Option<[u8; 32]>,
}
pub struct Access {
    path: PathBuf,
    state: RwLock<State>,
    failures: Mutex<HashMap<IpAddr, (Instant, u32)>>,
}
impl Access {
    pub fn open(directory: &Path) -> Result<Self> {
        if !directory.join("access.json").exists() {
            Self::initialize(
                directory,
                &std::env::var("HUIFA_ACCOUNT").unwrap_or_else(|_| "admin".into()),
                &std::env::var("HUIFA_PASSWORD").unwrap_or_else(|_| "admin".into()),
            )?;
        }
        let path = directory.join("access.json");
        regular(&path)?;
        ensure!(
            std::fs::metadata(&path)?.len() <= 16 * 1024,
            "Access configuration is too large"
        );
        let credentials: Credentials = serde_json::from_slice(&std::fs::read(&path)?)
            .context("Invalid access configuration")?;
        PasswordHash::new(&credentials.password_hash)
            .map_err(|_| anyhow::anyhow!("Invalid access password hash"))?;
        ensure!(
            !credentials.account.is_empty() && credentials.account.len() <= 128,
            "Invalid access account"
        );
        Ok(Self {
            path,
            state: RwLock::new(State {
                credentials,
                generation: 0,
                bearer_digest: None,
            }),
            failures: Mutex::new(HashMap::new()),
        })
    }
    pub fn initialize(directory: &Path, account: &str, password: &str) -> Result<()> {
        let path = directory.join("access.json");
        if path.exists() {
            regular(&path)?;
            return Ok(());
        }
        ensure!(
            !account.trim().is_empty() && account.len() <= 128,
            "Account must contain 1–128 bytes"
        );
        validate_password(password)?;
        std::fs::create_dir_all(directory)?;
        write(&path, &credentials(account, password)?, true)
    }
    pub fn status(&self) -> Value {
        let state = self.state.read().unwrap();
        json!({"account":state.credentials.account,"defaultPassword":state.credentials.default_password})
    }
    pub fn verify(&self, account: &str, password: &str) -> bool {
        let state = self.state.read().unwrap();
        let hash = PasswordHash::new(&state.credentials.password_hash)
            .expect("validated access password hash");
        let valid_password = Argon2::default()
            .verify_password(password.as_bytes(), &hash)
            .is_ok();
        valid_password & bool::from(digest(account).ct_eq(&digest(&state.credentials.account)))
    }
    pub fn verify_bearer(&self, password: &str) -> bool {
        if password.is_empty() || password.len() > 4096 {
            return false;
        }
        let candidate = digest(password);
        let state = self.state.read().unwrap();
        if let Some(cached) = state.bearer_digest {
            return bool::from(cached.ct_eq(&candidate));
        }
        let hash = PasswordHash::new(&state.credentials.password_hash)
            .expect("validated access password hash");
        let valid = Argon2::default()
            .verify_password(password.as_bytes(), &hash)
            .is_ok();
        let generation = state.generation;
        drop(state);
        if valid {
            let mut state = self.state.write().unwrap();
            if state.generation != generation {
                return false;
            }
            state.bearer_digest = Some(candidate);
        }
        valid
    }
    pub fn generation(&self) -> u64 {
        self.state.read().unwrap().generation
    }
    pub fn check_attempt(&self, ip: IpAddr) -> bool {
        let now = Instant::now();
        let mut failures = self.failures.lock().unwrap();
        failures.retain(|_, (at, _)| now.duration_since(*at) < LOGIN_WINDOW);
        failures.get(&ip).is_none_or(|(_, count)| *count < 5)
    }
    pub fn record_attempt(&self, ip: IpAddr, valid: bool) {
        let mut failures = self.failures.lock().unwrap();
        if valid {
            failures.remove(&ip);
            return;
        }
        let now = Instant::now();
        failures.retain(|_, (at, _)| now.duration_since(*at) < LOGIN_WINDOW);
        if failures.len() >= 1024
            && !failures.contains_key(&ip)
            && let Some(oldest) = failures
                .iter()
                .min_by_key(|(_, (at, _))| *at)
                .map(|(ip, _)| *ip)
        {
            failures.remove(&oldest);
        }
        let item = failures.entry(ip).or_insert((now, 0));
        item.1 = item.1.saturating_add(1);
    }
    pub fn change_password(&self, current: &str, new: &str) -> Result<Value> {
        validate_password(new)?;
        ensure!(current.len() <= 4096, "Current password is incorrect");
        let mut state = self.state.write().unwrap();
        let hash = PasswordHash::new(&state.credentials.password_hash)
            .expect("validated access password hash");
        ensure!(
            Argon2::default()
                .verify_password(current.as_bytes(), &hash)
                .is_ok(),
            "Current password is incorrect"
        );
        let updated = credentials(&state.credentials.account, new)?;
        write(&self.path, &updated, false)?;
        state.credentials = updated;
        state.generation += 1;
        state.bearer_digest = Some(digest(new));
        self.failures.lock().unwrap().clear();
        Ok(
            json!({"account":state.credentials.account,"defaultPassword":new == "admin","changed":true}),
        )
    }
}
fn digest(value: &str) -> [u8; 32] {
    Sha256::digest(value.as_bytes()).into()
}
fn validate_password(value: &str) -> Result<()> {
    ensure!(
        !value.trim().is_empty() && value.len() <= 4096 && !value.chars().any(char::is_control),
        "Password must contain 1–4096 bytes without control characters"
    );
    Ok(())
}
fn credentials(account: &str, password: &str) -> Result<Credentials> {
    let password_hash = Argon2::default()
        .hash_password(password.as_bytes(), &SaltString::generate(&mut OsRng))
        .map_err(|_| anyhow::anyhow!("Password hashing failed"))?
        .to_string();
    Ok(Credentials {
        account: account.into(),
        password_hash,
        default_password: password == "admin",
    })
}
fn regular(path: &Path) -> Result<()> {
    let metadata = std::fs::symlink_metadata(path)?;
    ensure!(
        metadata.is_file() && !metadata.file_type().is_symlink(),
        "Access configuration must be a regular file"
    );
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        ensure!(
            metadata.file_attributes() & 0x400 == 0,
            "Access configuration must not be a reparse point"
        );
    }
    Ok(())
}
fn write(path: &Path, credentials: &Credentials, initial: bool) -> Result<()> {
    let directory = path.parent().context("Access directory is unavailable")?;
    let mut file = tempfile::NamedTempFile::new_in(directory)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        file.as_file()
            .set_permissions(std::fs::Permissions::from_mode(0o600))?;
    }
    file.write_all(&serde_json::to_vec(credentials)?)?;
    file.as_file().sync_all()?;
    if initial {
        match file.persist_noclobber(path) {
            Ok(_) => {}
            Err(error) if error.error.kind() == std::io::ErrorKind::AlreadyExists => regular(path)?,
            Err(error) => return Err(error.error.into()),
        }
    } else {
        regular(path)?;
        file.persist(path).map_err(|error| error.error)?;
    }
    #[cfg(unix)]
    std::fs::File::open(directory)?.sync_all()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn failed_attempts_clear_expire_and_keep_other_sources_available() -> Result<()> {
        let dir = tempfile::tempdir()?;
        Access::initialize(dir.path(), "admin", "test-password")?;
        let access = Access::open(dir.path())?;
        let ip = "192.0.2.1".parse()?;
        for _ in 0..4 {
            access.record_attempt(ip, false);
        }
        access.record_attempt(ip, true);
        for _ in 0..5 {
            assert!(access.check_attempt(ip));
            access.record_attempt(ip, false);
        }
        assert!(!access.check_attempt(ip));
        assert!(access.check_attempt("192.0.2.2".parse()?));
        access.failures.lock().unwrap().get_mut(&ip).unwrap().0 = Instant::now() - LOGIN_WINDOW;
        assert!(access.check_attempt(ip));
        for number in 0..1025_u128 {
            access.record_attempt(IpAddr::V6(number.into()), false);
        }
        assert_eq!(access.failures.lock().unwrap().len(), 1024);
        assert!(access.check_attempt("192.0.2.3".parse()?));
        Ok(())
    }
    #[test]
    fn defaults_change_and_reopen_preserve_shared_password() -> Result<()> {
        let dir = tempfile::tempdir()?;
        Access::initialize(dir.path(), "admin", "admin")?;
        let access = Access::open(dir.path())?;
        assert!(access.verify("admin", "admin"));
        assert!(access.verify_bearer("admin"));
        assert_eq!(access.status()["defaultPassword"], true);
        let generation = access.generation();
        assert!(access.change_password("wrong", "new-password").is_err());
        assert_eq!(access.generation(), generation);
        access.change_password("admin", "new-password")?;
        assert_ne!(access.generation(), generation);
        assert!(!access.verify_bearer("admin"));
        assert!(access.verify("admin", "new-password"));
        assert!(access.verify_bearer("new-password"));
        Access::initialize(dir.path(), "different", "environment-overwrite")?;
        let reopened = Access::open(dir.path())?;
        assert!(reopened.verify("admin", "new-password"));
        assert!(!reopened.verify("different", "environment-overwrite"));
        assert_eq!(reopened.status()["defaultPassword"], false);
        let disk = std::fs::read_to_string(dir.path().join("access.json"))?;
        assert!(!disk.contains("new-password"));
        assert!(!reopened.status().to_string().contains("hash"));
        Ok(())
    }
    #[test]
    fn rejects_invalid_initial_or_updated_credentials() -> Result<()> {
        let dir = tempfile::tempdir()?;
        assert!(Access::initialize(dir.path(), "", "admin").is_err());
        assert!(Access::initialize(dir.path(), "admin", "").is_err());
        Access::initialize(dir.path(), "admin", "admin")?;
        let access = Access::open(dir.path())?;
        assert!(access.change_password("admin", "").is_err());
        assert!(access.change_password("admin", "bad\npassword").is_err());
        assert!(access.verify("admin", "admin"));
        Ok(())
    }
}

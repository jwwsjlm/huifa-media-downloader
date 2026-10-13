use anyhow::{Result, ensure};
use argon2::{Argon2, PasswordHash, PasswordHasher, PasswordVerifier, password_hash::SaltString};
use axum::http::{HeaderMap, StatusCode, header};
use rand_core::{OsRng, RngCore};
use sha2::{Digest, Sha256};
use std::{
    collections::HashMap,
    net::IpAddr,
    sync::Mutex,
    time::{Duration, Instant},
};
use subtle::ConstantTimeEq;

const SESSION_LIFETIME: Duration = Duration::from_secs(12 * 3600);
const LOGIN_WINDOW: Duration = Duration::from_secs(15 * 60);

pub struct Auth {
    account: [u8; 32],
    password_hash: String,
    public_origin: Option<String>,
    sessions: Mutex<HashMap<[u8; 32], Session>>,
    attempts: Mutex<HashMap<IpAddr, (Instant, u32)>>,
    global: Mutex<(Instant, u32)>,
    pub password_slots: tokio::sync::Semaphore,
}

#[derive(Clone)]
pub struct Session {
    pub csrf: String,
    expires: Instant,
}

impl Auth {
    pub fn new(account: &str, password: &str, public_origin: Option<String>) -> Result<Self> {
        ensure!(
            !account.trim().is_empty() && account.len() <= 128,
            "HUIFA_ACCOUNT must be set and at most 128 bytes"
        );
        ensure!(
            !password.trim().is_empty() && password.len() <= 4096,
            "HUIFA_PASSWORD must be set and at most 4096 bytes"
        );
        let password_hash = Argon2::default()
            .hash_password(password.as_bytes(), &SaltString::generate(&mut OsRng))
            .map_err(|_| anyhow::anyhow!("password hashing failed"))?
            .to_string();
        let public_origin = public_origin
            .map(|origin| normalized_origin(&origin))
            .transpose()?;
        Ok(Self {
            account: digest(account.as_bytes()),
            password_hash,
            public_origin,
            sessions: Mutex::new(HashMap::new()),
            attempts: Mutex::new(HashMap::new()),
            global: Mutex::new((Instant::now(), 0)),
            password_slots: tokio::sync::Semaphore::new(2),
        })
    }

    pub fn same_origin(&self, headers: &HeaderMap) -> bool {
        let Some(origin) = headers.get(header::ORIGIN).and_then(|v| v.to_str().ok()) else {
            return false;
        };
        let Ok(origin) = normalized_origin(origin) else {
            return false;
        };
        if let Some(expected) = &self.public_origin {
            return origin == *expected;
        }
        let Some(host) = headers.get(header::HOST).and_then(|v| v.to_str().ok()) else {
            return false;
        };
        let Ok(expected) = normalized_origin(&format!("http://{host}")) else {
            return false;
        };
        origin == expected
    }

    pub fn secure_cookie(&self) -> bool {
        self.public_origin
            .as_ref()
            .is_some_and(|v| v.starts_with("https://"))
    }

    pub fn check_attempt(&self, ip: IpAddr) -> bool {
        let now = Instant::now();
        let mut global = self.global.lock().unwrap();
        if now.duration_since(global.0) >= Duration::from_secs(60) {
            *global = (now, 0);
        }
        if global.1 >= 30 {
            return false;
        }
        let mut attempts = self.attempts.lock().unwrap();
        attempts.retain(|_, (at, _)| now.duration_since(*at) < LOGIN_WINDOW);
        if attempts.len() >= 1024 && !attempts.contains_key(&ip) {
            return false;
        }
        let item = attempts.entry(ip).or_insert((now, 0));
        if item.1 >= 5 {
            return false;
        }
        item.1 += 1;
        global.1 += 1;
        true
    }

    pub fn verify(&self, account: &str, password: &str) -> bool {
        let hash = PasswordHash::new(&self.password_hash).expect("validated Argon2 hash");
        let valid_password = Argon2::default()
            .verify_password(password.as_bytes(), &hash)
            .is_ok();
        let valid_account: bool = self.account.ct_eq(&digest(account.as_bytes())).into();
        valid_password & valid_account
    }

    pub fn create_session(&self) -> (String, Session) {
        let token = random_token();
        let session = Session {
            csrf: random_token(),
            expires: Instant::now() + SESSION_LIFETIME,
        };
        let mut sessions = self.sessions.lock().unwrap();
        sessions.retain(|_, item| item.expires > Instant::now());
        if sessions.len() >= 64
            && let Some(oldest) = sessions
                .iter()
                .min_by_key(|(_, s)| s.expires)
                .map(|(k, _)| *k)
        {
            sessions.remove(&oldest);
        }
        sessions.insert(digest(token.as_bytes()), session.clone());
        (token, session)
    }

    pub fn session(&self, headers: &HeaderMap) -> Option<Session> {
        let token = cookie(headers)?;
        let mut sessions = self.sessions.lock().unwrap();
        sessions.retain(|_, s| s.expires > Instant::now());
        sessions.get(&digest(token.as_bytes())).cloned()
    }

    pub fn authorize(&self, headers: &HeaderMap, mutation: bool) -> Result<Session, StatusCode> {
        let session = self.session(headers).ok_or(StatusCode::UNAUTHORIZED)?;
        if mutation {
            if !self.same_origin(headers) {
                return Err(StatusCode::FORBIDDEN);
            }
            let csrf = headers
                .get("x-csrf-token")
                .map(|v| v.as_bytes())
                .unwrap_or_default();
            if !bool::from(digest(csrf).ct_eq(&digest(session.csrf.as_bytes()))) {
                return Err(StatusCode::FORBIDDEN);
            }
        } else if headers.contains_key(header::ORIGIN) && !self.same_origin(headers) {
            return Err(StatusCode::FORBIDDEN);
        }
        Ok(session)
    }

    pub fn logout(&self, headers: &HeaderMap) {
        if let Some(token) = cookie(headers) {
            self.sessions
                .lock()
                .unwrap()
                .remove(&digest(token.as_bytes()));
        }
    }

    pub fn session_cookie(&self, token: &str) -> String {
        format!(
            "huifa_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=43200{}",
            if self.secure_cookie() { "; Secure" } else { "" }
        )
    }
}

fn normalized_origin(origin: &str) -> Result<String> {
    let url = reqwest::Url::parse(origin)?;
    ensure!(
        matches!(url.scheme(), "http" | "https")
            && url.host_str().is_some()
            && url.username().is_empty()
            && url.password().is_none()
            && url.path() == "/"
            && url.query().is_none()
            && url.fragment().is_none(),
        "invalid public origin"
    );
    Ok(url.origin().ascii_serialization())
}

fn cookie(headers: &HeaderMap) -> Option<&str> {
    let value = headers.get(header::COOKIE)?.to_str().ok()?;
    let mut found = value
        .split(';')
        .filter_map(|p| p.trim().strip_prefix("huifa_session="));
    let token = found.next()?;
    if found.next().is_some() || token.len() != 64 || !token.bytes().all(|b| b.is_ascii_hexdigit())
    {
        return None;
    }
    Some(token)
}

fn digest(value: &[u8]) -> [u8; 32] {
    Sha256::digest(value).into()
}

pub fn random_token() -> String {
    let mut bytes = [0u8; 32];
    OsRng.fill_bytes(&mut bytes);
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    fn headers(origin: &str) -> HeaderMap {
        let mut h = HeaderMap::new();
        h.insert(header::HOST, "localhost:19491".parse().unwrap());
        h.insert(header::ORIGIN, origin.parse().unwrap());
        h
    }
    #[test]
    fn session_origin_and_limits() -> Result<()> {
        let auth = Auth::new("admin", "test-password", None)?;
        assert!(auth.verify("admin", "test-password"));
        assert!(!auth.verify("wrong", "test-password"));
        assert!(!auth.verify("admin", "wrong"));
        assert!(!auth.same_origin(&headers("http://evil.test")));
        assert!(!auth.same_origin(&HeaderMap::new()));
        assert!(auth.same_origin(&headers("http://localhost:19491")));
        let (token, session) = auth.create_session();
        let mut h = headers("http://localhost:19491");
        h.insert(
            header::COOKIE,
            format!("huifa_session={token}").parse().unwrap(),
        );
        assert!(auth.authorize(&h, false).is_ok());
        assert_eq!(auth.authorize(&h, true).err(), Some(StatusCode::FORBIDDEN));
        h.insert("x-csrf-token", session.csrf.parse().unwrap());
        assert!(auth.authorize(&h, true).is_ok());
        auth.logout(&h);
        assert_eq!(
            auth.authorize(&h, false).err(),
            Some(StatusCode::UNAUTHORIZED)
        );
        let ip = "127.0.0.1".parse().unwrap();
        for _ in 0..5 {
            assert!(auth.check_attempt(ip));
        }
        assert!(!auth.check_attempt(ip));
        assert!(Auth::new("admin", "", None).is_err());
        Ok(())
    }
}

use anyhow::{Result, ensure};
use axum::http::{HeaderMap, StatusCode, header};
use rand_core::{OsRng, RngCore};
use sha2::{Digest, Sha256};
use std::{
    collections::HashMap,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};
use subtle::ConstantTimeEq;

const SESSION_LIFETIME: Duration = Duration::from_secs(12 * 3600);

pub struct Auth {
    app: Arc<huifa_core::App>,
    public_origin: Option<String>,
    sessions: Mutex<HashMap<[u8; 32], Session>>,
    pub password_slots: Arc<tokio::sync::Semaphore>,
}

#[derive(Clone)]
pub struct Session {
    pub csrf: String,
    expires: Instant,
    generation: u64,
}

impl Auth {
    pub fn new(app: Arc<huifa_core::App>, public_origin: Option<String>) -> Result<Self> {
        let public_origin = public_origin
            .map(|origin| normalized_origin(&origin))
            .transpose()?;
        Ok(Self {
            app,
            public_origin,
            sessions: Mutex::new(HashMap::new()),
            password_slots: Arc::new(tokio::sync::Semaphore::new(2)),
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

    pub fn create_session(&self, generation: u64) -> (String, Session) {
        let token = random_token();
        let session = Session {
            csrf: random_token(),
            expires: Instant::now() + SESSION_LIFETIME,
            generation,
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
        sessions.retain(|_, s| {
            s.expires > Instant::now() && s.generation == self.app.access.generation()
        });
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
    #[tokio::test]
    async fn session_origin_and_limits() -> Result<()> {
        let directory = tempfile::tempdir()?;
        huifa_core::access::Access::initialize(directory.path(), "admin", "test-password")?;
        let app = huifa_core::App::open(huifa_core::AppConfig {
            data_dir: directory.path().into(),
            download_dir: directory.path().join("downloads"),
            tool_dir: None,
        })
        .await?;
        let auth = Auth::new(app.clone(), None)?;
        assert!(app.access.verify("admin", "test-password"));
        assert!(!app.access.verify("wrong", "test-password"));
        assert!(!app.access.verify("admin", "wrong"));
        assert!(!auth.same_origin(&headers("http://evil.test")));
        assert!(!auth.same_origin(&HeaderMap::new()));
        assert!(auth.same_origin(&headers("http://localhost:19491")));
        let (token, session) = auth.create_session(app.access.generation());
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
        let (token, _) = auth.create_session(app.access.generation());
        h.insert(
            header::COOKIE,
            format!("huifa_session={token}").parse().unwrap(),
        );
        app.access
            .change_password("test-password", "changed-password")?;
        assert_eq!(
            auth.authorize(&h, false).err(),
            Some(StatusCode::UNAUTHORIZED)
        );
        assert!(app.access.verify("admin", "changed-password"));
        app.shutdown().await?;
        Ok(())
    }
}

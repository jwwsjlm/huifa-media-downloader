use anyhow::{Context, Result, ensure};
use serde_json::Value;
use std::{
    collections::VecDeque,
    time::{Duration, Instant},
};

const SOURCE_TTL: Duration = Duration::from_secs(30 * 60);
const COMMENT_TTL: Duration = Duration::from_secs(5 * 60);

#[derive(Default)]
pub(super) struct Cache {
    sources: VecDeque<(String, String, Instant)>,
    comments: VecDeque<(String, Value, usize, Instant)>,
}

impl Cache {
    fn prune(&mut self) {
        self.sources
            .retain(|(_, _, time)| time.elapsed() < SOURCE_TTL);
        self.comments
            .retain(|(_, _, _, time)| time.elapsed() < COMMENT_TTL);
    }
    pub(super) fn remember(&mut self, url: &str) -> String {
        self.prune();
        if let Some((id, _, time)) = self.sources.iter_mut().find(|(_, value, _)| value == url) {
            *time = Instant::now();
            return id.clone();
        }
        while self.sources.len() >= 2048 {
            self.sources.pop_front();
        }
        let id = uuid::Uuid::new_v4().to_string();
        self.sources
            .push_back((id.clone(), url.into(), Instant::now()));
        id
    }
    pub(super) fn resolve(&mut self, id: &str) -> Result<String> {
        self.prune();
        self.sources.iter().find(|(key, _, _)| key == id).map(|(_, url, _)| url.clone())
            .context("Source reference expired or is unknown; query the collection again or provide its webpage URL")
    }
    pub(super) fn comments(&mut self, key: &str) -> Option<Value> {
        self.prune();
        self.comments
            .iter()
            .find(|(id, _, _, _)| id == key)
            .map(|(_, value, _, _)| value.clone())
    }
    pub(super) fn save_comments(&mut self, key: String, value: Value) -> Result<()> {
        let size = serde_json::to_vec(&value)?.len();
        ensure!(
            size <= 32 * 1024 * 1024,
            "Comment result exceeds its byte limit"
        );
        self.prune();
        self.comments.retain(|(id, _, _, _)| id != &key);
        while self.comments.len() >= 4
            || self
                .comments
                .iter()
                .map(|(_, _, size, _)| *size)
                .sum::<usize>()
                + size
                > 64 * 1024 * 1024
        {
            self.comments.pop_front();
        }
        self.comments.push_back((key, value, size, Instant::now()));
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn references_are_private_bounded_and_expire() -> Result<()> {
        let mut cache = Cache::default();
        let reference = cache.remember("https://example.test/watch?token=private&id=3");
        assert!(!reference.contains("private"));
        assert_eq!(
            cache.resolve(&reference)?,
            "https://example.test/watch?token=private&id=3"
        );
        cache.sources[0].2 = Instant::now() - SOURCE_TTL;
        assert!(cache.resolve(&reference).is_err());
        let first = cache.remember("https://example.test/old");
        for index in 0..2048 {
            cache.remember(&format!("https://example.test/{index}"));
        }
        assert!(cache.resolve(&first).is_err());
        assert_eq!(cache.sources.len(), 2048);
        Ok(())
    }

    #[test]
    fn comment_cache_obeys_time_entry_and_byte_limits() -> Result<()> {
        let mut cache = Cache::default();
        for index in 0..5 {
            cache.save_comments(index.to_string(), json!({"comments":[]}))?;
        }
        assert_eq!(cache.comments.len(), 4);
        assert!(cache.comments("0").is_none());
        cache.comments[0].3 = Instant::now() - COMMENT_TTL;
        assert!(cache.comments("1").is_none());
        cache.save_comments("large".into(), json!("x".repeat(24 * 1024 * 1024)))?;
        cache.save_comments("large2".into(), json!("x".repeat(24 * 1024 * 1024)))?;
        cache.save_comments("large3".into(), json!("x".repeat(24 * 1024 * 1024)))?;
        assert!(cache.comments("large").is_none());
        assert_eq!(cache.comments.len(), 2);
        assert!(
            cache
                .save_comments("too-large".into(), json!("x".repeat(32 * 1024 * 1024)))
                .is_err()
        );
        Ok(())
    }
}

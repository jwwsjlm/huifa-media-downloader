use crate::Event;
use serde_json::{Value, json};
use std::{
    collections::VecDeque,
    sync::atomic::{AtomicU64, Ordering},
    time::{SystemTime, UNIX_EPOCH},
};

const MAX_EVENTS: usize = 2048;
const MAX_BYTES: usize = 8 * 1024 * 1024;

pub(crate) struct History {
    pub(crate) revision: u64,
    floor: u64,
    bytes: usize,
    events: VecDeque<(Event, usize)>,
}

impl Default for History {
    fn default() -> Self {
        // Microseconds distinguish restarts while remaining exactly representable in JavaScript.
        static LAST: AtomicU64 = AtomicU64::new(0);
        let clock = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_micros() as u64;
        LAST.fetch_max(clock, Ordering::SeqCst);
        let revision = LAST.fetch_add(1, Ordering::SeqCst) + 1;
        Self {
            revision,
            floor: revision,
            bytes: 0,
            events: VecDeque::new(),
        }
    }
}

impl History {
    #[cfg(all(test, windows))]
    pub(crate) fn retained(&self) -> (usize, usize) {
        (self.events.len(), self.bytes)
    }

    pub(crate) fn push(&mut self, kind: &str, data: Value) -> Event {
        self.revision += 1;
        let event = Event {
            kind: kind.into(),
            data,
            revision: self.revision,
        };
        let bytes = serde_json::to_vec(&event).map_or(MAX_BYTES + 1, |value| value.len());
        self.bytes += bytes;
        self.events.push_back((event.clone(), bytes));
        while self.events.len() > MAX_EVENTS || self.bytes > MAX_BYTES {
            if let Some((removed, bytes)) = self.events.pop_front() {
                self.bytes -= bytes;
                self.floor = removed.revision;
            }
        }
        event
    }

    pub(crate) fn sync(&self, since: u64) -> Value {
        let reset = since < self.floor || since > self.revision;
        let events: Vec<_> = if reset {
            vec![]
        } else {
            self.events
                .iter()
                .filter(|(event, _)| event.revision > since)
                .map(|(event, _)| event)
                .collect()
        };
        json!({"revision":self.revision,"reset":reset,"events":events})
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn history_is_ordered_bounded_and_detects_restarts() {
        let mut history = History::default();
        let initial = history.revision;
        assert!(initial < 9_007_199_254_740_991);
        assert_eq!(history.sync(initial)["events"], json!([]));
        history.push("task.remove", json!({"id":"one"}));
        history.push("settings.updated", json!({"analysisThreads":4}));
        let reply = history.sync(initial);
        assert_eq!(reply["reset"], false);
        assert_eq!(reply["events"][0]["revision"], initial + 1);
        assert_eq!(reply["events"][1]["revision"], initial + 2);
        assert_eq!(
            history.sync(initial + 1)["events"]
                .as_array()
                .unwrap()
                .len(),
            1
        );
        assert_eq!(history.sync(history.revision + 1)["reset"], true);
        assert_eq!(History::default().sync(initial)["reset"], true);
        for _ in 0..MAX_EVENTS {
            history.push("task.upsert", Value::Null);
        }
        assert_eq!(history.sync(initial)["reset"], true);
        assert_eq!(history.events.len(), MAX_EVENTS);
        history.push("large", json!("x".repeat(MAX_BYTES)));
        assert!(history.events.is_empty());
        assert_eq!(history.bytes, 0);
        assert_eq!(history.sync(history.revision)["reset"], false);
    }

    #[tokio::test]
    async fn snapshot_cursor_replays_public_mutations_and_rejects_invalid_cursors()
    -> anyhow::Result<()> {
        let root = tempfile::tempdir()?;
        let config = crate::AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("downloads"),
            tool_dir: None,
        };
        let app = crate::App::open(config.clone()).await?;
        let snapshot = app
            .command("snapshot", json!({"includeTools":false}))
            .await?;
        let since = snapshot["revision"].as_u64().unwrap();
        let mut task = crate::tests::paused_task(Default::default());
        app.db.insert_task(&task)?;
        app.save_task(&task)?;
        task.stage("completed", "completed", Some(100.0));
        app.save_task(&task)?;
        app.command("settings.save", json!({"settings":{"analysisThreads":4}}))
            .await?;
        app.command("tasks.delete", json!({"id":task.id})).await?;
        let sync = app.command("state.sync", json!({"since":since})).await?;
        assert_eq!(sync["reset"], false);
        let events = sync["events"].as_array().unwrap();
        assert_eq!(events[0]["kind"], "task.upsert");
        assert_eq!(events[1]["data"]["status"], "completed");
        assert_eq!(events.last().unwrap()["kind"], "task.remove");
        assert!(
            events
                .iter()
                .enumerate()
                .all(|(index, event)| event["revision"] == since + index as u64 + 1)
        );
        for invalid in [Value::Null, json!(-1), json!(1.5), json!("1")] {
            assert!(
                app.command("state.sync", json!({"since":invalid}))
                    .await
                    .is_err()
            );
        }
        app.shutdown().await?;
        drop(app);
        let app = crate::App::open(config).await?;
        assert_eq!(
            app.command("state.sync", json!({"since":since})).await?["reset"],
            true
        );
        app.shutdown().await?;
        Ok(())
    }
}

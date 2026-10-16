use super::*;
use std::{path::PathBuf, time::SystemTime};

#[derive(Clone)]
pub(super) struct Engine {
    path: PathBuf,
    modified: Option<SystemTime>,
    size: u64,
    pub(super) version: String,
    pub(super) extractors: Vec<Value>,
    pub(super) providers: Vec<Value>,
}

impl App {
    pub(super) async fn research_engine(&self, cancel: &CancellationToken) -> Result<Engine> {
        let path = self.tool("yt-dlp")?;
        let metadata = std::fs::metadata(&path)?;
        let modified = metadata.modified().ok();
        if let Some(engine) = self
            .research_state
            .engine
            .lock()
            .unwrap()
            .as_ref()
            .filter(|engine| {
                engine.path == path && engine.modified == modified && engine.size == metadata.len()
            })
        {
            return Ok(engine.clone());
        }
        let output = process::run_limited_text(
            &path,
            &[
                "--ignore-config".into(),
                "--encoding".into(),
                "utf-8".into(),
                "--version".into(),
            ],
            cancel,
            64 * 1024,
            Duration::from_secs(20),
        )
        .await?;
        let version = output.stdout.trim().to_owned();
        let output = process::run_limited_text(
            &path,
            &[
                "--ignore-config".into(),
                "--encoding".into(),
                "utf-8".into(),
                "--list-extractors".into(),
            ],
            cancel,
            4 * 1024 * 1024,
            Duration::from_secs(30),
        )
        .await?;
        let mut extractors = parse_extractors(&output.stdout);
        let output = process::run_limited_text(
            &path,
            &[
                "--ignore-config".into(),
                "--encoding".into(),
                "utf-8".into(),
                "--extractor-descriptions".into(),
            ],
            cancel,
            4 * 1024 * 1024,
            Duration::from_secs(30),
        )
        .await?;
        let providers = parse_providers(&output.stdout);
        for line in output.stdout.lines() {
            if let Some((id, description)) = line.split_once(": ")
                && let Some(extractor) = extractors
                    .iter_mut()
                    .find(|extractor| text(extractor, "id") == id)
            {
                extractor["description"] =
                    json!(description.split(" (e.g.").next().unwrap_or(description));
            }
        }
        let engine = Engine {
            path,
            modified,
            size: metadata.len(),
            version,
            extractors,
            providers,
        };
        *self.research_state.engine.lock().unwrap() = Some(engine.clone());
        Ok(engine)
    }
}

fn parse_extractors(source: &str) -> Vec<Value> {
    source
        .lines()
        .filter(|line| !line.starts_with(char::is_whitespace) && !line.is_empty())
        .map(|line| {
            let (name, broken) = line
                .strip_suffix(" (CURRENTLY BROKEN)")
                .map_or((line, false), |name| (name, true));
            json!({"id":name,"name":name,"knownBroken":broken})
        })
        .collect()
}

fn parse_providers(source: &str) -> Vec<Value> {
    source.lines().filter_map(|line| {
        let (id, description) = line.split_once(": ")?;
        let prefix = description.split('"').find(|part| part.ends_with(':') && part[..part.len()-1].chars().all(|c| c.is_ascii_alphanumeric() || c == '_'))?.trim_end_matches(':');
        if prefix.is_empty() { return None; }
        Some(json!({"id":id,"name":id,"searchPrefix":prefix,"description":description.split(" (e.g.").next().unwrap_or(description)}))
    }).collect()
}

pub(super) fn search_provider<'a>(providers: &'a [Value], requested: &str) -> Result<&'a Value> {
    let prefix = match requested {
        "" | "youtube" => "ytsearch",
        "bilibili" => "bilisearch",
        "soundcloud" => "scsearch",
        "niconico" => "nicosearch",
        value => value,
    };
    providers
        .iter()
        .find(|provider| {
            text(provider, "id") == requested || text(provider, "searchPrefix") == prefix
        })
        .context("Search provider is unavailable in the current yt-dlp; call list_search_providers")
}

pub(super) fn search_home(provider: &Value) -> &'static str {
    match text(provider, "searchPrefix") {
        "ytsearch" | "ytsearchdate" => "https://www.youtube.com/",
        "bilisearch" => "https://www.bilibili.com/",
        "scsearch" => "https://soundcloud.com/",
        "nicosearch" | "nicosearchdate" => "https://www.nicovideo.jp/",
        _ => "",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn engine_descriptions_discover_searches_without_random_examples() -> Result<()> {
        let providers = parse_providers(
            "youtube:search: YouTube search; \"ytsearch:\" prefix (e.g. \"ytsearch10:cute kittens\")\nBiliBili:search: Bilibili search; \"bilisearch:\" prefix (e.g. \"bilisearch5:cats\")\ngeneric: Generic downloader\n",
        );
        assert_eq!(providers.len(), 2);
        assert_eq!(
            text(search_provider(&providers, "bilibili")?, "searchPrefix"),
            "bilisearch"
        );
        assert_eq!(
            text(search_provider(&providers, "")?, "id"),
            "youtube:search"
        );
        assert!(search_provider(&providers, "unknown").is_err());
        assert!(!json!(providers).to_string().contains("cute kittens"));
        assert_eq!(
            parse_extractors("youtube\nbroken (CURRENTLY BROKEN)\ngeneric\n")[1]["knownBroken"],
            true
        );
        Ok(())
    }
}

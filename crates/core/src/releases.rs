use anyhow::{Result, ensure};
use serde_json::{Value, json};
use std::time::Duration;

pub(crate) async fn status() -> Result<Value> {
    let response = reqwest::Client::builder()
        .timeout(Duration::from_secs(12))
        .build()?
        .get("https://api.github.com/repos/jwwsjlm/huifa-media-downloader/releases/latest")
        .header("User-Agent", "HuifaDownloader/0.3.0")
        .header("Accept", "application/vnd.github+json")
        .send()
        .await?
        .error_for_status()?;
    ensure!(
        response
            .content_length()
            .is_none_or(|size| size <= 1024 * 1024),
        "Release response is too large"
    );
    let mut response = response;
    let mut bytes = Vec::new();
    while let Some(chunk) = response.chunk().await? {
        ensure!(
            bytes.len() + chunk.len() <= 1024 * 1024,
            "Release response is too large"
        );
        bytes.extend_from_slice(&chunk);
    }
    parse(&serde_json::from_slice(&bytes)?)
}

fn parse(value: &Value) -> Result<Value> {
    let tag = value["tag_name"].as_str().unwrap_or("");
    let latest = semver::Version::parse(tag.trim_start_matches('v'))?;
    ensure!(
        value["draft"] != true && value["prerelease"] != true && latest.pre.is_empty(),
        "Release is not a stable version"
    );
    let url = value["html_url"].as_str().unwrap_or("");
    ensure!(valid_url(url), "Invalid release page");
    let current = semver::Version::parse(env!("CARGO_PKG_VERSION"))?;
    Ok(
        json!({"currentVersion":current.to_string(),"latestVersion":latest.to_string(),"updateAvailable":latest>current,"url":url,"publishedAt":value["published_at"]}),
    )
}

pub fn valid_url(value: &str) -> bool {
    url::Url::parse(value).is_ok_and(|url| {
        url.scheme() == "https"
            && url.host_str() == Some("github.com")
            && url.port().is_none()
            && url.username().is_empty()
            && url.password().is_none()
            && url
                .path()
                .starts_with("/jwwsjlm/huifa-media-downloader/releases/")
            && url.query().is_none()
            && url.fragment().is_none()
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn release_comparison_uses_semantic_versions_and_trusted_pages() {
        let release = |tag: &str| json!({"tag_name":tag,"html_url":"https://github.com/jwwsjlm/huifa-media-downloader/releases/tag/v0.4.0"});
        assert_eq!(parse(&release("v0.3.0")).unwrap()["updateAvailable"], false);
        assert_eq!(parse(&release("v0.10.0")).unwrap()["updateAvailable"], true);
        assert!(parse(&release("v0.4.0-beta.1")).is_err());
        assert!(!valid_url("https://github.com/other/repo/releases/tag/v1"));
        assert!(!valid_url(
            "https://github.com.evil.test/jwwsjlm/huifa-media-downloader/releases/latest"
        ));
    }
}

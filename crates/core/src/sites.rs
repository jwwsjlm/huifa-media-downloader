use crate::{App, DownloadOptions, download, process};
use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};

fn normalize(value: &str) -> Result<url::Url> {
    let value = value.trim();
    ensure!(
        !value.is_empty() && !value.chars().any(char::is_whitespace),
        "Enter a website domain or HTTP / HTTPS URL"
    );
    let value = if value.contains("://") {
        value.to_string()
    } else {
        format!("https://{value}")
    };
    crate::model::validate_url(&value)?;
    Ok(url::Url::parse(&value)?)
}

fn supported(output: &str, url: &url::Url) -> Result<Value> {
    let mut current = "";
    let mut matched = false;
    let mut broken = false;
    let mut extractors = Vec::new();
    for line in output.lines() {
        if !line.starts_with(' ') {
            current = line;
            continue;
        }
        if line.trim() != url.as_str() {
            continue;
        }
        matched = true;
        if current == "generic" || current.starts_with("generic:") {
            continue;
        }
        if current.ends_with(" (CURRENTLY BROKEN)") {
            broken = true;
        } else {
            extractors.push(current);
        }
    }
    ensure!(
        matched,
        "The installed yt-dlp did not return website support information; update the component and retry"
    );
    let status = if !extractors.is_empty() {
        "supported"
    } else if broken {
        "unsupported"
    } else if url.path() == "/" && url.query().is_none() {
        "needs_url"
    } else {
        "generic"
    };
    Ok(json!({"url":url.as_str(),"host":url.host_str(),"status":status,"extractors":extractors}))
}

pub(crate) async fn check(app: &App, args: &Value) -> Result<Value> {
    let url = normalize(args["url"].as_str().context("Website URL must be text")?)?;
    let _permit = tokio::select! {
        permit = app.metadata.acquire() => permit?,
        _ = app.shutdown_token.cancelled() => anyhow::bail!("Website support check interrupted"),
    };
    let cookies = download::Cookies::new(app, None)?;
    let mut arguments =
        download::base_args(app, &DownloadOptions::default(), &cookies, None).await?;
    // Matching the installed extractor patterns performs no requests to the website.
    arguments.extend(["--list-extractors".into(), "--".into(), url.to_string()]);
    let result = process::run_limited_text(
        &app.tool("yt-dlp")?,
        &arguments,
        &app.shutdown_token,
        2 * 1024 * 1024,
        std::time::Duration::from_secs(15),
    )
    .await?;
    supported(&result.stdout, &url)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn domain_and_url_validation() -> Result<()> {
        assert_eq!(
            normalize(" www.example.com ")?.as_str(),
            "https://www.example.com/"
        );
        for invalid in [
            "",
            "https://a.test/\n--exec",
            "https://user:pass@a.test/",
            "file:///tmp/a",
            "--exec foo",
        ] {
            assert!(normalize(invalid).is_err(), "{invalid}");
        }
        Ok(())
    }

    #[test]
    fn separates_supported_broken_and_generic_without_promising_a_download() -> Result<()> {
        let url = normalize("https://example.test/video/1")?;
        let generic = format!("generic\n  {url}\nunrelated\n");
        assert_eq!(supported(&generic, &url)?["status"], "generic");
        let matched = format!("{generic}example\n  {url}\nother\n  https://different.test/\n");
        assert_eq!(supported(&matched, &url)?["extractors"], json!(["example"]));
        assert_eq!(supported(&matched, &url)?["status"], "supported");
        assert_eq!(
            supported(
                &format!("{generic}example (CURRENTLY BROKEN)\n  {url}\n"),
                &url
            )?["status"],
            "unsupported"
        );
        let domain = normalize("example.test")?;
        assert_eq!(
            supported(&format!("generic\n  {domain}\n"), &domain)?["status"],
            "needs_url"
        );
        assert!(supported("unrecognized output", &url).is_err());
        Ok(())
    }

    #[tokio::test]
    #[ignore = "requires the bundled yt-dlp executable"]
    async fn installed_extractors_report_support_without_network() -> Result<()> {
        let root = tempfile::tempdir()?;
        let app = App::open(crate::AppConfig {
            data_dir: root.path().join("data"),
            download_dir: root.path().join("output"),
            tool_dir: Some(
                std::env::var_os("HUIFA_TEST_TOOLS")
                    .map(std::path::PathBuf::from)
                    .unwrap_or_else(|| {
                        std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                            .join("../../runtime-tools")
                    }),
            ),
        })
        .await?;
        for (url, status) in [
            ("https://www.youtube.com/watch?v=jNQXAC9IVRw", "supported"),
            ("https://www.bilibili.com/video/BV1xx411c7mD", "supported"),
            (
                "https://www.douyin.com/video/1234567890123456789",
                "supported",
            ),
            ("https://example.invalid/video", "generic"),
            ("example.invalid", "needs_url"),
            ("https://example.invalid/video.mp4", "generic"),
        ] {
            assert_eq!(
                check(&app, &json!({"url":url})).await?["status"],
                status,
                "{url}"
            );
        }
        app.shutdown().await?;
        Ok(())
    }
}

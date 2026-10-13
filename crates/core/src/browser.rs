use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};

pub async fn import_browser(app: &crate::App, args: &Value) -> Result<Value> {
    let browser = args["browser"].as_str().context("browser must be text")?;
    ensure!(
        matches!(
            browser,
            "chrome"
                | "edge"
                | "firefox"
                | "brave"
                | "chromium"
                | "safari"
                | "opera"
                | "vivaldi"
                | "whale"
        ),
        "unsupported browser"
    );
    let profile = args
        .get("profile")
        .and_then(Value::as_str)
        .unwrap_or_default();
    ensure!(
        profile.len() <= 128
            && !profile.contains("..")
            && profile
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, ' ' | '.' | '_' | '-')),
        "profile must be a browser profile name"
    );
    let temporary = tempfile::Builder::new()
        .prefix("browser-cookies-")
        .tempdir_in(&app.config.data_dir)?;
    let info = temporary.path().join("empty-playlist.json");
    let cookies = temporary.path().join("cookies.txt");
    std::fs::write(
        &info,
        serde_json::to_vec(
            &json!({"_type":"playlist","entries":[],"id":"cookie-export","title":"Cookie export","extractor":"generic"}),
        )?,
    )?;
    let source = if profile.is_empty() {
        browser.to_owned()
    } else {
        format!("{browser}:{profile}")
    };
    // An empty local playlist makes yt-dlp export its jar without downloading or contacting a website.
    let arguments = vec![
        "--ignore-config".into(),
        "--no-plugin-dirs".into(),
        "--no-cache-dir".into(),
        "--simulate".into(),
        "--skip-download".into(),
        "--no-warnings".into(),
        "--cookies-from-browser".into(),
        source,
        "--cookies".into(),
        cookies.to_string_lossy().into_owned(),
        "--load-info-json".into(),
        info.to_string_lossy().into_owned(),
    ];
    let executable = app.tool("yt-dlp")?;
    crate::process::run(
        &executable,
        &arguments,
        Some(temporary.path()),
        &app.shutdown_token,
        None,
    )
    .await
    .context(
        "browser cookie extraction failed; close the browser or export a Netscape cookie file",
    )?;
    let text = std::fs::read_to_string(&cookies).context("browser did not export a cookie file")?;
    app.credentials.put(
        &format!(
            "{browser}{}",
            if profile.is_empty() {
                String::new()
            } else {
                format!(" ({profile})")
            }
        ),
        &text,
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn rejects_paths_and_unknown_browsers_before_running_tools() -> Result<()> {
        let directory = tempfile::tempdir()?;
        let app = crate::App::open(crate::AppConfig {
            data_dir: directory.path().join("data"),
            download_dir: directory.path().join("downloads"),
            tool_dir: None,
        })
        .await?;
        assert!(
            import_browser(&app, &json!({"browser":"shell"}))
                .await
                .is_err()
        );
        assert!(
            import_browser(&app, &json!({"browser":"chrome","profile":"../../secrets"}))
                .await
                .is_err()
        );
        app.shutdown().await?;
        Ok(())
    }
}

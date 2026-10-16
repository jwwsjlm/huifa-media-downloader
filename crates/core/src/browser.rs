use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};

mod cdp;
pub use cdp::Cdp;

pub fn netscape(cookies: &Value) -> Result<String> {
    let cookies = cookies
        .as_array()
        .context("Browser returned invalid cookies")?;
    ensure!(
        !cookies.is_empty() && cookies.len() <= 10000,
        "No cookies to save, or too many cookies"
    );
    let mut text = String::from("# Netscape HTTP Cookie File\n# Saved by Huifa Downloader Rust\n");
    for cookie in cookies {
        let domain = cookie["domain"].as_str().context("Invalid cookie domain")?;
        let path = cookie["path"].as_str().context("Invalid cookie path")?;
        let name = cookie["name"].as_str().context("Invalid cookie name")?;
        let value = cookie["value"].as_str().context("Invalid cookie value")?;
        ensure!(
            !domain.is_empty()
                && !path.is_empty()
                && !name.is_empty()
                && ![domain, path, name, value]
                    .iter()
                    .any(|s| s.chars().any(char::is_control)),
            "Invalid cookie content"
        );
        let prefix = if cookie["httpOnly"].as_bool().unwrap_or(false) {
            "#HttpOnly_"
        } else {
            ""
        };
        let subdomains = if domain.starts_with('.') {
            "TRUE"
        } else {
            "FALSE"
        };
        let secure = if cookie["secure"].as_bool().unwrap_or(false) {
            "TRUE"
        } else {
            "FALSE"
        };
        let expires = if cookie["session"].as_bool() == Some(true) {
            0
        } else {
            cookie["expires"].as_f64().unwrap_or(0.0).max(0.0) as i64
        };
        text.push_str(&format!(
            "{prefix}{domain}\t{subdomains}\t{path}\t{secure}\t{expires}\t{name}\t{value}\n"
        ));
        ensure!(text.len() <= 2 * 1024 * 1024, "Cookies exceed 2 MiB");
    }
    crate::credentials::validate_cookies(&text)?;
    Ok(text)
}

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
        "--encoding".into(),
        "utf-8".into(),
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
    async fn saved_cookies_are_automatic_for_new_downloads() -> Result<()> {
        let directory = tempfile::tempdir()?;
        let app = crate::App::open(crate::AppConfig {
            data_dir: directory.path().join("data"),
            download_dir: directory.path().join("downloads"),
            tool_dir: None,
        })
        .await?;
        let text =
            "# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tTRUE\t0\tsid\tautomatic-login\n";
        app.credentials.put("Saved browser login", text)?;
        assert_eq!(
            app.credential_snapshot(
                &crate::DownloadOptions::default(),
                "https://www.example.com/video"
            )?
            .as_deref(),
            Some(text)
        );
        app.shutdown().await?;
        Ok(())
    }

    #[tokio::test]
    async fn automatic_login_updates_preserve_settings_and_frozen_identity() -> Result<()> {
        let directory = tempfile::tempdir()?;
        let config = crate::AppConfig {
            data_dir: directory.path().join("data"),
            download_dir: directory.path().join("downloads"),
            tool_dir: None,
        };
        let app = crate::App::open(config.clone()).await?;
        app.command("settings.save", json!({"settings":{"uiLanguage":"zh-CN","downloadOptions":{"cookieId":"","quality":"720p"}}})).await?;
        let text =
            "# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tTRUE\t0\tsid\tfirst-login\n";
        let saved = app
            .sync_login_cookies("embedded", "Browser", Some(text))?
            .unwrap();
        assert_eq!(saved["settings"]["downloadOptions"]["cookieId"], "auto");
        assert_eq!(saved["settings"]["downloadOptions"]["quality"], "720p");
        let frozen = app
            .credential_snapshot(
                &crate::DownloadOptions::default(),
                "https://www.example.com/",
            )?
            .unwrap();
        app.credentials
            .put_secret("task-cookie:existing", &frozen)?;
        app.sync_login_cookies(
            "embedded",
            "Browser",
            Some(&text.replace("first-login", "new-login")),
        )?;
        assert_eq!(
            app.credentials.secret("task-cookie:existing")?.as_deref(),
            Some(text)
        );
        assert!(
            app.credential_snapshot(
                &crate::DownloadOptions::default(),
                "https://www.example.com/"
            )?
            .unwrap()
            .contains("new-login")
        );
        let none = crate::DownloadOptions::parse(json!({"cookieId":""}))?;
        assert!(
            app.credential_snapshot(&none, "https://www.example.com/")?
                .is_some()
        );
        app.shutdown().await?;
        drop(app);
        let reopened = crate::App::open(config).await?;
        assert!(
            reopened
                .credential_snapshot(
                    &crate::DownloadOptions::default(),
                    "https://www.example.com/"
                )?
                .unwrap()
                .contains("new-login")
        );
        reopened.shutdown().await?;
        Ok(())
    }

    #[test]
    fn cookie_export_keeps_scope_flags_and_rejects_invalid_or_large_jars() -> Result<()> {
        let cookies = json!([
            {"domain":".example.com","path":"/private","name":"sid","value":"secret","httpOnly":true,"secure":true,"expires":2000000000.5},
            {"domain":"www.example.com","path":"/","name":"host","value":"session","session":true,"expires":-1}
        ]);
        let text = netscape(&cookies)?;
        assert!(
            text.contains(
                "#HttpOnly_.example.com\tTRUE\t/private\tTRUE\t2000000000\tsid\tsecret\n"
            )
        );
        assert!(text.contains("www.example.com\tFALSE\t/\tFALSE\t0\thost\tsession\n"));
        for value in ["bad\nrow", "bad\trow", "bad\rrow", "bad\0row"] {
            let mut invalid = cookies.clone();
            invalid[0]["value"] = value.into();
            assert!(netscape(&invalid).is_err());
        }
        assert!(netscape(&json!([])).is_err());
        assert!(netscape(&json!(vec![cookies[0].clone(); 10001])).is_err());
        let mut large = cookies.clone();
        large[0]["value"] = "x".repeat(2 * 1024 * 1024).into();
        assert!(netscape(&large).is_err());
        Ok(())
    }

    #[tokio::test]
    async fn login_cookie_selection_is_persistent_and_keeps_settings_and_frozen_identity()
    -> Result<()> {
        let directory = tempfile::tempdir()?;
        let config = crate::AppConfig {
            data_dir: directory.path().join("data"),
            download_dir: directory.path().join("downloads"),
            tool_dir: None,
        };
        let app = crate::App::open(config.clone()).await?;
        app.command("settings.save", json!({"settings":{"uiLanguage":"zh-CN","aiApiKey":"keep-key","downloadOptions":{"quality":"720p","proxy":"http://localhost:8080"}}})).await?;
        let first_text =
            "# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tTRUE\t0\tsid\tfirst-login\n";
        let second_text = first_text.replace("first-login", "second-login");
        let first = app.import_login_cookies("First", first_text)?;
        let first_id = first["cookie"]["id"].as_str().unwrap();
        let options = crate::DownloadOptions::parse(json!({"cookieId":first_id}))?;
        let frozen = app
            .credential_snapshot(&options, "https://www.example.com/video")?
            .unwrap();
        app.credentials.put_secret("task-cookie:frozen", &frozen)?;
        let result = app.import_login_cookies("Second", &second_text)?;
        let settings = &result["settings"];
        assert_eq!(settings["uiLanguage"], "zh-CN");
        assert_eq!(settings["downloadOptions"]["quality"], "720p");
        assert_eq!(settings["downloadOptions"]["cookieId"], "auto");
        assert_eq!(
            app.credentials.secret("aiApiKey")?.as_deref(),
            Some("keep-key")
        );
        assert_eq!(
            app.credentials.secret("downloadProxy")?.as_deref(),
            Some("http://localhost:8080")
        );
        assert_eq!(
            app.credentials.secret("task-cookie:frozen")?.as_deref(),
            Some(first_text)
        );
        assert!(!result.to_string().contains("second-login"));
        assert!(app.import_login_cookies("Invalid", "not cookies").is_err());
        let id = result["cookie"]["id"].as_str().unwrap().to_owned();
        app.shutdown().await?;
        drop(app);
        let reopened = crate::App::open(config).await?;
        assert_eq!(
            reopened.public_settings()?["downloadOptions"]["cookieId"],
            "auto"
        );
        assert_eq!(reopened.credentials.get(&id)?, second_text);
        reopened.shutdown().await?;
        Ok(())
    }
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

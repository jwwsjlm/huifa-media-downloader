use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};
use std::time::Duration;

pub(crate) async fn status(app: &crate::App) -> Result<Value> {
    let proxy = app.download_proxy()?;
    tokio::select! {
        result=status_for(env!("CARGO_PKG_VERSION"), &proxy)=>result,
        _=app.shutdown_token.cancelled()=>anyhow::bail!("Release check interrupted"),
    }
}

fn client(proxy: &str) -> Result<reqwest::Client> {
    let mut builder = reqwest::Client::builder()
        .no_proxy()
        .timeout(Duration::from_secs(12))
        .redirect(reqwest::redirect::Policy::none())
        .user_agent(concat!("HuifaDownloader/", env!("CARGO_PKG_VERSION")));
    if !proxy.is_empty() {
        builder = builder.proxy(reqwest::Proxy::all(proxy)?);
    }
    Ok(builder.build()?)
}

async fn status_for(current: &str, proxy: &str) -> Result<Value> {
    let client = client(proxy)?;
    check(
        &client,
        current,
        "https://api.github.com/repos/jwwsjlm/huifa-media-downloader/releases/latest",
        "https://github.com/jwwsjlm/huifa-media-downloader/releases/latest",
    )
    .await
}

async fn check(client: &reqwest::Client, current: &str, api: &str, page: &str) -> Result<Value> {
    let api_error = match api_status(client, current, api).await {
        Ok(value) => return Ok(value),
        Err(reason) => reason,
    };
    let fallback = async {
        let response = client.get(page).send().await?.error_for_status()?;
        ensure!(
            response.status().is_redirection(),
            "GitHub did not return the latest release redirect"
        );
        let location = response
            .headers()
            .get(reqwest::header::LOCATION)
            .context("GitHub release redirect has no destination")?
            .to_str()?;
        let destination = response.url().join(location)?;
        release_page(destination.as_str(), current)
    };
    fallback.await.with_context(|| {
        format!("GitHub API check failed: {api_error:#}; official release page check also failed")
    })
}

fn release_page(url: &str, current: &str) -> Result<Value> {
    ensure!(valid_url(url), "Invalid release page");
    let parsed = url::Url::parse(url)?;
    let tag = parsed
        .path()
        .strip_prefix("/jwwsjlm/huifa-media-downloader/releases/tag/")
        .filter(|tag| !tag.is_empty() && !tag.contains('/'))
        .context("GitHub did not identify a release version")?;
    parse(&json!({"tag_name":tag,"html_url":url}), current)
}

async fn api_status(client: &reqwest::Client, current: &str, api: &str) -> Result<Value> {
    let response = client
        .get(api)
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
    parse(&serde_json::from_slice(&bytes)?, current)
}

fn parse(value: &Value, current: &str) -> Result<Value> {
    let tag = value["tag_name"].as_str().unwrap_or("");
    let latest = semver::Version::parse(tag.trim_start_matches('v'))?;
    ensure!(
        value["draft"] != true && value["prerelease"] != true && latest.pre.is_empty(),
        "Release is not a stable version"
    );
    let url = value["html_url"].as_str().unwrap_or("");
    ensure!(valid_url(url), "Invalid release page");
    let current = semver::Version::parse(current)?;
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

    #[tokio::test]
    async fn shutdown_cancels_a_pending_release_check() -> Result<()> {
        use tokio::io::AsyncReadExt;
        let directory = tempfile::tempdir()?;
        let app = crate::App::open(crate::AppConfig {
            data_dir: directory.path().join("data"),
            download_dir: directory.path().join("downloads"),
            tool_dir: None,
        })
        .await?;
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
        let proxy = format!("http://{}", listener.local_addr()?);
        app.command(
            "settings.save",
            json!({"settings":{"downloadOptions":{"proxy":proxy}}}),
        )
        .await?;
        let (started, request_started) = tokio::sync::oneshot::channel();
        let fixture = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await?;
            let mut buffer = [0; 8192];
            let size = socket.read(&mut buffer).await?;
            let _ = started.send(String::from_utf8_lossy(&buffer[..size]).to_string());
            std::future::pending::<()>().await;
            drop(socket);
            Ok::<_, anyhow::Error>(())
        });
        let checking_app = app.clone();
        let checking =
            tokio::spawn(
                async move { checking_app.command("app.release_status", json!({})).await },
            );
        let request = tokio::time::timeout(Duration::from_secs(5), request_started).await??;
        assert!(request.starts_with("CONNECT api.github.com:443 HTTP/1.1"));

        let shutdown = tokio::time::timeout(Duration::from_secs(1), app.shutdown()).await;
        if shutdown.is_err() {
            checking.abort();
        }
        let result = checking.await;
        fixture.abort();
        let _ = fixture.await;
        shutdown
            .context("Shutdown waited for the pending release check instead of cancelling it")??;
        assert!(
            result?.is_err(),
            "A cancelled release check must not succeed"
        );
        Ok(())
    }

    #[tokio::test]
    async fn release_check_uses_the_configured_proxy() -> Result<()> {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
        let proxy = format!("http://{}", listener.local_addr()?);
        let fixture = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await?;
            let mut buffer = [0u8; 8192];
            let count = socket.read(&mut buffer).await?;
            let request = String::from_utf8_lossy(&buffer[..count]).to_string();
            let body = r#"{"tag_name":"v99.0.0","html_url":"https://github.com/jwwsjlm/huifa-media-downloader/releases/tag/v99.0.0"}"#;
            socket
                .write_all(
                    format!(
                        "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                        body.len()
                    )
                    .as_bytes(),
                )
                .await?;
            Ok::<_, anyhow::Error>(request)
        });
        let value = check(
            &client(&proxy)?,
            "0.4.2",
            "http://release-proxy.invalid/api",
            "http://release-proxy.invalid/latest",
        )
        .await?;
        assert_eq!(value["latestVersion"], "99.0.0");
        let request = tokio::time::timeout(Duration::from_secs(5), fixture).await???;
        assert!(request.starts_with("GET http://release-proxy.invalid/api HTTP/1.1"));
        Ok(())
    }

    #[tokio::test]
    async fn rate_limited_api_uses_official_release_redirect() -> Result<()> {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
        let base = format!("http://{}", listener.local_addr()?);
        let fixture = tokio::spawn(async move {
            loop {
                let Ok((mut socket, _)) = listener.accept().await else {
                    break;
                };
                tokio::spawn(async move {
                    let mut request = [0; 8192];
                    let size = socket.read(&mut request).await.unwrap_or(0);
                    let latest = request[..size].starts_with(b"GET /latest ");
                    let reply = if latest {
                        "HTTP/1.1 302 Found\r\nLocation: https://github.com/jwwsjlm/huifa-media-downloader/releases/tag/v0.4.1\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                    } else {
                        "HTTP/1.1 403 Forbidden\r\nX-RateLimit-Remaining: 0\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                    };
                    let _ = socket.write_all(reply.as_bytes()).await;
                });
            }
        });
        let client = reqwest::Client::builder()
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .build()?;
        let result = check(
            &client,
            "0.4.0",
            &format!("{base}/api"),
            &format!("{base}/latest"),
        )
        .await;
        fixture.abort();
        let result = result?;
        assert_eq!(result["latestVersion"], "0.4.1");
        assert_eq!(result["updateAvailable"], true);
        assert_eq!(
            result["url"],
            "https://github.com/jwwsjlm/huifa-media-downloader/releases/tag/v0.4.1"
        );
        Ok(())
    }
    #[test]
    fn release_comparison_uses_semantic_versions_and_trusted_pages() {
        let release = |tag: &str| json!({"tag_name":tag,"html_url":"https://github.com/jwwsjlm/huifa-media-downloader/releases/tag/v0.4.0"});
        assert_eq!(
            parse(&release("v0.4.1"), "0.4.0").unwrap()["updateAvailable"],
            true
        );
        assert_eq!(
            parse(&release("v0.4.1"), "0.4.1").unwrap()["updateAvailable"],
            false
        );
        assert_eq!(
            parse(&release("v0.3.0"), "0.4.1").unwrap()["updateAvailable"],
            false
        );
        assert_eq!(
            parse(&release("v0.10.0"), "0.4.1").unwrap()["updateAvailable"],
            true
        );
        assert!(parse(&release("v0.4.0-beta.1"), "0.4.1").is_err());
        assert!(!valid_url("https://github.com/other/repo/releases/tag/v1"));
        assert!(!valid_url(
            "https://github.com.evil.test/jwwsjlm/huifa-media-downloader/releases/latest"
        ));
        for url in [
            "http://github.com/jwwsjlm/huifa-media-downloader/releases/tag/v0.4.1",
            "https://github.com/other/repo/releases/tag/v0.4.1",
            "https://github.com/jwwsjlm/huifa-media-downloader/releases/latest",
            "https://github.com/jwwsjlm/huifa-media-downloader/releases/tag/v0.4.1/extra",
            "https://github.com/jwwsjlm/huifa-media-downloader/releases/tag/v0.4.2-beta.1",
            "https://github.com/jwwsjlm/huifa-media-downloader/releases/tag/v0.4.1?token=secret",
        ] {
            assert!(release_page(url, "0.4.0").is_err(), "{url}");
        }
    }

    #[tokio::test]
    #[ignore = "requires HUIFA_TEST_RELEASE=1 and public GitHub API access"]
    async fn live_release_check_recognizes_old_and_current_versions() -> Result<()> {
        if std::env::var("HUIFA_TEST_RELEASE").as_deref() != Ok("1") {
            return Ok(());
        }
        let previous = status_for("0.4.0", "").await?;
        println!("Previous version check: {previous}");
        ensure!(
            previous["updateAvailable"] == true,
            "Previous release did not detect the update"
        );
        let current = status_for(env!("CARGO_PKG_VERSION"), "").await?;
        println!("Current version check: {current}");
        ensure!(
            current["latestVersion"] == previous["latestVersion"],
            "Latest release changed during verification"
        );
        let latest = semver::Version::parse(current["latestVersion"].as_str().unwrap())?;
        let installed = semver::Version::parse(env!("CARGO_PKG_VERSION"))?;
        ensure!(
            current["updateAvailable"] == (latest > installed),
            "Incorrect current version comparison"
        );
        ensure!(
            current["url"] == previous["url"],
            "Update links do not match"
        );
        Ok(())
    }
}

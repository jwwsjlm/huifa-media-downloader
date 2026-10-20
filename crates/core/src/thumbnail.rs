use crate::{
    App,
    model::{Task, text},
};
use anyhow::{Context, Result, bail, ensure};
use reqwest::header::{AUTHORIZATION, COOKIE, HeaderMap, HeaderName, HeaderValue, LOCATION};
use serde_json::Value;
use std::{
    collections::HashSet,
    io::{Cursor, Write},
    path::PathBuf,
    time::Duration,
};
use tokio_util::sync::CancellationToken;

pub(crate) async fn cache_preview(
    app: &App,
    task: &Task,
    info: &Value,
    cancel: &CancellationToken,
) -> Result<Option<PathBuf>> {
    let directory = app
        .config
        .data_dir
        .join("tasks")
        .join(&task.id)
        .join("preview");
    crate::reject_links(&directory)?;
    let target = directory.join("thumbnail.jpg");
    crate::reject_links(&target)?;
    if target.is_file() {
        return Ok(Some(target));
    }
    let Some(bytes) = download(
        app,
        task,
        info,
        8 * 1024 * 1024,
        Duration::from_secs(10),
        cancel,
    )
    .await?
    else {
        return Ok(None);
    };
    let mut reader = image::ImageReader::new(Cursor::new(bytes)).with_guessed_format()?;
    let mut limits = image::Limits::default();
    limits.max_image_width = Some(8192);
    limits.max_image_height = Some(8192);
    limits.max_alloc = Some(64 * 1024 * 1024);
    reader.limits(limits);
    let image = reader.decode()?;
    let image = if image.width() > 640 || image.height() > 360 {
        image.thumbnail(640, 360)
    } else {
        image
    }
    .to_rgb8();
    ensure!(!cancel.is_cancelled(), "Operation interrupted");
    std::fs::create_dir_all(&directory)?;
    let mut output = tempfile::NamedTempFile::new_in(&directory)?;
    image::DynamicImage::ImageRgb8(image)
        .write_to(output.as_file_mut(), image::ImageFormat::Jpeg)?;
    output.as_file_mut().flush()?;
    output.as_file().sync_all()?;
    output.persist(&target)?;
    Ok(Some(target))
}

pub(crate) async fn download(
    app: &App,
    task: &Task,
    info: &Value,
    max_bytes: usize,
    timeout: Duration,
    cancel: &CancellationToken,
) -> Result<Option<Vec<u8>>> {
    let thumbnail = text(info, "thumbnail");
    let mut candidates = Vec::new();
    let mut seen = HashSet::new();
    if !thumbnail.is_empty() {
        let candidate = info["thumbnails"].as_array().and_then(|values| {
            values
                .iter()
                .rev()
                .find(|value| text(value, "url") == thumbnail)
        });
        candidates.push((thumbnail, candidate));
        seen.insert(thumbnail);
    }
    for candidate in info["thumbnails"].as_array().into_iter().flatten().rev() {
        let url = text(candidate, "url");
        if url.starts_with("http") && seen.insert(url) {
            candidates.push((url, Some(candidate)));
        }
    }
    if candidates.is_empty() {
        return Ok(None);
    }
    let origin = url::Url::parse(candidates[0].0)
        .ok()
        .map(|url| url.origin());
    let options = app.runtime_options(task)?;
    let cookies = app.task_cookies(task)?;
    let deadline = tokio::time::Instant::now() + timeout;
    let download = async {
        let mut failure = anyhow::anyhow!("No thumbnail candidate is available");
        for (thumbnail, candidate) in candidates {
            ensure!(!cancel.is_cancelled(), "Operation interrupted");
            ensure!(
                tokio::time::Instant::now() < deadline,
                "Thumbnail request timed out"
            );
            let mut headers = HeaderMap::new();
            for (source, candidate_headers) in [
                (info.get("http_headers"), false),
                (candidate.and_then(|value| value.get("http_headers")), true),
            ] {
                if let Some(source) = source.and_then(Value::as_object) {
                    for (name, value) in source {
                        if matches!(
                            name.to_ascii_lowercase().as_str(),
                            "cookie"
                                | "host"
                                | "content-length"
                                | "connection"
                                | "proxy-authorization"
                                | "proxy-connection"
                        ) {
                            continue;
                        }
                        if name.eq_ignore_ascii_case("authorization")
                            && !candidate_headers
                            && url::Url::parse(thumbnail).ok().map(|url| url.origin()) != origin
                        {
                            continue;
                        }
                        if let (Ok(name), Some(value)) =
                            (HeaderName::from_bytes(name.as_bytes()), value.as_str())
                            && let Ok(value) = HeaderValue::from_str(value)
                        {
                            headers.insert(name, value);
                        }
                    }
                }
            }
            let result = fetch(
                thumbnail,
                headers,
                cookies.as_deref(),
                &options.proxy,
                max_bytes,
                deadline.saturating_duration_since(tokio::time::Instant::now()),
                cancel,
            )
            .await
            .and_then(|bytes| {
                image::guess_format(&bytes)?;
                Ok(bytes)
            });
            ensure!(!cancel.is_cancelled(), "Operation interrupted");
            match result {
                Ok(bytes) => return Ok(Some(bytes)),
                Err(reason) => failure = reason,
            }
        }
        Err(failure).context("No thumbnail candidate could be downloaded")
    };
    tokio::select! {
        biased;
        _ = cancel.cancelled() => bail!("Operation interrupted"),
        result = tokio::time::timeout_at(deadline, download) => result.context("Thumbnail request timed out")?,
    }
}

async fn fetch(
    url: &str,
    mut headers: HeaderMap,
    cookies: Option<&str>,
    proxy: &str,
    max_bytes: usize,
    timeout: Duration,
    cancel: &CancellationToken,
) -> Result<Vec<u8>> {
    let mut builder = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(timeout);
    if !proxy.is_empty() {
        builder = builder.proxy(reqwest::Proxy::all(proxy)?);
    }
    let client = builder.build()?;
    let fetch = async {
        let mut url = url::Url::parse(url)?;
        for redirects in 0..=5 {
            ensure!(
                matches!(url.scheme(), "http" | "https")
                    && url.username().is_empty()
                    && url.password().is_none(),
                "Invalid thumbnail URL"
            );
            headers.remove(COOKIE);
            if let Some(cookies) = cookies {
                let value = cookie_header(cookies, &url);
                if !value.is_empty() {
                    headers.insert(COOKIE, HeaderValue::from_str(&value)?);
                }
            }
            let mut response = client
                .get(url.clone())
                .headers(headers.clone())
                .send()
                .await
                .map_err(reqwest::Error::without_url)?;
            if response.status().is_redirection() {
                ensure!(redirects < 5, "Too many thumbnail redirects");
                let target = url.join(
                    response
                        .headers()
                        .get(LOCATION)
                        .context("Missing thumbnail redirect target")?
                        .to_str()?,
                )?;
                if target.origin() != url.origin() {
                    headers.remove(AUTHORIZATION);
                }
                url = target;
                continue;
            }
            response = response
                .error_for_status()
                .map_err(reqwest::Error::without_url)?;
            ensure!(
                response
                    .content_length()
                    .is_none_or(|length| length <= max_bytes as u64),
                "Thumbnail is too large"
            );
            let mut bytes = Vec::new();
            while let Some(chunk) = response
                .chunk()
                .await
                .map_err(reqwest::Error::without_url)?
            {
                ensure!(
                    chunk.len() <= max_bytes.saturating_sub(bytes.len()),
                    "Thumbnail is too large"
                );
                bytes.extend_from_slice(&chunk);
            }
            return Ok(bytes);
        }
        unreachable!()
    };
    tokio::select! {
        biased;
        _ = cancel.cancelled() => bail!("Operation interrupted"),
        result = tokio::time::timeout(timeout, fetch) => result.context("Thumbnail request timed out")?,
    }
}

fn cookie_header(text: &str, url: &url::Url) -> String {
    let host = url.host_str().unwrap_or("").to_ascii_lowercase();
    let now = chrono::Utc::now().timestamp();
    text.lines()
        .filter_map(|line| {
            if line.starts_with('#') && !line.starts_with("#HttpOnly_") {
                return None;
            }
            let fields = line.trim_end_matches('\r').split('\t').collect::<Vec<_>>();
            if fields.len() != 7 {
                return None;
            }
            let domain = fields[0]
                .strip_prefix("#HttpOnly_")
                .unwrap_or(fields[0])
                .trim_start_matches('.')
                .to_ascii_lowercase();
            let domain_matches = host == domain
                || fields[1] == "TRUE"
                    && host
                        .strip_suffix(&domain)
                        .is_some_and(|prefix| prefix.ends_with('.'));
            let path = fields[2];
            let path_matches = url.path() == path
                || url
                    .path()
                    .strip_prefix(path)
                    .is_some_and(|rest| path.ends_with('/') || rest.starts_with('/'));
            let expiry = fields[4].parse::<i64>().ok()?;
            if domain.is_empty()
                || !domain_matches
                || !path_matches
                || fields[3] == "TRUE" && url.scheme() != "https"
                || expiry != 0 && expiry <= now
            {
                return None;
            }
            Some(if fields[5].is_empty() {
                fields[6].to_owned()
            } else {
                format!("{}={}", fields[5], fields[6])
            })
        })
        .collect::<Vec<_>>()
        .join("; ")
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    #[test]
    fn nameless_cookie_values_are_sent_as_bare_tokens() {
        let text = "# Netscape HTTP Cookie File\nwww.example.com\tFALSE\t/\tTRUE\t0\t\tanonymous\nwww.example.com\tFALSE\t/\tTRUE\t0\tnamed\tvalue\n";
        assert_eq!(
            cookie_header(
                text,
                &url::Url::parse("https://www.example.com/cover").unwrap()
            ),
            "anonymous; named=value"
        );
    }

    #[test]
    fn cookies_follow_domain_path_security_and_expiry() {
        let text = "# Netscape HTTP Cookie File\n#HttpOnly_.example.com\tTRUE\t/private\tTRUE\t0\tsid\tsecret\nwww.example.com\tFALSE\t/\tFALSE\t0\thost\tlocal\n.example.com\tTRUE\t/\tFALSE\t1\texpired\told\n.other.test\tTRUE\t/\tFALSE\t0\tother\thidden\n";
        assert_eq!(
            cookie_header(
                text,
                &url::Url::parse("https://www.example.com/private/cover").unwrap()
            ),
            "sid=secret; host=local"
        );
        assert_eq!(
            cookie_header(
                text,
                &url::Url::parse("http://www.example.com/private/cover").unwrap()
            ),
            "host=local"
        );
        assert_eq!(
            cookie_header(
                text,
                &url::Url::parse("https://cdn.example.com/private/cover").unwrap()
            ),
            "sid=secret"
        );
        assert_eq!(
            cookie_header(
                text,
                &url::Url::parse("https://www.example.com/private-other").unwrap()
            ),
            "host=local"
        );
        assert!(
            cookie_header(
                text,
                &url::Url::parse("https://notexample.com/private").unwrap()
            )
            .is_empty()
        );
    }

    async fn request(socket: &mut tokio::net::TcpStream) -> String {
        let mut data = Vec::new();
        loop {
            let mut byte = [0];
            if socket.read(&mut byte).await.unwrap() == 0 {
                break;
            }
            data.push(byte[0]);
            if data.ends_with(b"\r\n\r\n") {
                break;
            }
        }
        String::from_utf8(data).unwrap()
    }

    fn png() -> Vec<u8> {
        let image = image::RgbImage::from_pixel(24, 16, image::Rgb([40, 20, 90]));
        let mut bytes = std::io::Cursor::new(Vec::new());
        image::DynamicImage::ImageRgb8(image)
            .write_to(&mut bytes, image::ImageFormat::Png)
            .unwrap();
        bytes.into_inner()
    }

    #[tokio::test]
    async fn thumbnail_candidates_fall_back_without_repeating_urls_or_sharing_headers() -> Result<()>
    {
        let temp = tempfile::tempdir()?;
        let app = crate::App::open(crate::AppConfig {
            data_dir: temp.path().join("data"),
            download_dir: temp.path().join("downloads"),
            tool_dir: None,
        })
        .await?;
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
        let proxy = format!("http://{}", listener.local_addr()?);
        let expected = png();
        let response = expected.clone();
        let server = tokio::spawn(async move {
            for candidate in ["preferred", "invalid", "fallback"] {
                let (mut socket, _) = listener.accept().await.unwrap();
                let headers = request(&mut socket).await.to_ascii_lowercase();
                assert!(headers.starts_with(&format!(
                    "get http://{candidate}.example.test/private/cover http/1.1"
                )));
                assert!(headers.contains("user-agent: extractor-agent"));
                assert!(headers.contains(&format!("x-candidate: {candidate}")));
                assert!(headers.contains(&format!("cookie: sid={candidate}")));
                assert!(!headers.contains("unscoped"));
                if candidate == "preferred" {
                    assert!(headers.contains("authorization: bearer preferred-only"));
                } else {
                    assert!(!headers.contains("authorization:"));
                }
                let (status, bytes) = match candidate {
                    "preferred" => ("404 Not Found", Vec::new()),
                    "invalid" => ("200 OK", b"not an image".to_vec()),
                    _ => ("200 OK", response.clone()),
                };
                socket
                    .write_all(
                        format!(
                            "HTTP/1.1 {status}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                            bytes.len()
                        )
                        .as_bytes(),
                    )
                    .await
                    .unwrap();
                socket.write_all(&bytes).await.unwrap();
            }
        });
        let mut task = crate::tests::paused_task(Default::default());
        app.credentials.put_secret("fallback-proxy", &proxy)?;
        app.credentials.put_secret("fallback-cookies", "# Netscape HTTP Cookie File\npreferred.example.test\tFALSE\t/private\tFALSE\t0\tsid\tpreferred\ninvalid.example.test\tFALSE\t/private\tFALSE\t0\tsid\tinvalid\nfallback.example.test\tFALSE\t/private\tFALSE\t0\tsid\tfallback\n")?;
        task.proxy_snapshot = Some("fallback-proxy".into());
        task.cookie_snapshot = Some("fallback-cookies".into());
        let preferred = json!({"url":"http://preferred.example.test/private/cover","http_headers":{"X-Candidate":"preferred","Authorization":"Bearer preferred-only","Cookie":"unscoped=leak"}});
        let info = json!({
            "thumbnail":preferred["url"],
            "http_headers":{"User-Agent":"extractor-agent","Cookie":"unscoped=leak","Authorization":"Bearer original-origin-only"},
            "thumbnails":[
                {"url":"http://fallback.example.test/private/cover","http_headers":{"X-Candidate":"fallback"}},
                {"url":"http://invalid.example.test/private/cover","http_headers":{"X-Candidate":"invalid"}},
                preferred.clone(), preferred,
            ],
        });
        assert_eq!(
            download(
                &app,
                &task,
                &info,
                1024 * 1024,
                Duration::from_secs(5),
                &CancellationToken::new()
            )
            .await?,
            Some(expected)
        );
        server.await?;
        app.shutdown().await?;
        Ok(())
    }

    #[tokio::test]
    async fn thumbnail_fallbacks_share_one_deadline_and_stop_when_cancelled() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let app = crate::App::open(crate::AppConfig {
            data_dir: temp.path().join("data"),
            download_dir: temp.path().join("downloads"),
            tool_dir: None,
        })
        .await?;
        let task = crate::tests::paused_task(Default::default());
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
        let url = format!("http://{}", listener.local_addr()?);
        let server = tokio::spawn(async move {
            for (status, bytes) in [("404 Not Found", Vec::new()), ("200 OK", png())] {
                let (mut socket, _) = listener.accept().await.unwrap();
                let _ = request(&mut socket).await;
                tokio::time::sleep(Duration::from_millis(250)).await;
                let _ = socket
                    .write_all(
                        format!(
                            "HTTP/1.1 {status}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                            bytes.len()
                        )
                        .as_bytes(),
                    )
                    .await;
                let _ = socket.write_all(&bytes).await;
            }
        });
        let info = json!({"thumbnail":format!("{url}/first"),"thumbnails":[{"url":format!("{url}/second")}]});
        assert!(
            download(
                &app,
                &task,
                &info,
                1024 * 1024,
                Duration::from_millis(400),
                &CancellationToken::new()
            )
            .await
            .is_err()
        );
        server.abort();
        let _ = server.await;

        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
        let url = format!("http://{}", listener.local_addr()?);
        let cancel = CancellationToken::new();
        let stopped = cancel.clone();
        let server = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.unwrap();
            let _ = request(&mut socket).await;
            stopped.cancel();
            let _ = socket
                .write_all(
                    b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
                )
                .await;
            assert!(
                tokio::time::timeout(Duration::from_millis(100), listener.accept())
                    .await
                    .is_err(),
                "Cancellation requested another thumbnail candidate"
            );
        });
        let info = json!({"thumbnail":format!("{url}/first"),"thumbnails":[{"url":format!("{url}/second")}]});
        assert!(
            download(
                &app,
                &task,
                &info,
                1024 * 1024,
                Duration::from_secs(5),
                &cancel
            )
            .await
            .unwrap_err()
            .to_string()
            .contains("interrupted")
        );
        server.await?;
        app.shutdown().await?;
        Ok(())
    }

    #[tokio::test]
    async fn preview_is_local_and_uses_frozen_cookie_proxy_and_extractor_headers()
    -> anyhow::Result<()> {
        let temp = tempfile::tempdir()?;
        let app = crate::App::open(crate::AppConfig {
            data_dir: temp.path().join("data"),
            download_dir: temp.path().join("downloads"),
            tool_dir: None,
        })
        .await?;
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
        let proxy = format!("http://{}", listener.local_addr()?);
        let server = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.unwrap();
            let headers = request(&mut socket).await;
            assert!(
                headers.starts_with("GET http://example.test/private/cover?token=private HTTP/1.1")
            );
            let lower = headers.to_ascii_lowercase();
            assert!(lower.contains("cookie: sid=frozen"));
            assert!(!lower.contains("unscoped"));
            assert!(lower.contains("user-agent: extractor-agent"));
            assert!(lower.contains("referer: https://example.test/video"));
            let bytes = png();
            socket
                .write_all(
                    format!(
                        "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                        bytes.len()
                    )
                    .as_bytes(),
                )
                .await
                .unwrap();
            socket.write_all(&bytes).await.unwrap();
        });
        let mut task = crate::tests::paused_task(Default::default());
        app.credentials.put_secret(
            "preview-cookie",
            "# Netscape HTTP Cookie File\n.example.test\tTRUE\t/private\tFALSE\t0\tsid\tfrozen\n",
        )?;
        app.credentials.put_secret("preview-proxy", &proxy)?;
        task.cookie_snapshot = Some("preview-cookie".into());
        task.proxy_snapshot = Some("preview-proxy".into());
        let info = json!({"thumbnail":"http://example.test/private/cover?token=private", "http_headers":{"User-Agent":"extractor-agent", "Referer":"https://example.test/video", "Cookie":"unscoped=leak"}});
        let path = cache_preview(&app, &task, &info, &CancellationToken::new())
            .await?
            .unwrap();
        assert!(
            path.starts_with(
                app.config
                    .data_dir
                    .join("tasks")
                    .join(&task.id)
                    .join("preview")
            )
        );
        assert_eq!(image::open(&path)?.width(), 24);
        assert_eq!(
            image::guess_format(&std::fs::read(&path)?)?,
            image::ImageFormat::Jpeg
        );
        assert!(
            !std::fs::read(&path)?
                .windows(7)
                .any(|bytes| bytes == b"private")
        );
        server.await?;
        assert_eq!(
            cache_preview(&app, &task, &json!({}), &CancellationToken::new()).await?,
            Some(path)
        );
        app.shutdown().await?;
        Ok(())
    }

    #[tokio::test]
    async fn missing_or_invalid_images_do_not_create_preview_files() -> anyhow::Result<()> {
        let temp = tempfile::tempdir()?;
        let app = crate::App::open(crate::AppConfig {
            data_dir: temp.path().join("data"),
            download_dir: temp.path().join("downloads"),
            tool_dir: None,
        })
        .await?;
        let task = crate::tests::paused_task(Default::default());
        assert!(
            cache_preview(&app, &task, &json!({}), &CancellationToken::new())
                .await?
                .is_none()
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
        let url = format!("http://{}/cover", listener.local_addr()?);
        let server = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.unwrap();
            let _ = request(&mut socket).await;
            socket
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\nConnection: close\r\n\r\noops")
                .await
                .unwrap();
        });
        assert!(
            cache_preview(
                &app,
                &task,
                &json!({"thumbnail":url}),
                &CancellationToken::new()
            )
            .await
            .is_err()
        );
        assert!(
            !app.config
                .data_dir
                .join("tasks")
                .join(&task.id)
                .join("preview/thumbnail.jpg")
                .exists()
        );
        server.await?;
        app.shutdown().await?;
        Ok(())
    }

    #[tokio::test]
    async fn redirects_drop_cross_origin_auth_and_recompute_cookies() -> anyhow::Result<()> {
        let first = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
        let second = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
        let first_url = format!("http://{}/cover", first.local_addr()?);
        let target = format!("http://localhost:{}/cover", second.local_addr()?.port());
        let server = tokio::spawn(async move {
            let (mut socket, _) = first.accept().await.unwrap();
            let headers = request(&mut socket).await.to_ascii_lowercase();
            assert!(headers.contains("authorization: bearer private"));
            assert!(headers.contains("cookie: sid=private"));
            socket.write_all(format!("HTTP/1.1 302 Found\r\nLocation: {target}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n").as_bytes()).await.unwrap();
            let (mut socket, _) = second.accept().await.unwrap();
            let headers = request(&mut socket).await.to_ascii_lowercase();
            assert!(!headers.contains("authorization:"));
            assert!(!headers.contains("cookie:"));
            socket
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
                .await
                .unwrap();
        });
        let mut headers = reqwest::header::HeaderMap::new();
        headers.insert(reqwest::header::AUTHORIZATION, "Bearer private".parse()?);
        let bytes = fetch(
            &first_url,
            headers,
            Some("# Netscape HTTP Cookie File\n127.0.0.1\tFALSE\t/\tFALSE\t0\tsid\tprivate\n"),
            "",
            32,
            Duration::from_secs(2),
            &CancellationToken::new(),
        )
        .await?;
        assert_eq!(bytes, b"ok");
        server.await?;
        Ok(())
    }

    #[tokio::test]
    async fn response_limit_timeout_and_cancellation_are_enforced() -> anyhow::Result<()> {
        for content in [Some(b"HTTP/1.1 200 OK\r\nContent-Length: 99\r\nConnection: close\r\n\r\n".as_slice()), Some(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n4\r\nlong\r\n0\r\n\r\n".as_slice()), None] {
            let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
            let target = format!("http://{}/cover", listener.local_addr()?);
            let content = content.map(Vec::from);
            let server = tokio::spawn(async move {
                let (mut socket, _) = listener.accept().await.unwrap();
                let _ = request(&mut socket).await;
                if let Some(content) = content { let _ = socket.write_all(&content).await; }
                else { tokio::time::sleep(Duration::from_secs(2)).await; }
            });
            assert!(fetch(&target, Default::default(), None, "", 3, Duration::from_millis(100), &CancellationToken::new()).await.is_err());
            server.abort();
        }
        let cancelled = CancellationToken::new();
        cancelled.cancel();
        assert!(
            fetch(
                "http://127.0.0.1:9/",
                Default::default(),
                None,
                "",
                8,
                Duration::from_secs(10),
                &cancelled
            )
            .await
            .unwrap_err()
            .to_string()
            .contains("interrupted")
        );
        Ok(())
    }
}

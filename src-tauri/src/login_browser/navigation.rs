use anyhow::{Context, Result, bail, ensure};
use serde_json::Value;
#[cfg(any(windows, target_os = "linux"))]
use serde_json::json;
use std::time::Duration;
use tauri::{Manager, Webview};

pub async fn command(webview: Webview, action: &str, url: Option<&str>) -> Result<Value> {
    let root = webview.window().label().to_owned();
    ensure!(
        root.strip_prefix("login-")
            .is_some_and(|id| uuid::Uuid::parse_str(id).is_ok())
            && webview.label() == format!("{root}-toolbar"),
        "Only the login browser toolbar can navigate"
    );
    let action = navigation_action(action)?;
    let page = webview
        .app_handle()
        .get_webview(&format!("{root}-page"))
        .context("Login browser is closed")?;
    match action {
        "navigate" => page.navigate(navigation_url(
            url.context("A website address is required")?,
        )?)?,
        "reload" => page.reload()?,
        _ => {}
    }
    let (sender, receiver) = tokio::sync::oneshot::channel();
    page.with_webview(move |_native| {
        #[cfg(windows)]
        let result = (|| -> Result<Value> {
            use windows::core::{BOOL, PWSTR};
            unsafe {
                let browser = _native.controller().CoreWebView2()?;
                let mut back = BOOL(0);
                let mut forward = BOOL(0);
                browser.CanGoBack(&mut back)?;
                browser.CanGoForward(&mut forward)?;
                if action == "back" && back.as_bool() {
                    browser.GoBack()?;
                } else if action == "forward" && forward.as_bool() {
                    browser.GoForward()?;
                }
                let mut source = PWSTR::null();
                browser.Source(&mut source)?;
                let url = webview2_com::take_pwstr(source);
                Ok(json!({"url":url,"canGoBack":back.as_bool(),"canGoForward":forward.as_bool()}))
            }
        })();
        #[cfg(target_os = "linux")]
        let result = {
            use webkit2gtk::WebViewExt;
            let browser = _native.inner();
            if action == "back" && browser.can_go_back() {
                browser.go_back();
            } else if action == "forward" && browser.can_go_forward() {
                browser.go_forward();
            }
            Ok(json!({"url":browser.uri().map(|url|url.to_string()).unwrap_or_default(),"canGoBack":browser.can_go_back(),"canGoForward":browser.can_go_forward()}))
        };
        #[cfg(not(any(windows, target_os = "linux")))]
        let result: Result<Value> = Err(anyhow::anyhow!("Login browser navigation is unavailable"));
        let _ = sender.send(result);
    })?;
    tokio::time::timeout(Duration::from_secs(5), receiver)
        .await
        .context("Browser navigation timed out")?
        .context("Login browser closed during navigation")?
}

fn navigation_action(action: &str) -> Result<&'static str> {
    match action {
        "state" => Ok("state"),
        "back" => Ok("back"),
        "forward" => Ok("forward"),
        "reload" => Ok("reload"),
        "navigate" => Ok("navigate"),
        _ => bail!("Unknown browser navigation action"),
    }
}

fn navigation_url(text: &str) -> Result<tauri::Url> {
    let text = text.trim();
    ensure!(!text.is_empty(), "A website address is required");
    let address = if text.contains("://") {
        text.to_owned()
    } else {
        format!("https://{text}")
    };
    super::login_url(&address)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn navigation_actions_do_not_expose_other_browser_operations() {
        for action in ["state", "back", "forward", "reload", "navigate"] {
            assert_eq!(navigation_action(action).unwrap(), action);
        }
        for action in ["cookies.read", "eval", "close", "", "Navigate"] {
            assert!(navigation_action(action).is_err());
        }
    }

    #[test]
    fn navigation_only_accepts_websites_and_adds_https() {
        assert_eq!(
            navigation_url(" youtube.com/watch?v=123 ")
                .unwrap()
                .as_str(),
            "https://youtube.com/watch?v=123"
        );
        assert_eq!(
            navigation_url("http://localhost:1234/path")
                .unwrap()
                .as_str(),
            "http://localhost:1234/path"
        );
        for address in [
            "",
            "javascript:alert(1)",
            "file:///etc/passwd",
            "https://user:secret@example.com",
            "data:text/html,test",
        ] {
            assert!(navigation_url(address).is_err(), "{address}");
        }
    }
}

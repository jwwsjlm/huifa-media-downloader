use anyhow::{Context, Result, ensure};
use futures_util::{SinkExt, StreamExt};
use serde_json::{Value, json};
use std::{collections::HashMap, time::Duration};
use tokio::sync::{broadcast, mpsc, oneshot};
use tokio_tungstenite::tungstenite::Message;

struct Request {
    method: &'static str,
    params: Value,
    session: Option<String>,
    reply: oneshot::Sender<Result<Value>>,
}

pub struct Cdp {
    channel: mpsc::Sender<Request>,
    events: broadcast::Sender<Value>,
    driver: tokio::task::JoinHandle<()>,
}

impl Cdp {
    pub async fn connect(endpoint: &str) -> Result<Self> {
        let url = url::Url::parse(endpoint)?;
        ensure!(
            url.scheme() == "ws"
                && url.host_str() == Some("127.0.0.1")
                && url.username().is_empty()
                && url.password().is_none()
                && url.path().starts_with("/devtools/browser/")
                && url.query().is_none()
                && url.fragment().is_none(),
            "Invalid local browser endpoint"
        );
        let (socket, _) = tokio_tungstenite::connect_async(endpoint).await?;
        let (channel, mut receiver) = mpsc::channel::<Request>(16);
        let (events, _) = broadcast::channel(16);
        let outgoing = events.clone();
        let driver = tokio::spawn(async move {
            let (mut writer, mut reader) = socket.split();
            let mut next_id = 0u64;
            let mut pending: HashMap<u64, oneshot::Sender<Result<Value>>> = HashMap::new();
            loop {
                tokio::select! {
                    request = receiver.recv() => {
                        let Some(request) = request else { break };
                        next_id += 1;
                        let mut value = json!({"id":next_id,"method":request.method,"params":request.params});
                        if let Some(session) = request.session { value["sessionId"] = session.into(); }
                        if writer.send(Message::Text(value.to_string().into())).await.is_err() { break; }
                        pending.retain(|_, reply| !reply.is_closed());
                        pending.insert(next_id, request.reply);
                    }
                    message = reader.next() => {
                        let Some(Ok(message)) = message else { break };
                        let Message::Text(text) = message else { continue };
                        let Ok(value) = serde_json::from_str::<Value>(&text) else { continue };
                        if let Some(id) = value["id"].as_u64() {
                            if let Some(reply) = pending.remove(&id) {
                                let result = if value.get("error").is_some() {
                                    Err(anyhow::anyhow!("Chromium rejected the login action"))
                                } else { Ok(value["result"].clone()) };
                                let _ = reply.send(result);
                            }
                        } else {
                            if value["method"] == "Page.screencastFrame" {
                                next_id += 1;
                                let ack = json!({"id":next_id,"method":"Page.screencastFrameAck","sessionId":value["sessionId"],"params":{"sessionId":value["params"]["sessionId"]}});
                                if writer.send(Message::Text(ack.to_string().into())).await.is_err() { break; }
                            }
                            let _ = outgoing.send(value);
                        }
                    }
                }
            }
            for (_, reply) in pending {
                let _ = reply.send(Err(anyhow::anyhow!("Browser login session ended")));
            }
            let _ = outgoing.send(json!({"method":"Inspector.detached"}));
        });
        Ok(Self {
            channel,
            events,
            driver,
        })
    }

    pub fn subscribe(&self) -> broadcast::Receiver<Value> {
        self.events.subscribe()
    }
    pub fn is_closed(&self) -> bool {
        self.driver.is_finished()
    }

    pub async fn request(
        &self,
        method: &'static str,
        params: Value,
        session: Option<&str>,
    ) -> Result<Value> {
        self.request_timeout(method, params, session, Duration::from_secs(10))
            .await
    }

    async fn request_timeout(
        &self,
        method: &'static str,
        params: Value,
        session: Option<&str>,
        timeout: Duration,
    ) -> Result<Value> {
        let (reply, receiver) = oneshot::channel();
        tokio::time::timeout(timeout, async {
            self.channel
                .send(Request {
                    method,
                    params,
                    session: session.map(str::to_owned),
                    reply,
                })
                .await
                .context("Browser login session ended")?;
            receiver
                .await
                .context("Browser login session ended")?
                .with_context(|| format!("Chromium command failed: {method}"))
        })
        .await
        .context("Chromium command timed out")?
    }
}

impl Drop for Cdp {
    fn drop(&mut self) {
        self.driver.abort();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn matches_responses_and_handles_events_errors_timeout_and_disconnect() -> Result<()> {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
        let port = listener.local_addr()?.port();
        let server = tokio::spawn(async move {
            let (stream, _) = listener.accept().await.unwrap();
            let mut socket = tokio_tungstenite::accept_async(stream).await.unwrap();
            let mut requests = Vec::new();
            for _ in 0..2 {
                let text = socket.next().await.unwrap().unwrap().into_text().unwrap();
                requests.push(serde_json::from_str::<Value>(&text).unwrap());
            }
            for request in requests.iter().rev() {
                socket
                    .send(Message::Text(
                        json!({"id":request["id"],"result":{"method":request["method"]}})
                            .to_string()
                            .into(),
                    ))
                    .await
                    .unwrap();
            }
            socket.send(Message::Text(json!({"method":"Page.screencastFrame","sessionId":"tab","params":{"sessionId":7}}).to_string().into())).await.unwrap();
            let ack: Value =
                serde_json::from_str(&socket.next().await.unwrap().unwrap().into_text().unwrap())
                    .unwrap();
            assert_eq!(ack["method"], "Page.screencastFrameAck");
            let bad: Value =
                serde_json::from_str(&socket.next().await.unwrap().unwrap().into_text().unwrap())
                    .unwrap();
            socket
                .send(Message::Text(
                    json!({"id":bad["id"],"error":{"message":"secret"}})
                        .to_string()
                        .into(),
                ))
                .await
                .unwrap();
            let _ = socket.next().await;
            // Leave the timeout request unanswered, then close with another request pending.
            let _ = socket.next().await;
        });
        let cdp = Cdp::connect(&format!("ws://127.0.0.1:{port}/devtools/browser/test")).await?;
        let mut events = cdp.subscribe();
        let (a, b) = tokio::join!(
            cdp.request("A", json!({}), None),
            cdp.request("B", json!({}), Some("tab"))
        );
        assert_eq!(a?["method"], "A");
        assert_eq!(b?["method"], "B");
        assert_eq!(events.recv().await?["method"], "Page.screencastFrame");
        let reason = cdp.request("Bad", json!({}), None).await.unwrap_err();
        assert!(!format!("{reason:#}").contains("secret"));
        assert!(
            cdp.request_timeout("Timeout", json!({}), None, Duration::from_millis(25))
                .await
                .is_err()
        );
        assert!(cdp.request("Disconnect", json!({}), None).await.is_err());
        server.await?;
        assert!(
            Cdp::connect("ws://example.com/devtools/browser/test")
                .await
                .is_err()
        );
        Ok(())
    }
}

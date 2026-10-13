use anyhow::{Context, Result};
use huifa_server::{Config, Server};
use std::{net::SocketAddr, path::PathBuf};

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "huifa_server=info".into()),
        )
        .init();
    let account = std::env::var("HUIFA_ACCOUNT").context("HUIFA_ACCOUNT is required")?;
    let password = std::env::var("HUIFA_PASSWORD")
        .context("HUIFA_PASSWORD is required; refusing unauthenticated startup")?;
    let server = Server::open(Config {
        account,
        password,
        public_origin: std::env::var("HUIFA_PUBLIC_ORIGIN").ok(),
        data_dir: env_path("HUIFA_DATA_DIR", "/data"),
        download_dir: env_path("HUIFA_DOWNLOAD_DIR", "/downloads"),
        tool_dir: std::env::var_os("HUIFA_TOOL_DIR").map(PathBuf::from),
        frontend_dir: env_path("HUIFA_FRONTEND_DIR", "/app/frontend"),
        chromium: env_path("HUIFA_CHROMIUM", "/usr/bin/chromium"),
    })
    .await?;
    let address: SocketAddr = std::env::var("HUIFA_BIND")
        .unwrap_or_else(|_| "0.0.0.0:19491".into())
        .parse()?;
    let listener = tokio::net::TcpListener::bind(address).await?;
    tracing::info!(%address,"authenticated web server listening");
    let shutdown_server = server.clone();
    axum::serve(
        listener,
        server
            .router()
            .into_make_service_with_connect_info::<SocketAddr>(),
    )
    .with_graceful_shutdown(async move {
        shutdown_signal().await;
        shutdown_server.shutdown().await;
    })
    .await?;
    Ok(())
}
fn env_path(name: &str, fallback: &str) -> PathBuf {
    std::env::var_os(name)
        .map(PathBuf::from)
        .unwrap_or_else(|| fallback.into())
}
async fn shutdown_signal() {
    #[cfg(unix)]
    {
        let mut term = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
            .expect("SIGTERM handler");
        tokio::select! { _ = tokio::signal::ctrl_c() => {}, _ = term.recv() => {} }
    }
    #[cfg(not(unix))]
    {
        let _ = tokio::signal::ctrl_c().await;
    }
}

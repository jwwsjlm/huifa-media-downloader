use anyhow::{Context, Result, bail, ensure};
use std::{
    net::{IpAddr, SocketAddr},
    time::Duration,
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
    task::{JoinHandle, JoinSet},
};

pub struct NetworkProxy {
    pub address: SocketAddr,
    handle: JoinHandle<()>,
}

impl NetworkProxy {
    pub fn start() -> Result<Self> {
        let listener = std::net::TcpListener::bind("127.0.0.1:0")?;
        listener.set_nonblocking(true)?;
        let listener = TcpListener::from_std(listener)?;
        let address = listener.local_addr()?;
        let handle = tokio::spawn(async move {
            let mut connections = JoinSet::new();
            loop {
                tokio::select! {
                    incoming = listener.accept() => {
                        let Ok((socket,_)) = incoming else { break };
                        if connections.len() < 64 {
                            connections.spawn(tokio::time::timeout(Duration::from_secs(15*60), proxy_connection(socket)));
                        }
                    }
                    Some(_) = connections.join_next(), if !connections.is_empty() => {}
                }
            }
        });
        Ok(Self { address, handle })
    }
}

impl Drop for NetworkProxy {
    fn drop(&mut self) {
        // Dropping the listener's JoinSet also stops every open tunnel.
        self.handle.abort();
    }
}

pub fn public_ip(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(ip) => !matches!(
            ip.octets(),
            [0 | 10 | 127, _, _, _]
                | [100, 64..=127, _, _]
                | [169, 254, _, _]
                | [172, 16..=31, _, _]
                | [192, 0, 0 | 2, _]
                | [192, 88, 99, _]
                | [192, 168, _, _]
                | [198, 18 | 19, _, _]
                | [198, 51, 100, _]
                | [203, 0, 113, _]
                | [224..=255, _, _, _]
        ),
        IpAddr::V6(ip) => {
            if let Some(ip) = ip.to_ipv4_mapped() {
                return public_ip(ip.into());
            }
            let segments = ip.segments();
            segments[0] & 0xe000 == 0x2000
                && segments[0] != 0x2002
                && !(segments[0] == 0x2001 && (segments[1] < 0x200 || segments[1] == 0xdb8))
                && !(segments[0] == 0x3fff && segments[1] < 0x1000)
        }
    }
}

pub async fn resolve_public(host: &str, port: u16) -> Result<Vec<SocketAddr>> {
    ensure!(port != 0, "invalid destination port");
    let addresses = tokio::time::timeout(
        Duration::from_secs(5),
        tokio::net::lookup_host((host.trim_matches(['[', ']']), port)),
    )
    .await
    .context("login destination lookup timed out")??
    .collect::<Vec<_>>();
    ensure!(
        !addresses.is_empty() && addresses.iter().all(|address| public_ip(address.ip())),
        "local login destinations are unavailable"
    );
    Ok(addresses)
}

async fn proxy_connection(mut client: TcpStream) -> Result<()> {
    let mut upstream =
        tokio::time::timeout(Duration::from_secs(15), socks_connect(&mut client)).await??;
    tokio::io::copy_bidirectional(&mut client, &mut upstream).await?;
    Ok(())
}

async fn socks_connect(client: &mut TcpStream) -> Result<TcpStream> {
    let mut greeting = [0; 2];
    client.read_exact(&mut greeting).await?;
    ensure!(
        greeting[0] == 5 && greeting[1] > 0,
        "invalid proxy greeting"
    );
    let mut methods = vec![0; greeting[1] as usize];
    client.read_exact(&mut methods).await?;
    if !methods.contains(&0) {
        client.write_all(&[5, 0xff]).await?;
        bail!("unsupported proxy authentication");
    }
    client.write_all(&[5, 0]).await?;
    let result = async {
        let mut request = [0; 4];
        client.read_exact(&mut request).await?;
        ensure!(
            request[0] == 5 && request[1] == 1 && request[2] == 0,
            "only TCP connections are supported"
        );
        let host = match request[3] {
            1 => {
                let mut bytes = [0; 4];
                client.read_exact(&mut bytes).await?;
                std::net::Ipv4Addr::from(bytes).to_string()
            }
            4 => {
                let mut bytes = [0; 16];
                client.read_exact(&mut bytes).await?;
                std::net::Ipv6Addr::from(bytes).to_string()
            }
            3 => {
                let size = client.read_u8().await?;
                let mut bytes = vec![0; size as usize];
                client.read_exact(&mut bytes).await?;
                String::from_utf8(bytes)?
            }
            _ => bail!("invalid destination address"),
        };
        let port = client.read_u16().await?;
        let addresses = resolve_public(&host, port).await?;
        // Connect to the checked IPs, without a second DNS lookup that could rebind.
        tokio::time::timeout(
            Duration::from_secs(10),
            TcpStream::connect(addresses.as_slice()),
        )
        .await?
        .map_err(anyhow::Error::from)
    }
    .await;
    match result {
        Ok(upstream) => {
            client.write_all(&[5, 0, 0, 1, 0, 0, 0, 0, 0, 0]).await?;
            Ok(upstream)
        }
        Err(error) => {
            let _ = client.write_all(&[5, 2, 0, 1, 0, 0, 0, 0, 0, 0]).await;
            Err(error)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_local_mapped_and_special_use_addresses() {
        for ip in [
            "127.0.0.1",
            "0.0.0.0",
            "10.1.2.3",
            "100.64.0.1",
            "169.254.169.254",
            "172.16.0.1",
            "192.168.1.1",
            "198.18.0.1",
            "203.0.113.1",
            "224.0.0.1",
            "::1",
            "fc00::1",
            "fe80::1",
            "::ffff:127.0.0.1",
            "::ffff:192.168.1.1",
            "64:ff9b::7f00:1",
            "2001:db8::1",
            "2002:7f00:1::",
            "3fff::1",
        ] {
            assert!(!public_ip(ip.parse().unwrap()), "accepted {ip}");
        }
        for ip in [
            "1.1.1.1",
            "8.8.8.8",
            "::ffff:8.8.8.8",
            "2606:4700:4700::1111",
            "2001:4860:4860::8888",
        ] {
            assert!(public_ip(ip.parse().unwrap()), "rejected {ip}");
        }
    }

    #[tokio::test]
    async fn blocks_private_tcp_connections_after_literal_or_dns_resolution() -> Result<()> {
        let fixture = TcpListener::bind("127.0.0.1:0").await?;
        let proxy = NetworkProxy::start()?;
        for (kind, host) in [
            (1, vec![127, 0, 0, 1]),
            (3, b"localhost".to_vec()),
            (
                4,
                "::ffff:127.0.0.1"
                    .parse::<std::net::Ipv6Addr>()?
                    .octets()
                    .to_vec(),
            ),
        ] {
            let mut socket = TcpStream::connect(proxy.address).await?;
            socket.write_all(&[5, 1, 0]).await?;
            let mut greeting = [0; 2];
            socket.read_exact(&mut greeting).await?;
            assert_eq!(greeting, [5, 0]);
            let mut request = vec![5, 1, 0, kind];
            if kind == 3 {
                request.push(host.len() as u8);
            }
            request.extend(host);
            request.extend(fixture.local_addr()?.port().to_be_bytes());
            socket.write_all(&request).await?;
            let mut reply = [0; 10];
            socket.read_exact(&mut reply).await?;
            assert_eq!(reply[1], 2);
        }
        assert!(
            tokio::time::timeout(Duration::from_millis(20), fixture.accept())
                .await
                .is_err()
        );
        Ok(())
    }
}

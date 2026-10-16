# Docker web application

The Rust web host serves the same React build as the desktop app on port 19491.
Both `linux/amd64` and `linux/arm64` use their corresponding official standalone
yt-dlp and QuickJS-NG binaries, verified against release checksums; FFmpeg and Chromium
come from Debian packages. Python application code is not used at runtime.

Run `docker compose up --build -d`; a new installation defaults to account `admin`
and password `admin`. Optional `HUIFA_ACCOUNT` and `HUIFA_PASSWORD` seed a new data
directory only. After initialization the persisted configuration takes precedence.
An explicitly empty initial credential is rejected. Set
`HUIFA_PUBLIC_ORIGIN` to the exact URL you open, such as `http://server:19491`.
The default is `http://localhost:19491`. For an HTTPS reverse proxy, use its public
HTTPS origin; the session cookie then gains the Secure flag. A direct HTTP URL is
supported as requested, and its login traffic is plaintext on the network.

The new named volumes `rust-data` and `rust-downloads` are separate from legacy
application data. Cookies, proxy settings and AI keys are plaintext files under
`/data/credentials`; back up `/data`. Old encrypted records migrate at startup, so
retain their `.credentials.key` until migration completes. Mounted host directories
must be owned by UID/GID 10001. `/app` runs read-only, with a non-root UID, dropped
capabilities, and a writable temporary filesystem. No Docker socket is mounted.
The `/tmp` tmpfs uses `exec,nosuid,nodev`: the official standalone yt-dlp binary
extracts bundled native libraries there and needs executable memory mappings.

Remote login starts one disposable headless Chromium profile on demand. Its
screencast and bounded mouse/keyboard input use authenticated endpoints on the
same port. The CDP port binds to 127.0.0.1 and is never published. Save imports
Cookies into the local plaintext credential store, stops Chromium and deletes the
profile. Closing the session, 15 minutes of idle time, or SIGTERM also cleans it
up. Container Chromium uses `--no-sandbox` because user namespaces are commonly
disabled; container isolation and the non-root user are its containment boundary.
Some websites reject headless login or require device verification; a Netscape
Cookie file remains available as an import option.

Remote login accepts public websites only. Chromium's TCP traffic goes through a
loopback SOCKS5 proxy that rejects local, private and special-use addresses after
DNS resolution, then connects to the checked IPs. Redirects, child pages and
subresources use the same proxy. Chromium's implicit loopback bypass and direct
hostname resolution are disabled; QUIC and non-proxied WebRTC UDP are disabled.
The proxy stops with the disposable browser session. See
[Chromium's proxy documentation](https://chromium.googlesource.com/chromium/src/+/HEAD/net/docs/proxy.md).
Runtime components are updated by updating the Docker image and recreating the
container; the web interface offers component checks only.

## MCP interface

After signing in, open **AI 媒体服务（MCP）** in the sidebar or visit `/mcp`.
The browser page and AI transport have separate paths: the JSON configuration
uses `/mcp-server` on the same external origin and published port. For example,
`http://NAS-IP:19491/mcp` displays the page while an AI client connects to
`http://NAS-IP:19491/mcp-server`. No additional port needs publishing.

One switch enables or disables the service, and `/data` preserves its state.
The page displays generated JSON, a copy button and the available tools.
MCP calls require the shared access password as a Bearer token; management actions retain the normal web
session and CSRF checks. Calls use the saved Cookie/proxy and existing download
queue. Set `HUIFA_PUBLIC_ORIGIN` to the externally accessed origin when using
a NAS address, mapped port or reverse proxy. The service is disabled initially.

Web login and MCP share one access password. Change it in **Settings → Access
password** using the current and new password. `access.json` in `/data` persists
its Argon2 hash; restarting or recreating the container does not reset it. A change
invalidates existing web and MCP sessions. MCP clients send
`Authorization: Bearer <access-password>`; generated examples use
`YOUR_ACCESS_PASSWORD` and never include the saved password. Update clients after
changing it. Change the initial password before exposing the service to other
devices. HTTP sends credentials without transport encryption; use an HTTPS proxy
for an untrusted network.

Web login and MCP share a limit of five failed authentications per connecting IP
within 15 minutes. Successful authentication clears that IP's failures and does
not consume the limit. The server uses the actual connection IP, without trusting
`Forwarded` or `X-Forwarded-For`; clients behind a proxy or NAT may share the
failure limit. Add per-client limits at the proxy when serving multiple users.

The independent **Start MCP when the app starts** option controls container
startup. New installations default off; existing configurations inherit the
previous enabled state. Manually starting or stopping MCP does not change this
preference.

`runtime-tool-lock.json` pins the official yt-dlp and QuickJS-NG assets and SHA-256
checksums for both architectures. Build both architectures with
`docker buildx build --platform linux/amd64,linux/arm64`.

Release CI supplies `--build-arg USE_PREBUILT_FRONTEND=1` after placing the shared
React build in `frontend/dist`. A normal local build runs `npm ci` and builds it.
`scripts/check-rust-container.mjs` checks an actual download/transcode and volume
restart; `docker/check-browser.mjs` checks the actual remote login flow. Both take
`HUIFA_IMAGE` and `HUIFA_PLATFORM` environment variables and clean up their own
containers and test volumes.

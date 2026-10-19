# Docker web application

The Rust web host serves the same React build as the desktop app on port 19491.
Both `linux/amd64` and `linux/arm64` use their corresponding official standalone
yt-dlp and QuickJS-NG binaries. FFmpeg and FFprobe use the pinned compatibility
build from BtbN. These runtime downloads are verified against the SHA-256 values
in [`runtime-tool-lock.json`](../runtime-tool-lock.json). Chromium comes from
Debian packages. Python application code is not used at runtime.

## Released image

Create a directory and save the following as `compose.yaml`. No repository clone
is needed. The image automatically selects amd64 or ARM64 for the host:

```yaml
services:
  downloader:
    image: ghcr.io/jwwsjlm/huifa-media-downloader:latest
    ports:
      - "19491:19491"
    environment:
      HUIFA_ACCOUNT: admin
      HUIFA_PASSWORD: admin
      HUIFA_PUBLIC_ORIGIN: http://localhost:19491
    volumes:
      - ./data:/data
      - ./downloads:/downloads
    read_only: true
    tmpfs:
      - /tmp:rw,exec,nosuid,nodev,size=512m
    shm_size: 256m
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    stop_grace_period: 30s
    restart: unless-stopped
```

Set `HUIFA_PUBLIC_ORIGIN` to the complete origin used by the browser: scheme,
host and port, with no path. Use `http://localhost:19491` locally,
`http://192.168.1.10:19491` for a NAS, or `https://download.example.com` for an
HTTPS reverse proxy. Update it when changing the published host port. HTTPS
origins give the session cookie its Secure flag; direct HTTP sends login traffic
without transport encryption.

Create the host directories beside `compose.yaml`:

```sh
mkdir -p data downloads
```

On Linux/NAS, both directories must be writable by container UID/GID
`10001:10001`. For these newly created directories, run:

```sh
sudo chown 10001:10001 data downloads
```

Handle existing directories according to their current permissions; avoid
recursively changing existing user data. Docker Desktop on Windows does not
require `chown`. Then run from the Compose directory:

```sh
docker compose pull
docker compose up -d
```

Open [http://localhost:19491](http://localhost:19491), or the configured NAS/domain
address. The initial account and password are both `admin`. Before allowing
access from other devices, change the password in **Settings → Access password**
using the current and new password. It is shared by web login and HTTP MCP;
changing it invalidates existing sessions, so sign in again and update AI clients.

`HUIFA_ACCOUNT` and `HUIFA_PASSWORD` seed an empty data directory only. An
explicitly empty initial credential is rejected. Later starts use the persisted
account and Argon2 password hash in `/data/access.json`; recreating the container
or changing environment variables does not reset an existing password.

### Update and preserve data

Compose binds `./data` to `/data` and `./downloads` to `/downloads`, relative to the
directory containing `compose.yaml`. `data` contains configuration, credentials,
task records and recovery data; `downloads` contains media. Stop the container
before backing up both directories and `compose.yaml`.

The example's `latest` tag follows the latest stable release. To pin a release,
replace `latest` with its version number, without a leading `v`. Update from the
same Compose directory with `docker compose pull` and `docker compose up -d`.
Keep the data/download directories and their mount paths; deleting them is not
part of an upgrade. Runtime components are updated with the image and container
recreation; the web interface offers component checks only.

Existing `rust-data` / `rust-downloads` named volumes are not copied automatically
when changing mounts. Keep their original mount configuration until their data
has been migrated. Do not run `docker compose down -v`: it deletes named volumes
referenced by the configuration.

## Source build

With the repository cloned, run from its root, using the supplied `compose.yaml`:

```sh
mkdir -p data downloads
# Linux/NAS only, for newly created directories:
sudo chown 10001:10001 data downloads
docker compose up --build -d
```

On Windows Docker Desktop, omit `chown`. The source Compose configuration uses
the same host mounts, credentials and origin settings described above.

## Container and browser login

Cookies, proxy settings and AI keys are plaintext files under `/data/credentials`.
Old encrypted records migrate at startup, so retain their `.credentials.key` until
migration completes. Mounted directories must be writable by UID/GID 10001.
`/app` runs read-only, with a non-root UID, dropped
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

See [MCP usage](../docs/MCP.md) for client configuration and tools.

## Build checks

`runtime-tool-lock.json` pins yt-dlp, QuickJS-NG and the compatibility FFmpeg/FFprobe
pair for both architectures. The installer validates matching FFmpeg/FFprobe
versions and required encoders before installing the pair. From the repository
root, build both architectures with
`docker buildx build --platform linux/amd64,linux/arm64 .`.

Release CI supplies `--build-arg USE_PREBUILT_FRONTEND=1` after placing the shared
React build in `frontend/dist`. A normal local build runs `npm ci` and builds it.
`scripts/check-rust-container.mjs` checks an actual download/transcode and volume
restart; `docker/check-browser.mjs` checks the actual remote login flow. Both take
`HUIFA_IMAGE` and `HUIFA_PLATFORM` environment variables and clean up their own
containers and test volumes.

## Download recovery and upgrades

The web host shares the desktop download workflow: explicit quality, frame rate
and audio requirements must be met. Missing requested subtitles are skipped by
default; disable **Continue when subtitles are unavailable** to require them.
A failed item does not stop the rest of a batch. Different sources with the same output filename prompt before
transfer, with a second confirmation before replacement. Queue priority persists;
live capture offers **Stop live capture and save** separately from cancellation.

Settings reports free space and recoverable caches. Successful downloads clean
their intermediate sources; paused and failed downloads retain recovery files.
Version and component checks use the saved download proxy. Container runtime
updates still require recreating the image as described above.

Before an application upgrade or credential migration, `/data/backups` receives
a consistent database and configuration backup, checksums and `RESTORE.txt`.
Restore with the container stopped. These backups exclude downloads, runtimes,
processing workspaces and browser profiles; continue backing up both host mounts.
Do not delete the data mount when pulling `latest` and recreating the container.

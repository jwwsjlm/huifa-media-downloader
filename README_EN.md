# Huifa Media Downloader

Huifa Media Downloader is a Rust media downloader and processing app. The Tauri desktop host and the Docker/NAS web host share one React interface. Official yt-dlp handles extraction, while FFmpeg handles merging, transcoding and cover processing. You can save videos, audio, collections and batches, or expose yt-dlp metadata and the download queue to your own AI client through MCP.

[简体中文](README.md) · [Releases](https://github.com/jwwsjlm/huifa-media-downloader/releases/latest) · [Issues](https://github.com/jwwsjlm/huifa-media-downloader/issues)

![Docker download queue](docs/images/docker-downloads.jpg)

## Features

- Official yt-dlp, FFmpeg, FFprobe and lightweight QuickJS-NG for videos, audio, collections, batches and multiple sites; existing tasks keep their frozen Deno runtime when needed.
- Automatic best/worst quality selection, formats and audio tracks, subtitles, chapters, cuts, SponsorBlock, transcoding and audio export.
- Separate download and processing queues. Each task freezes its plan, runtime versions and input, and supports pause, restart recovery and offline retries after processing failures.
- Completed media management, JPEG crop/padding exports and AI cover editing based on the original image.
- Encrypted Cookies, proxy settings and AI keys; browser Cookie import on desktop and a disposable remote browser login in Docker.
- Built-in MCP service. An AI client can query metadata, formats, collections, subtitles and comments, then create or control downloads. The service is disabled by default and the page displays copyable JSON configuration.
- Chinese/English, dark/light themes, virtual task lists, responsive layouts, SHA-256 checksums and signed desktop updates.

## Screenshots

These screenshots are from a real Docker container with isolated test data.

| Download queue | Completed media |
| --- | --- |
| ![Docker download queue](docs/images/docker-downloads.jpg) | ![Docker completed media](docs/images/docker-completed.jpg) |

| Settings | AI Video Service (MCP) |
| --- | --- |
| ![Docker settings](docs/images/docker-settings.jpg) | ![Docker MCP page](docs/images/mcp-docker.jpg) |

## Install

Choose an artifact from [Releases](https://github.com/jwwsjlm/huifa-media-downloader/releases/latest):

| Platform | Artifact |
| --- | --- |
| Windows 10/11 x64 installer | `HuifaDownloader-<version>-windows-x64-setup.exe` |
| Windows x64 portable | `HuifaDownloader-<version>-windows-x64-portable.zip` |
| Linux x64 | `HuifaDownloader-<version>-linux-x64.AppImage` |
| Docker amd64 / ARM64 | `ghcr.io/jwwsjlm/huifa-media-downloader:<version>` |

The installer and portable ZIP contain the same application with verified download and processing tools. You do not need a separate Python or FFmpeg install. The Windows installer currently has no Authenticode publisher certificate, but installers, portable archives and update manifests carry signatures and SHA-256 checksums.

Extract a portable ZIP and run `HuifaDownloader.exe`; keep `tools` and `portable.flag` beside it. Microsoft Edge WebView2 Runtime is required. Both Windows editions share system user data and Downloads folders. Close the other edition before switching, and replace the portable directory only while the app is closed.

The application ID is `com.huifa.downloader.rust`. Rust state is stored under `HuifaDownloaderRust` in the system local data directory, with media in the matching Downloads folder. Include `.credentials.key` in backups so encrypted Cookies and AI keys can be restored.

## Docker web host

Create `.env` in the repository root:

```dotenv
HUIFA_IMAGE=ghcr.io/jwwsjlm/huifa-media-downloader:0.3.0
HUIFA_ACCOUNT=your-account
HUIFA_PASSWORD=your-password
HUIFA_PUBLIC_ORIGIN=http://localhost:19491
```

```sh
docker compose up -d --no-build
```

Open `HUIFA_PUBLIC_ORIGIN` and sign in. For remote access, set it to the complete origin used by the browser. The `rust-data` and `rust-downloads` volumes preserve configuration and media; do not run `docker compose down -v` when that data matters. See the [Docker guide](docker/README.md) for container hardening, browser login and multi-architecture builds.

## MCP: use yt-dlp from your AI client

After signing in, open **AI 视频服务（MCP）** in the sidebar and enable the single switch. Desktop listens on `127.0.0.1:19492/mcp`. In Docker, `/mcp` is the management page and `/mcp-server` is the protocol endpoint on the same origin and published port:

```json
{
  "mcpServers": {
    "huifa": {
      "url": "http://localhost:19491/mcp-server"
    }
  }
}
```

The page generates the address for the current origin. MCP calls do not use a token; changing the switch still requires the normal web session and CSRF protection. MCP inherits saved Cookies, proxy and download settings, and returns sanitized metadata, task state and errors without Cookie contents, proxy passwords, internal paths or signed media URLs. Give the endpoint only to trusted AI clients. Desktop also offers stdio configuration using the bundled `huifa-mcp-bridge`, with no separate Python or Node install.

See [MCP usage](docs/MCP.md) for the complete tool list and schemas.

## Development and releases

Install Node.js 24, Rust stable and the platform's Tauri build dependencies:

```sh
npm ci --prefix frontend
node scripts/acquire-rust-tools.mjs
npm --prefix frontend run tauri -- dev
```

See [development and release instructions](docs/DEVELOPMENT.md) for checks, MCP bridge preparation and GitHub publishing. The current Rust implementation is the source of truth for builds and releases.

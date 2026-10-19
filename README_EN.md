# Huifa Media Downloader

A media downloader and processing app for Windows, macOS, Linux and Docker/NAS.
One interface handles videos, audio, collections and batches with yt-dlp and
FFmpeg, with an optional MCP service for your own AI client.

[简体中文](README.md) · **English** · [Latest download](https://github.com/jwwsjlm/huifa-media-downloader/releases/latest) · [Report an issue](https://github.com/jwwsjlm/huifa-media-downloader/issues)

![Download queue](docs/images/docker-downloads.jpg)

## Download and install

Choose the package matching your operating system and processor from the
[latest release](https://github.com/jwwsjlm/huifa-media-downloader/releases/latest).

| Platform | Release package |
| --- | --- |
| Windows 10/11 x64 | `HuifaDownloader-<version>-windows-x64-setup.exe` or `HuifaDownloader-<version>-windows-x64-portable.zip` |
| Windows ARM64 | `HuifaDownloader-<version>-windows-arm64-setup.exe` or `HuifaDownloader-<version>-windows-arm64-portable.zip` |
| Linux x64 | `HuifaDownloader-<version>-linux-x64.AppImage` |
| Linux ARM64 | `HuifaDownloader-<version>-linux-arm64.AppImage` |
| Intel Mac, macOS 15+ | `HuifaDownloader-<version>-macos-x64.dmg` |
| Apple silicon Mac, macOS 15+ | `HuifaDownloader-<version>-macos-arm64.dmg` |

Desktop packages include the download and processing tools for their architecture;
no separate Python or FFmpeg installation is needed. Releases include `SHA256SUMS`
and signed desktop update packages.

- **Windows:** WebView2 Runtime is required. For the portable edition, extract the
  ZIP and run `HuifaDownloader.exe`, keeping `tools` and `portable.flag` beside it.
  The installer currently has no Authenticode publisher certificate. Installer and
  portable editions share user data; close the app before switching or replacing files.
- **Linux:** make the AppImage executable, then run it. Choose x64 for `x86_64` or
  ARM64 for `aarch64`, for example `chmod +x ./HuifaDownloader-*-linux-x64.AppImage`.
- **macOS:** drag the app from the DMG into **Applications**. Builds require macOS
  15+, use ad-hoc signing and are not Apple-notarized. After verifying the download
  source, use **System Settings → Privacy & Security → Open Anyway** if needed;
  see [Apple's instructions](https://support.apple.com/en-us/102445).
  The `.app.tar.gz` files are updater packages.

For sign-in, recovery, data locations and troubleshooting, see the
[usage guide](docs/USAGE_EN.md).

## Quick start

1. Open the app, paste one or more media links, and choose quality or **Quick options**.
   Automatic mode queues single videos directly; collections and manual formats ask for confirmation.
2. Start the download. Follow the separate download and processing queues; use the
   task menu to pause, retry, change pending priority or view logs.
3. Find the result in **Completed**. Use its context menu to reveal/download the file,
   convert its format or compress a video. Open the completed item's details to play
   media or use Cover studio.

If a website requires sign-in, use **Settings → Network & cookies** first.
The [usage guide](docs/USAGE_EN.md) covers subtitles, batches, recovery and file safety.

## Main features

- **Downloads:** videos, audio, collections and batches, with automatic or manual
  formats, audio tracks, subtitles, chapters, cuts and SponsorBlock where supported.
- **Recoverable queues:** separate download and processing work, frozen task plans
  and runtimes, pause/resume, restart recovery and local processing retries.
- **Video compression:** target-size or bitrate controls, optional lower resolution
  and frame rate, HEVC/MKV output, and preservation checks before saving a new file.
- **GPU and FFmpeg policies:** detected hardware encoders, explicit CPU retry, a
  default Compatibility build, a newer Efficiency build and exact release-tag selection.
- **Media management:** actual quality and file size, source-file availability status,
  format conversion, JPEG crop/padding exports and AI cover editing.
- **Clipboard downloads:** paste/import links, or opt into desktop background clipboard
  monitoring; collections and manual formats still ask for confirmation.
- **Website sign-in:** automatic cookie matching, proxy settings, local browser sign-in
  and a disposable remote browser for Docker.
- **MCP and interface:** metadata and queue tools for trusted AI clients, Chinese/English,
  light/dark themes, virtual lists and responsive layouts.

Compression is lossy. Hardware support depends on the video, driver and installed
FFmpeg build; unsupported preservation paths are disabled. See
[compression limits](docs/USAGE_EN.md#compression-and-format-conversion).

## Docker / NAS

Use `ghcr.io/jwwsjlm/huifa-media-downloader:latest` for amd64 or ARM64.
Follow the [released-image Compose setup](docker/README.md#released-image), then open
[http://localhost:19491](http://localhost:19491). No repository clone is needed.

The initial account/password is `admin/admin`; change the access password before
allowing other devices to connect. Set `HUIFA_PUBLIC_ORIGIN` to the browser's exact
origin for NAS access or a reverse proxy. Keep both data and download mounts when upgrading.

Directory permissions, HTTPS, remote sign-in, backups and upgrades are covered in the
[Docker guide](docker/README.md). Container tools update with the image.

## Connect your AI client with MCP

1. Desktop: open **Settings → AI media service (MCP)**. Docker: open the sidebar's
   **AI media service (MCP)** page. Enable the service.
2. Copy the generated configuration into your client and replace the password
   placeholder with your access password (initially `admin`). Desktop also offers a bundled stdio bridge.
3. Ask the client to inspect media or manage downloads. Keep the app running;
   enable automatic MCP startup separately if wanted.

Desktop HTTP defaults to `http://127.0.0.1:19492/mcp`. Docker uses `/mcp-server`
on its existing web origin and port. Share the endpoint and password only with trusted
clients; queries inherit saved cookies and may expose private media content to the client.
See the [MCP guide](docs/MCP.md) for tools, authentication and limits.

## Documentation and development

| Guide | Contents |
| --- | --- |
| [Usage](docs/USAGE_EN.md) · [中文](docs/USAGE.md) | Downloads, compression, sign-in, recovery, backups and troubleshooting |
| [Runtime updates](docs/RUNTIME_UPDATES.md) | yt-dlp channels, FFmpeg policies and component verification |
| [MCP](docs/MCP.md) | Connection setup, tool schemas and permission boundaries |
| [Docker](docker/README.md) | Released images, deployment, storage and upgrades |
| [Development](docs/DEVELOPMENT.md) | Build dependencies, checks and releases |

With Node.js 24, Rust stable and your platform's Tauri dependencies installed:

```sh
npm ci --prefix frontend
node scripts/acquire-rust-tools.mjs
npm --prefix frontend run tauri -- dev
```

<details>
<summary>More screenshots</summary>

Screenshots use isolated test data in a real Docker container.

| Completed media | Settings | AI media service (MCP) |
| --- | --- | --- |
| ![Completed media](docs/images/docker-completed.jpg) | ![Settings](docs/images/docker-settings.jpg) | ![MCP page](docs/images/mcp-docker.jpg) |

</details>

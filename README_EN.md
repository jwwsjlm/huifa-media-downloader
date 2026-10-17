# Huifa Media Downloader

Huifa Media Downloader is a Rust media downloader and processing app. The Tauri desktop host and the Docker/NAS web host share one React interface. Official yt-dlp handles extraction, while FFmpeg handles merging, transcoding and cover processing. You can save videos, audio, collections and batches, or expose yt-dlp metadata and the download queue to your own AI client through MCP.

[简体中文](README.md) · [Releases](https://github.com/jwwsjlm/huifa-media-downloader/releases/latest) · [Issues](https://github.com/jwwsjlm/huifa-media-downloader/issues)

![Docker download queue](docs/images/docker-downloads.jpg)

## Features

- Official yt-dlp, FFmpeg, FFprobe and lightweight QuickJS-NG for videos, audio, collections, batches and multiple sites; existing tasks keep their frozen Deno runtime when needed.
- Automatic best/worst quality selection, formats and audio tracks, subtitles, chapters, cuts, SponsorBlock, transcoding and audio export.
- Separate download and processing queues. Each task freezes its plan, runtime versions and input, and supports pause, restart recovery and offline retries after processing failures.
- Completed media management, JPEG crop/padding exports and AI cover editing based on the original image.
- Cookies, proxy settings and AI keys stored in ordinary local files; built-in and compatible local browser sign-in plus existing-browser Cookie import on desktop, and disposable remote browser sign-in in Docker.
- Built-in MCP service. An AI client can query metadata, formats, collections, subtitles and comments, then create or control downloads. The service is disabled by default and the page displays copyable JSON configuration.
- Chinese/English, dark/light themes, virtual task lists, responsive layouts, SHA-256 checksums and signed desktop updates.

## Screenshots

These screenshots are from a real Docker container with isolated test data.

| Download queue | Completed media |
| --- | --- |
| ![Docker download queue](docs/images/docker-downloads.jpg) | ![Docker completed media](docs/images/docker-completed.jpg) |

| Settings | AI Media Service (MCP) |
| --- | --- |
| ![Docker settings](docs/images/docker-settings.jpg) | ![Docker MCP page](docs/images/mcp-docker.jpg) |

## Install

Choose an artifact from [Releases](https://github.com/jwwsjlm/huifa-media-downloader/releases/latest):

| Platform | Artifact |
| --- | --- |
| Windows 10/11 x64 installer | `HuifaDownloader-<version>-windows-x64-setup.exe` |
| Windows x64 portable | `HuifaDownloader-<version>-windows-x64-portable.zip` |
| Linux x64 | `HuifaDownloader-<version>-linux-x64.AppImage` |
| Docker amd64 / ARM64 | `ghcr.io/jwwsjlm/huifa-media-downloader:latest` |

The installer and portable ZIP contain the same application with verified download and processing tools. You do not need a separate Python or FFmpeg install. The Windows installer currently has no Authenticode publisher certificate, but installers, portable archives and update manifests carry signatures and SHA-256 checksums.

Extract a portable ZIP and run `HuifaDownloader.exe`; keep `tools` and `portable.flag` beside it. Microsoft Edge WebView2 Runtime is required. Both Windows editions share system user data and Downloads folders. Close the other edition before switching, and replace the portable directory only while the app is closed.

The application ID is `com.huifa.downloader.rust`. Rust state is stored under `HuifaDownloaderRust` in the system local data directory, with media in the matching Downloads folder. Cookies, proxy settings and AI keys are plaintext files in `credentials`; back up the entire data directory. Older encrypted records migrate automatically at startup and their key is removed after migration. Keep the original `.credentials.key` until that migration completes.

Under desktop **Settings → Network & cookies**, enter the source website and select **Open built-in browser** or **Local browser sign-in** directly, without an intermediate settings dialog. Use the built-in browser (Windows WebView2 / Linux WebKit), or a window from an installed Edge, Chrome or Chromium. The last successfully used address and browser choice are saved automatically. Cookies sync automatically during sign-in and once more before the browser closes. No save click is needed. New downloads and media queries match cookies to the target domain by default and pass them to yt-dlp through its cookie file; existing tasks retain their frozen credentials. Use the local browser for sites such as Google that reject embedded sign-in.

The built-in browser has an address bar and Back, Forward and Reload buttons. Press Enter to visit an address; omitted protocols default to HTTPS. The toolbar is isolated from website content, and websites and sign-in popups cannot call application commands.

Cookies are optional for sites that allow anonymous access. Without a matching record, queries and downloads run anonymously. Sync failures show an error but allow closing the login browser and keep previously saved cookies. The old Douyin extractor can report an API 403 as `Fresh cookies (not necessarily logged in)` even after signing in. The app temporarily embeds [yt-dlp Douyin fix PR #17826](https://github.com/yt-dlp/yt-dlp/pull/17826), commit `edf5ca18`, under the Unlicense. Only this compiled-in compatibility code is loaded; user plugin directories are disabled and the official yt-dlp executable is unchanged. Anonymous short-link identification, downloading and publication into the app's media library have passed. Site changes can still break extraction; the override will be removed after the upstream fix is included in the bundled engine.

Website sign-in shows automatic save status without cookie records, account names or a cookie-source selector. Sign in once and new downloads match cookies to their destination website automatically. Expand **Other import methods** when importing existing cookies is needed.

Automatic quality queues known single videos directly, including YouTube video URLs with playlist parameters. Unknown URLs are classified first; only actual collections show item selection. Manual mode opens the format picker. **Download clipboard** extracts, deduplicates and queues links. Fragment concurrency defaults to 4, recommends 6 and is capped at 8; existing valid settings are retained. Subtitles are off by default; choose a language to prefer manual tracks with automatic fallback and external SRT output. Missing subtitles produce a warning while the video continues. AI cover settings can fetch model IDs from the configured service or accept a manually entered ID.

On desktop, use **Settings → Downloads → Download folder → Choose folder** to select a writable folder. The selection saves immediately and survives restarts. It applies to new or restarted downloads; existing queued, running, paused and processing tasks keep their original folder, and existing files stay in place. Docker continues to use the Compose `./downloads:/downloads` volume mapping; the web interface cannot choose folders on the host computer.

New output uses `downloads/title/filename`, without a short task-ID directory. Adding the same source again selects its queued, running, processing or paused task without starting another download. Completed, failed or cancelled downloads reuse their record with the new settings. Existing results remain until the replacement has been validated; failure or cancellation preserves them. Historical folders are not moved in bulk; a successful replacement cleans only the old managed files and empty ID directory.

Stage prefixes such as `2/6` show the current major step and actual workflow size and disappear on completion. Task menus and details can copy the video title or source; desktop results also provide the full file path including the filename. Format conversion offers MP4, MKV, MOV and WebM, preferring remuxing and transcoding only when needed, with advanced codec controls and preserved input files. The sidebar indicates newer releases and opens their download page; About can recheck manually. Portable builds do not launch an updater installer.

Completed tasks and library entries show actual quality and file size. Details distinguish downloaded source and final output dimensions, frame rate, codecs and size. Quality labels come from media information; a "Highest quality" download plan does not guarantee a 4K source. Older tasks reuse saved local records and inspect existing output files when details are missing, without downloading the video again.

During downloads, the size column shows cumulative downloaded bytes and the total when available; estimates use `≈`. Completed tasks show final output size. The task list starts with no selection, and background progress updates do not select tasks. Use Shift for ranges, Ctrl to toggle tasks, or Ctrl+A to select the current filtered list. The list header also provides Select all and Delete selected. Batch deletion confirms whether to delete managed media and keeps failed items available for retry.

High-frame-rate results use labels such as `1080p60` and `4K60`. The compact label rounds `59.94 fps` to `60`; details keep the precise frame rate. Missing frame rates are not guessed. Task lists and the media library share thumbnail styling and context menus. Failed or cancelled tasks show their final state while retaining the previous workflow position in task data and logs. The status bar sums all downloading tasks regardless of list filters.

View logs records original yt-dlp arguments, stdout, stderr and progress without redaction or an entry-count cutoff. The window reads pages, refreshes automatically and can copy the complete log. Earlier unrecorded output cannot be reconstructed. Logs may contain cookies, authentication data, local paths and temporary media URLs; check them before sharing. Complete raw logs are available only through the authenticated task-log endpoint and are excluded from ordinary snapshots and MCP tool results.

Finished live recordings match the regular-video filter. Live and upcoming streams still follow the plan's live filter; All content allows both, while waiting for a scheduled stream remains a separate option.

Version checks first query GitHub's latest-release API. If the API is rate limited or unavailable, the app checks the official project release-page redirect instead. If both fail, it shows the specific reason and allows a manual retry.

The delete confirmation remembers **Also delete media files managed by this task**, even when deletion is cancelled. It is unchecked on a fresh install, and deletion still requires confirmation. Download options, language, theme, notifications, proxy and AI configuration are written to disk with **Save settings**; quick options use **Save batch plan**. MCP startup and service configuration, the access password, cookies and browser preferences are saved by their respective successful operations. Temporary home-page quality, list filters, tool installation parameters, cookie import sources and unsubmitted cover edits are not automatically saved as global settings.

Sign-in persists in separate profiles under the app data directory's `login-browser` folder. Login browsers use system network settings. Each profile can contain multiple sites, and saving exports that profile's cookies. Deleting a saved cookie entry does not sign the browser out; sign out on the website when needed. Browser profiles contain login information and should be kept private.

On Windows, run `node scripts/check-login-browser.mjs` for the native regression test (requires WebView2 and Edge / Chrome). It uses isolated temporary profiles and local test pages to check cookie scopes, popups, persistence and IPC isolation, without personal browser profiles or real accounts.

## Docker web host

Create a directory and save the following as `compose.yaml`. No repository clone is needed:

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

`./data` and `./downloads` are relative to the directory containing `compose.yaml`. Create them there before starting:

```sh
mkdir -p data downloads
```

On Linux/NAS, the container runs as UID/GID `10001:10001` and needs write access to both directories. For newly created directories, run the command below. Handle existing directories according to their current permissions rather than recursively changing user data. Windows Docker Desktop does not need `chown`.

```sh
sudo chown 10001:10001 data downloads
```

Then start the service:

```sh
docker compose pull
docker compose up -d
```

The example uses `latest` to follow the latest stable release. To update, run the same two commands in this directory and keep `./data` and `./downloads`.

Open [http://localhost:19491](http://localhost:19491). For NAS or remote access, set `HUIFA_PUBLIC_ORIGIN` to the complete origin used by the browser (scheme, host and port), such as `http://192.168.1.10:19491`. With an HTTPS reverse proxy, use `https://download.example.com` without a path. Update this origin when changing the published host port. The image supports amd64 and ARM64 and selects the matching architecture automatically.

The initial web account and password are both `admin`. Change the shared web/MCP password under **Settings → Access password** by entering the current and new password. A successful change invalidates old web and MCP sessions; sign in again and update AI clients. Desktop has no added login screen and uses the same settings for its MCP password. `HUIFA_ACCOUNT` and `HUIFA_PASSWORD` seed a new data directory only; later restarts use the password hash in `access.json`. Preserve the `data` directory and change the default before allowing access from other devices.

`data` stores configuration, Cookies and the access password; `downloads` stores media. Stop the container before backing up both directories and `compose.yaml`. To upgrade, edit the `image` version in the original Compose directory and rerun `pull` and `up -d` above. Keep both local directories and their mount paths to reuse existing data; there is no need to delete containers or data first.

If you already use the `rust-data`/`rust-downloads` named volumes, keep the original mount configuration or migrate the data into local directories before switching. Changing mounts does not migrate existing volumes automatically. Do not run `docker compose down -v`, which deletes named volumes referenced by the configuration. See the [Docker guide](docker/README.md) for container hardening, remote browser login, directory permissions and multi-architecture builds.

## MCP: use yt-dlp from your AI client

After signing in, open **AI 媒体服务（MCP）** in the sidebar and enable the single switch. Desktop listens on `127.0.0.1:19492/mcp`. In Docker, `/mcp` is the management page and `/mcp-server` is the protocol endpoint on the same origin and published port:

```json
{
  "mcpServers": {
    "huifa": {
      "url": "http://localhost:19491/mcp-server",
      "headers": { "Authorization": "Bearer YOUR_ACCESS_PASSWORD" }
    }
  }
}
```

The page generates the address for the current origin. Replace `YOUR_ACCESS_PASSWORD` with the access password (`admin` initially). The service switch takes effect immediately; the independent **Start MCP when the app starts** option controls the next launch, defaults off for new installations and inherits the prior enabled state for existing installations. Management actions retain web session and CSRF checks. MCP inherits saved Cookies, proxy and download settings, and returns sanitized metadata, task state and errors without Cookie contents, proxy passwords, internal paths or signed media URLs. Give the endpoint and password only to trusted AI clients. Desktop also offers stdio configuration using the bundled `huifa-mcp-bridge`; set the same password in `HUIFA_MCP_PASSWORD`, with no separate Python or Node install.

See [MCP usage](docs/MCP.md) for the complete tool list and schemas.

## Development and releases

Install Node.js 24, Rust stable and the platform's Tauri build dependencies:

```sh
npm ci --prefix frontend
node scripts/acquire-rust-tools.mjs
npm --prefix frontend run tauri -- dev
```

See [development and release instructions](docs/DEVELOPMENT.md) for checks, MCP bridge preparation and GitHub publishing. The current Rust implementation is the source of truth for builds and releases.

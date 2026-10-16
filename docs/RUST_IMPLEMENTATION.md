# Rust / Tauri / React implementation contract

The approved target is one Rust business core, a Tauri desktop application for
Windows x64 and Linux x64, and an authenticated Docker web application on port
19491 for amd64 and arm64. Both hosts serve the same React build. Publishing
videos and publishing accounts have no registered commands or background jobs.
The existing Python/Electron implementation is a behavior reference, kept in
Git as the pre-migration baseline; the new runtime never imports it.

The new identity is `com.huifa.downloader.rust`; the data directory is
`HuifaDownloaderRust`, never an old application directory. Docker uses `/data`
and `/downloads`. Initial development version is 0.2.0; a release's version is
provided explicitly to GitHub Actions. No version is released implicitly.

## Shared command boundary

`huifa_core::App` opens from explicit data/download/tool paths and is held as an
`Arc<App>`. It exposes `command(&self, command: &str, args: serde_json::Value)`
as an async function returning `anyhow::Result<Value>`, `subscribe()` returning
a Tokio broadcast receiver, and an async `shutdown()` that awaits supervised
process termination and durable task state. Core initialization must not mutate
old application data. The core owns all media and task operations. Host file
dialogs and environment capabilities remain in their hosts.

Commands use `{command, args}`. Web `POST /api/command` returns `{value}` or a
non-success status with `{error}`. Tauri's `command` invoke returns the same
value, with failures returned as a string. `snapshot` returns
`{version, tasks, media, settings, tools}`. Core events use
`{kind, data}`: `task.upsert`, `task.remove`, `media.upsert`, `media.remove`, `settings.updated`,
`tools.updated`. Removal events carry `{id}`. Deleting a task without deleting its
files retains its media record. SSE uses the default message event; Tauri emits `huifa-event`.
DTO fields use camelCase. View state and unsaved form inputs stay in React.

Core commands:

- `tasks.add` `{urls: string[], options: DownloadOptions}` -> task IDs.
- `tasks.pause`, `tasks.resume`, `tasks.cancel`, `tasks.retry`, `tasks.delete`
  `{id: string}`; delete additionally accepts `deleteFiles: boolean`.
- `tasks.pauseAll`, `tasks.resumeAll`; `tasks.convert` `{id, encoder}`.
- `probe.video` `{url, options}` -> metadata and formats;
  `probe.collection` `{url, options}` -> entries for client selection.
- `settings.save` `{settings}` -> sanitized settings.
- `tools.check` returns local component versions and the yt-dlp package selected for the host platform/architecture. It never contacts release servers.
- `tools.latest` `{name:"yt-dlp", channel?:"stable"|"nightly"}` checks the official GitHub release redirect and returns `{name, channel, current, latest, updateAvailable, platform, asset}` without installing. Available on both hosts; it does not use the GitHub API quota.
- `tools.update` `{name, channel?, version?, downloadSource?}` is desktop-only. Only yt-dlp accepts `channel: stable | nightly` (default stable), an optional exact official release tag (blank means latest), and `downloadSource: auto | github | cdn` (default auto). Auto tries GitHub, then the fixed third-party CDN `ghfast.top`. Other components keep their official download path. Docker tools update with the image.
- `cookies.import` `{text, name}`, `cookies.list`, `cookies.delete` `{id}`;
  `cookies.browser` `{browser, profile?}` for desktop browser reading.
- `cover.preview`, `cover.export` `{mediaId, ...editOptions}`;
  `cover.generate` `{mediaId, prompt}` -> persistent image asset.
- `media.list`, `media.file` `{id, kind?}` -> trusted, owned path for host use.

Task DTO minimum: `{id, url, title, status, stage, progress, stageProgress,
stageIndeterminate, speed, eta, error, createdAt, options, outputPath}`.
`stageProgress` is nullable; statuses include queued, probing, downloading,
waiting_processing, processing, processing_failed, paused, cancelled, failed,
completed. A completed task is only published after validated output and its
database transaction. Inputs, plans and publication journal are durable. Retry
of processing_failed tasks uses saved local inputs without running yt-dlp.

Video quality supports `best` (highest available resolution), `worst` (lowest
available resolution), and the existing resolution caps. Storyboards are excluded;
audio is matched automatically. Choosing a quality preset clears exact format IDs
and exits per-task manual selection, while existing queued plans remain frozen.
The download-page selector applies to subsequent batches in the current session;
Quick options and Settings save defaults for future sessions.

Runtime updates apply to newly added tasks; retry retains the original frozen tools
and credentials. The selected yt-dlp release tag is resolved before downloading;
GitHub and CDN requests target that same exact tag. SHA-256 checksums are always
fetched from the official GitHub release, independently of the CDN. If trusted
checksums cannot be retrieved, installation stops. Remote checks and yt-dlp
checksum downloads do not depend on the unauthenticated GitHub API quota.
Native execution and required CLI options are checked before switching the current
tool pointer. Failed installation leaves that pointer unchanged.

Windows bundles use the GPL shared FFmpeg build from BtbN/FFmpeg-Builds. FFmpeg,
FFprobe and all seven libav/swresample/swscale DLLs travel together in installers,
portable packages, managed updates and frozen task snapshots. DLL bytes are hashed
before task execution, including rejection of unlisted DLLs. Linux desktop builds
keep static FFmpeg; container FFmpeg comes from Debian. New bundles use QuickJS-NG
for yt-dlp's EJS scripts; existing frozen Deno tasks continue using their Deno copy.
Desktop users can optionally install Deno under Tools & updates for faster YouTube
JavaScript parsing. Installed Deno is preferred for new probes and tasks, while
existing QuickJS snapshots retain QuickJS.
Runtime payloads are stored once per SHA-256 under tools/objects. Frozen task sets
hard-link these immutable objects where supported and fall back to copies on other
filesystems. They never link mutable installations, overwrite old task snapshots,
or remove old runtime versions automatically.

The media DTO uses `{id, taskId, title, filename, size, duration}`; snapshots
never expose Cookie text, AI keys or signed media URLs. Public YouTube watch URLs
retain only a validated public video ID; other query parameters are removed. Desktop settings retain
the chosen `processingTempDir`; web responses and events remove it, and web
requests cannot set it. Task DTOs hide it on both hosts.
`media.file` is a host-internal command: web rejects it from public command
requests and resolves `/api/files/:id` through it after authentication.
Hosts may define `capabilities`, upload, download and remote login endpoints;
public web callers never get shell, arbitrary filesystem or SQL access.

## Required checks and delivery

Each implementation unit has meaningful tests. Real local media checks cover
split audio/video, FFmpeg processing, offline restart, publication recovery,
queue budgets and child process cleanup. Web tests cover sessions, CSRF/Origin,
login limits, file ownership, secret redaction and persistent credentials.
React tests cover virtual rows, keyboard navigation and stable form/selection
state. Native desktop and container builds are independently verified.

GitHub Actions accepts an explicit version and Markdown notes, creates one
version commit, builds a shared frontend once, builds desktop + both container
architectures from that commit, and publishes only after checks and signed
desktop update artifacts succeed. Already published versions are immutable.

Commit completed, checked units frequently. Never commit local acceptance
artifacts, credentials, runtime payloads or build output.

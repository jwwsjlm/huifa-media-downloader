# Usage guide

[Project home](../README_EN.md) · [简体中文](USAGE.md) · [Docker deployment](../docker/README.md)

## Add downloads

Paste a single link or a batch into the home page. Automatic quality queues known
single videos directly, including YouTube video links containing playlist parameters.
Unknown links are identified first; actual collections show item selection, while
manual mode opens the format picker. A broken link does not stop the rest of a batch.
You can cancel remaining checks; failed links remain available to adjust and retry.

**Quick options** controls the download plan: video/audio mode, quality, audio tracks,
subtitles, output format, chapters, cuts, live filters and supported site features.
Explicit quality, frame-rate and audio-language requirements must be available;
otherwise the task stops and offers **Adjust requirements and retry**. Automatic
best/worst quality remains automatic rather than promising a particular resolution.

Subtitles are off by default. Choose a language to prefer manual subtitles with
automatic fallback and external SRT output. **Continue when subtitles are unavailable**
defaults on: missing requested languages are skipped while the video continues.
Turn it off to require every requested subtitle language. Fragment concurrency
defaults to 4, recommends 6 and is limited to 8.

**Settings → Downloads → Video analysis threads** automatically allocates up to
8 threads for full analysis, or accepts a manual integer from 1 to 256.
Saving applies to video analyses started afterward; running analyses are unchanged.
Manual threads count toward admission to the shared resource budget. Docker reports the server host's
detected processors rather than the browser device's.

**Download clipboard** extracts and deduplicates links before adding them. On desktop,
**Settings → Downloads → Clipboard monitoring** can automatically add copied HTTP/HTTPS
links while the app runs, including in the background. It is off by default and
takes effect after saving; enabling it also checks the current clipboard.
Collections and manual formats still use the normal confirmation flow.

### Download folders and names

On desktop, choose **Settings → Downloads → Download folder → Choose folder**.
This selection saves immediately and survives restarts. It applies to new downloads
and retries using current settings; queued, running, paused and processing tasks
keep their original folder. Existing files are not moved. The processing temporary
folder is a separate setting and needs enough free space.

Docker uses its `/downloads` mount. The web interface cannot choose folders on the
host computer; change the volume mapping as described in the [Docker guide](../docker/README.md).

The default layout is `downloads/title/title.ext`, without source or task IDs in
the name. Existing historical files and folders are not renamed or moved in bulk.
Adding an already active or paused source selects its existing task. Re-adding a
completed, failed or cancelled download reuses the record with current settings;
an existing result stays in place until a new result has passed validation.

## Compression and format conversion

Open a completed video's context menu and choose **Compress video**. The app checks
the local file and available encoders first. Compression creates a separate task
and saves a new HEVC video in MKV; the original file is preserved.

**Start compression** adds the job to the processing queue and selects the new task.

At most two jobs run together; CPU re-encoding remains single lane. Two NVIDIA jobs
require Windows, at least 8 logical processors, 8 GiB free RAM, 4 GiB free VRAM,
and a successful concurrent encoder probe. High resolution, HDR, 10-bit video, or
unknown resources use serial admission. Full scans and disk verification are limited
separately, preserving priority and FIFO within each priority. Eligible 8-bit SDR
video up to 4K, without scaling or frame-rate reduction, can use NVIDIA hardware
decoding after matching decoded pixels and timestamps; failed checks retain the
selected encoder with software decoding.
The task list and details show progress for source verification, full video analysis
and encoding. Each progress bar represents the current stage. Cancel from the task actions.

| Mode | Controls and limits |
| --- | --- |
| **Simple** | Enter a target size or drag the percentage slider. Keeps source resolution and frame rate. The target cannot exceed the actual source file size. |
| **Advanced** | Set video bitrate and optionally lower resolution or frame rate. Output size is an estimate, not a maximum. |

Size inputs accept `4MB`, `4GB`, `4G`, `MiB` and `GiB`; bare numbers mean MB.
MB/GB use decimal units, while MiB/GiB use binary units. Bitrate accepts `8Mbps`
or `800Kbps`; bare numbers mean Mbps. Simple and Advanced keep their own input
drafts while switching modes. Choosing a fixed frame rate changes a variable-frame-rate
source's timing. Smaller size or bitrate targets reduce picture quality.

Audio tracks, subtitles, attachments, chapters and supported metadata are retained
and checked before publication. A target that leaves too little space for those
streams fails rather than silently removing them. Unsupported features such as
Dolby Vision/dynamic HDR, certain pixel formats or uncopyable streams block compression.
Changed source files or failed preservation checks stop the task and keep the original.

Opening the compression dialog checks encoders for the current video. **GPU encoding
acceleration** is selected by default when a GPU encoder is available; otherwise,
CPU is used and the reason is shown. You can turn GPU acceleration off, and switching
between Simple and Advanced keeps your choice. Changing resolution or other options
rechecks the selected encoder; if unsupported, adjust the options or choose CPU.
GPU encoding failure stops the task; choose **Retry with CPU** explicitly.
Hardware availability alone does not guarantee support for a video's bit depth,
pixel format or HDR metadata.
The bundled macOS CPU x265 encoder currently supports 8-bit output only; paths
that cannot preserve 10-bit video or HDR are disabled with an explanation.

### FFmpeg version policies

Desktop **Settings → Tools & updates** offers **Compatibility** by default and
**Efficiency** for a newer build. Compatibility uses a pinned older build to
support existing hardware and drivers. You can also enter an exact release tag
from the repository displayed for your system and selected policy.

Save a changed policy, then choose **Apply FFmpeg version** to install FFmpeg and
FFprobe together. Saving or checking a release does not replace the installed build.
The component list shows the installed version separately from the selected policy.
Existing tasks keep their frozen tools. For an incompatible NVIDIA driver, first
try the Compatibility build; updating the driver or using CPU are alternatives.
Docker components update with the image. See [runtime updates](RUNTIME_UPDATES.md).

**Convert format** offers MP4, MKV, MOV and WebM, preferring remuxing when possible
and transcoding when necessary. Advanced controls select codecs. Converted files
use a `title [converted]` name with a number for conflicts, and keep the original.

## Task recovery and file safety

Task plans freeze options, cookies, proxy, runtime versions and save locations.
Download and processing queues run separately. **Move to front of queue** persists
the order of pending downloads, live captures and processing without interrupting
running work. Stage prefixes such as `2/6` show major workflow steps, not a fixed
number of steps for every task.

| Action | What it uses |
| --- | --- |
| **Resume / Retry download** | The original task plan and identity; compatible partial transfers can resume. |
| **Retry processing** | Saved local source files when available, without downloading the source again. |
| **Retry with current settings** | Current saved options, cookies, proxy, tools and download folder; available for ordinary downloads. |
| **Adjust requirements and retry** | Your explicit changes to unmet quality, frame-rate, audio or subtitle requirements. |

Changed source or format identities use separate partial files. Transient connection
failures, timeouts, HTTP 429 and server errors have bounded retries with backoff.
Authentication, permission and unavailable-format errors require user action.
The current session summarizes failed/actionable batch items; after restarting,
use the task status filters to find them.

### Existing results and missing files

Different sources using the same filename prompt before transfer. Rename, skip,
or explicitly confirm replacement twice. A validated replacement updates the result,
managed attachments and prior task ownership. Cancellation or failure preserves
existing files. Historical folders are not moved in bulk; cleanup only removes old
managed files and empty task-ID directories after a successful replacement.

Completed lists check whether local results are still available. **Source file missing**
means the file was not found; **File temporarily inaccessible** means its availability
could not be established, for example when storage or the service is unavailable.
File actions and processing are disabled in those states. Restoring access does not
require downloading again; refresh the library or return to it to recheck.

Task deletion asks whether to also delete managed media. That choice is remembered
even if you cancel the dialog, starts unchecked on a fresh installation, and still
requires confirmation for every deletion. Failed batch deletions remain available
for retry. Conversion and compression preserve their input files.

### Live captures, storage and validation

**Stop live capture and save** validates and publishes the recorded part. **Cancel**
does not publish a result. Data not yet written when stopping may be lost, and
pausing a live capture can create a time gap. Finished recordings count as regular
videos; live/upcoming streams follow the live filter. **All content** permits both,
while waiting for scheduled streams is a separate option.

Free-space checks cover data, processing and output volumes, but unknown source
sizes cannot be fully estimated. Successful tasks clean source and processing caches
while keeping results, previews and logs. Unfinished tasks retain recovery files.
**Storage and cache** reports free space and safely cleans completed caches and
unreferenced runtimes; do not manually remove recovery files from active tasks.

Non-live downloads compare expected and actual duration. Processed outputs also
check required streams, duration and existing hashes. These checks do not perform
a complete frame-by-frame decode or prove that the website's source is undamaged.

## Website sign-in and proxies

Cookies are optional for sites that allow anonymous access. Without a matching
record, media queries and downloads run anonymously. Under **Settings → Network &
cookies**, enter the website directly and sign in once. New tasks automatically
match cookies to the destination domain; existing tasks retain their frozen identity.

| Host | Sign-in options |
| --- | --- |
| Windows / Linux desktop | Built-in browser or compatible local Chrome, Edge or Chromium. |
| macOS desktop | Installed Chrome, Edge or Chromium in `/Applications` or `~/Applications`; Safari is not used. |
| Docker | Disposable remote Chromium session, or import a Netscape cookie file. |

Desktop cookies sync during sign-in and once more before closing; no save click is
needed. Sync failures show an error and preserve previously saved cookies. Use a
local browser for sites such as Google that reject embedded sign-in. The last
successful address and browser choice are saved automatically. **Other import methods**
provides cookie-file import without exposing saved cookie records or account names.
For Docker's explicit save/cleanup flow and login restrictions, see the
[Docker guide](../docker/README.md#container-and-browser-login).

The built-in Windows/Linux browser has address, Back, Forward and Reload controls;
Enter navigates, and an omitted protocol defaults to HTTPS. Web pages and login
popups cannot call application commands.

Desktop sign-in browsers share the download proxy. Close and reopen browser windows
after changing it, and save settings to retain it across restarts. An empty proxy
selects direct access. Sign-in browsers require a proxy URL without embedded login
credentials; use a local proxy endpoint if needed. Release checks, component
downloads and application updates also use the saved download proxy.

Desktop sign-in profiles persist under `login-browser` in the application data
directory. They can contain multiple websites' login information independently of
exported cookies. Sign out on the website to end that browser login, and keep these
profiles private. Docker's remote session is disposable rather than a persistent
desktop browser profile.

## Settings, interface and logs

**Save settings** writes download options, language, theme, notifications, clipboard
monitoring, FFmpeg policy, proxy and AI configuration to disk. **Save batch plan**
saves Quick options. The download folder saves immediately; cookies, browser
preferences, access password and MCP configuration save through their respective
successful operations. Temporary home-page quality, filters, installation tags,
cookie import sources and unsubmitted cover edits are not global saved settings.

AI cover settings can fetch model IDs from the configured service or accept a
manual ID. Cover creation includes JPEG crop/padding export and AI editing based
on the original image; API support depends on the configured service.

Completed items show measured quality and final file size. Details distinguish
downloaded-source information from output information. A **Highest quality** plan
does not guarantee a 4K source. Labels such as `1080p60` round 59.94 fps for display;
details keep precise values, and unknown frame rates are not guessed. Older entries
reuse saved data and inspect existing files when necessary, without redownloading.
During downloads, size means cumulative downloaded bytes and an available total;
`≈` marks an estimate. The status bar sums active downloads regardless of filters.

Completion times use local time. Under **Settings → Appearance & covers**, customize
the format, for example `YYYY-MM-DD HH:mm` or `YYMMDD HH:mm`; `MM` means month and
`mm` means minute. Older tasks use recorded completion events when available.

Use Shift for ranges, Ctrl to toggle tasks and Ctrl+A for the current filtered list.
The header also has **Select all** and **Delete selected**. Progress updates do not
select tasks. Task menus can copy titles and sources; desktop results also offer
the full file path, including its filename.

**View logs** contains original yt-dlp arguments, stdout, stderr and progress without
redaction or an entry-count cutoff. It loads pages, refreshes automatically and can
copy the complete log; output never recorded by an older version cannot be recovered.
**Logs can contain cookies, authentication data, local paths and temporary media
URLs. Review them before sharing.** Raw logs require the authenticated task-log
endpoint and are excluded from normal snapshots and MCP tool results.

## Data and backups

The application ID is `com.huifa.downloader.rust`. Desktop state lives in
`HuifaDownloaderRust` under the system's local application-data directory; the
default media folder is under the user's Downloads directory. Installer and
portable Windows editions share these folders. Docker uses `/data` and `/downloads`;
back up their actual host mounts and the Compose configuration.

**Cookies, proxy settings and AI keys are plaintext files in `credentials`.**
Protect the data directory and its backups. Older encrypted records migrate at
startup; keep their original `.credentials.key` until migration completes. The key
is removed only after a successful migration. Browser profiles also contain login data.

Version changes, legacy credential migration and desktop installer updates create
a consistent database/configuration/credential backup under `backups`, including
checksums and `RESTORE.txt`. Backup failure stops migration or installation.
**These automatic backups exclude media, runtimes, task workspaces and browser
profiles.** Restored tasks still need their original inputs and frozen tool copies.

For a full backup, stop the app/container and preserve the entire data directory,
media directories, any custom processing locations and deployment configuration.
Before restoring, keep a separate copy of current data, close the app/container,
and follow the backup's `RESTORE.txt`. Do not restore over a running database.

Docker mount changes do not migrate old named volumes. Keep the original mounts
until migration is complete, and do not use `docker compose down -v` on data you
want to retain. See [Docker storage and upgrades](../docker/README.md).

## Troubleshooting

| Symptom | Next step |
| --- | --- |
| Explicit quality, frame rate, audio or subtitle requirements are unavailable | Use **Adjust requirements and retry**; change only the requirements you accept relaxing. |
| GPU encoding is unavailable or fails | Apply the Compatibility FFmpeg build, check the driver and source restrictions, or use **Retry with CPU**. |
| Result file missing or temporarily inaccessible | Check whether it was moved, deleted or stored on an unavailable volume; restore access and refresh. |
| Download timeout, SSL/EOF or connection error | Check connectivity and the download proxy; retry when the source is reachable. |
| Website needs fresh cookies or sign-in | Reopen the target website in the sign-in browser and allow cookies to sync; then retry with current settings. |
| Component check/update fails | A failed check does not mean up to date. Check the exact tag, selected policy and network; see [runtime updates](RUNTIME_UPDATES.md). |
| GitHub rejects component requests with 403/429 | Wait and retry. Source-site cookies do not resolve GitHub rate limits. |
| Insufficient space or unwritable folder | Free space or choose a writable download/processing folder; keep unfinished task files for recovery. |

### Windows startup failures

Startup errors display the location of `startup-error.log`. If the app exits
without a message, download [diagnose-windows-startup.ps1](../scripts/diagnose-windows-startup.ps1)
and run it in PowerShell:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\diagnose-windows-startup.ps1
```

Enter the application EXE path when asked. The generated `startup-report.txt`
contains system, application and WebView2 versions, exit status and matching error
events, without reading cookies or application configuration. Review it before
attaching it to an [issue](https://github.com/jwwsjlm/huifa-media-downloader/issues).

### Website extraction and application updates

Some sites reject headless/embedded login or require device verification. Signing
in cannot fix every extractor failure. Douyin can report `Fresh cookies (not
necessarily logged in)` for an API 403 even with saved cookies. The app includes
a temporary compatibility fix based on [yt-dlp PR #17826](https://github.com/yt-dlp/yt-dlp/pull/17826)
while waiting for the bundled upstream engine to include it. Website changes can
still break extraction; do not treat an extractor's presence as a site availability guarantee.

The sidebar indicates available application releases; **About** can check again.
Checks try the official GitHub API and then the release-page redirect. Failure
shows a reason and allows retry. Portable Windows builds update by closing the
app and replacing the extracted program directory; they do not launch an installer.
Container application and component updates require pulling and recreating the image,
with existing storage preserved. See [installation](../README_EN.md#download-and-install)
and the [Docker guide](../docker/README.md#released-image).

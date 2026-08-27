# 开发与发布说明

> [返回中文首页](../README.md) · [English README](../README_EN.md)

本文档保留项目的详细运行时、构建、数据目录和自动更新设计说明。面向普通用户的安装与功能介绍请查看首页 README。

Windows 10/11 desktop application for downloading videos and playlists from
the sites supported by `yt-dlp`, then preparing multi-platform publishing tasks.

## Quick start

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m app.main
```

## Windows 发行版

官方仓库为 `jwwsjlm/huifa-media-downloader`。推送与 `APP_VERSION` 一致的
`v<版本号>` 标签后，GitHub Actions 会在 `windows-latest` 上安装固定构建依赖，
从 `yt-dlp/FFmpeg-Builds` 和 Playwright 官方源准备发行运行时，执行完整测试、
构建及打包烟雾检查，然后生成便携版 ZIP、安装包 ZIP、两种更新机制所需的技术资产和
`SHA256SUMS.txt`。应用默认通过 GitHub REST API
`releases/latest` 检查稳定版，并使用 Release 资产中由 GitHub 提供的 SHA-256
digest 验证下载内容；启用预发布更新时才读取 Release 列表。

发布示例：

```powershell
git tag v0.1.1
git push origin v0.1.1
```

便携版 ZIP 包含单文件 `HuifaVideoDownloader.exe`，解压后可直接运行；安装包 ZIP 包含
Velopack `Setup.exe`，由安装程序管理应用目录。PySide6/Qt 是本程序界面运行时，不能从程序内部删除；内置 yt-dlp 模块和 FFmpeg
会随 EXE 提供，用户无需另装 Python 或 PySide6。程序也支持从官方 Release 自动安装并优先调用
可独立更新的外置 `yt-dlp.exe`，内置模块保留为安全回退。单文件首次启动会将运行库临时
解包，这是正常行为。Deno 不是程序启动的硬依赖，但它是 yt-dlp 推荐的 JavaScript
运行时，用于 `yt-dlp-ejs` 的 YouTube 格式解析。程序数据默认保存在 EXE 同目录的 `data/`，不会写入当前命令行目录。
程序不再打包或启动 QtWebEngine，也不会调用用户电脑中安装的 Chrome/Edge。登录、Cookie
检查和发布统一使用软件本地 `social-auto-upload` 源码与官方 Playwright 管理的一套 Chromium。
发行版会把 Python 运行时、Playwright、SAU 源码快照和对应 Chromium 一起打包；
用户无需预装 Python、Chrome 或 SAU。
SAU 登录状态持久保存在 `data/browser/sau-cookies/cookies/`，核心更新不会清除 Cookie；下载用 Cookie
另存为 Windows DPAPI 加密副本。Bilibili 同样通过软件内置 Playwright Chromium 登录，并在本地生成
biliup 所需的专用账号文件。
首次运行若触发 Windows Defender/SmartScreen，请确认文件来源后选择允许。

开发者可使用 `scripts/build_release.ps1` 重建单文件便携核心；脚本会先运行编译检查和单元测试，
再生成 `releases/HuifaVideoDownloader.exe`。该脚本本身不创建 ZIP，GitHub Release 的统一封装由
`scripts/package_github_release.ps1` 完成。
每次构建使用独立的 `build/single-exe-dist-<运行编号>/` 暂存目录，只替换最终 EXE；如果 `releases/data/` 中已有本地
数据库和设置，脚本会保留它们，但这些运行数据不是交付附件。成功替换后会自动删除
PyInstaller 工作目录和暂存副本，避免保留重复的大文件。发布前脚本还会在隔离目录真实启动
刚生成的 EXE，由程序创建完整主窗口并输出烟雾测试报告，确认 PySide6、内置 yt-dlp 下载核心和
单 EXE 自动更新模式均可加载后才替换正式发行文件；该测试不会读取或修改 `releases/data/`。
最后还会执行单文件核心目录校验：`releases/` 顶层文件必须只有非空的
`HuifaVideoDownloader.exe`；`releases/data/` 等本地运行目录不参与交付，也不会被构建脚本删除。

单 EXE 不使用 Velopack，因为 Velopack 的更新单位是完整的 onedir 应用目录。便携版使用
独立的 GitHub Release 单 EXE 更新流程：查找名称严格为 `HuifaVideoDownloader.exe` 的资产，
下载到 `data/updates/application/`，按 GitHub 资产 `digest` 强制校验 SHA-256，用户确认后
安全停止任务，退出主程序，再由临时 PowerShell 替换器备份、替换、复验并自动重启；失败时
恢复旧 EXE。替换器会原子写入成功或失败回执；新进程启动后核对实际运行版本与目标版本，
只提示一次真实安装结果，避免把“已下载”误认为“已安装”。安装版使用 Velopack 的 feed、
完整包和受管目录更新；二者共享检查、下载、更新内容、确认界面和安装结果回执，但使用各自适合的替换机制。

单 EXE 的大文件下载支持标准 HTTP `Range` 断点续传。网络中断或程序安全退出时会保留
`.part` 与仅包含版本、大小、GitHub URL、SHA-256 和 HTTP 校验标识的恢复记录；再次下载时
使用 `Range`/`If-Range` 继续。若 CDN 忽略范围、ETag/Last-Modified 变化或返回矛盾的
`Content-Range`，程序会清除旧断点并安全地从零下载，不会拼接不同版本。无论是否续传，
最终文件都必须再次通过完整大小、GitHub SHA-256、MZ/PE 签名验证后才会进入安装确认。
构建 Velopack 安装版前可先运行
`.\scripts\build_velopack_release.ps1 -Version <APP_VERSION> -ValidateEnvironmentOnly`；脚本会优先
选择 64 位 Program Files 中真正安装了 SDK 的 dotnet，而不是误用 PATH 中仅有 Runtime 的
x86 host。稳定版使用 `win` 通道，预发布版需显式指定例如 `win-beta`，构建完成后脚本还会
校验安装器、便携包、更新 feed 和完整更新包，不会执行 GitHub 上传。

The x64 release embeds only the FFmpeg runtime under `tools/ffmpeg/x64/`;
legacy root and x86 copies are not added to the deliverable. For the primary
single-EXE build, external portable tools are resolved in this order:

1. files beside `HuifaVideoDownloader.exe` (`yt-dlp.exe`, `ffmpeg.exe`, `deno.exe`, `sau.exe`),
   followed by their documented subdirectories under that same EXE folder;
2. an explicit valid path saved in Settings (and `YT_DLP_DENO_PATH` for Deno);
3. embedded or persistent runtime roots;
4. system `PATH`.

This makes copying a portable runtime beside the EXE the highest-priority,
immediately effective override. Temporary PyInstaller `_MEI` paths are never
saved as preferences. A standalone `yt-dlp.exe` is detected from the EXE
directory (and its documented `tools/` subdirectories) for diagnostics,
including its reported version, so a portable local installation is no longer
shown as missing. A usable standalone executable is the active download core;
the Python `yt_dlp` module embedded in the main EXE is retained as a fallback.
PySide6 is likewise required
internally by the GUI but is already embedded in the single EXE; users do not
install it separately.

If Deno is installed, the downloader automatically enables yt-dlp's EJS
challenge solver (including the official remote EJS component) for sites that
need JavaScript-based format extraction. Without Deno, the app still works
for other extractors but some YouTube formats may be unavailable.

The Settings page can inspect the latest GitHub releases/tags for `yt-dlp`,
the yt-dlp maintained `FFmpeg-Builds`, Deno and `social-auto-upload`. PySide6 is an internal bundled GUI
runtime and is therefore not shown as a separately installable component. A
usable external `yt-dlp.exe` is preferred by the actual download worker and can
be downloaded, atomically installed and updated from the official yt-dlp
GitHub Release. If no usable executable is present, the bundled Python module
is used as a fallback. Tool
version detection for FFmpeg, Deno and `sau` first checks files beside
`HuifaVideoDownloader.exe`, then their documented `tools/` subdirectories and
system `PATH`. The in-app updater supports both release families: Velopack
manages installer/onedir portable builds, while the primary single-EXE build
downloads the exact `HuifaVideoDownloader.exe` GitHub Release asset, requires
its SHA-256 digest, and replaces it only after clean shutdown and explicit
confirmation. Both paths persist the confirmed source/target versions and show
a one-time post-restart success or failure result after verifying the version
that is actually running.

Component checks persist a bounded `data/update-component-cache.json` file.
GitHub `ETag`/`Last-Modified` validators are sent on the next check, and a
`304 Not Modified` response reuses only the matching repository payload. The
cache is written atomically, ignores corrupt entries, expires its timestamps
after six hours, and never stores `GITHUB_TOKEN` or request headers. A network
failure is still shown as a failed check rather than silently claiming that an
old cached release is current.

For supported upstream assets, the tool-update dialog can download and
install yt-dlp, FFmpeg/FFprobe, Deno, yt-dlp-ejs and social-auto-upload without blocking the UI. Direct EXE files are
atomically replaced; ZIP packages are inspected and only the required runtime
files are installed (`yt-dlp.exe`, `ffmpeg.exe` + `ffprobe.exe`, or `deno.exe`).
The application vendors one fixed `social-auto-upload` source snapshot and runs it
in-process through the packaged Python runtime. Official Playwright and its matching
app-local Chromium are packaged with the application; end users do not install Python
or a browser separately. The full archive, documentation, examples, `ffplay.exe` and
development libraries are not retained. Existing files are backed up during replacement
and restored on failure. Release-provided SHA-256
digests are enforced; when a publisher has not supplied one, the user must
explicitly confirm before installation. A pinned SAU source snapshot may also use a configured
GitHub proxy when direct GitHub access is unavailable; the installer still validates the archive structure
before replacing the existing core. Portable builds prefer the EXE
directory; Velopack-managed installations keep downloaded tools in persistent
`data/tools/`.

Closing the main window is cooperative and non-blocking: downloads are marked
paused, publish/login/update workers receive cancellation requests, and the UI
continues processing events while waiting for their threads to finish. The
database is closed only after workers have stopped, avoiding frozen shutdowns,
stale background processes and Qt thread-destruction crashes.

When enabled in `设置 → 通知与体验`, the application uses Qt's native system
tray notification API to report completed or failed download/publish tasks only
while the main window is in the background. Clicking a notification restores
the window and opens the relevant download task or publishing queue row. The
task card, queue result and logs remain the source of truth because operating
systems may suppress notifications. This feature does not implement
close-to-tray: closing the main window still performs the normal cooperative
shutdown and removes the tray icon before the process exits.

## Portable data directory

Runtime data is stored beside the application under `data/`:

- `data/app.db` — download, media and publishing task database;
- `data/backups/app.backup-1.db` … `app.backup-3.db` — rotating, transactionally consistent SQLite snapshots;
- `data/recovery/database-*` — damaged database files isolated during automatic recovery; they are preserved for support instead of deleted;
- `data/settings.ini` — download directory, quality, download-only proxy and tool settings;
- `data/browser/cookies/` — 下载功能使用的 Windows DPAPI 加密 Cookie 副本；
- `data/browser/sau-cookies/` — SAU/Playwright Chromium Profile 与平台账号文件，更新核心或重启软件后继续保留；
- `data/downloads/` — default download directory.
- `data/logs/downloads/` — per-task diagnostic logs in JSON Lines format;

On startup, an existing database is checked with SQLite `quick_check`. A damaged
database and its WAL/journal sidecars are moved together into `data/recovery/`,
then the newest healthy snapshot is restored. If no snapshot is usable, the app
starts with a new database and clearly notifies the user; the damaged original
is never silently overwritten. Missing `app.db` remains an intentional reset and
does not resurrect an old backup. Backups are created atomically, rotated to
three generations, refreshed after recovery, and updated again on a clean exit
when database state changed.

The Settings page is the single place for selecting the download directory and
the defaults for quality, playlist handling, filename template, concurrency
and request pacing. `并行下载数` controls how many tasks can run at once;
`单任务分片并发` controls concurrent DASH/HLS fragments within one task.
智能模式最多使用 8 路以降低平台风控概率；手动模式允许用户输入更高值。Progressive single-file streams may not benefit
from fragment concurrency, and excessive values can trigger throttling. If the
speed is still low, reduce `并行下载数` to 1–2 so several tasks do not share the
same connection bandwidth.
The download page keeps those values read-only: a compact `智能下载方案` bar
shows the quality, playlist policy, task concurrency, fragment concurrency and
whether proxy/Cookie support is active, while `修改下载参数` jumps to the one
Settings source of truth. The page also provides `粘贴并下载` (Ctrl+Shift+V)
and accepts up to 100 ordered, de-duplicated HTTP(S) links from plain text,
Markdown or angle-bracket clipboard content. Equivalent queued, running or
paused work is not created twice. The directory remains read-only with
shortcuts to open it or jump to Settings, so the two pages cannot drift out of
sync. Settings are grouped into download, network and tool sections, and
invalid or unwritable download paths are rejected with an explicit message.
The completed-media catalog opens with a bounded first page and fetches older
media only when requested; global search and distribution filters deliberately
materialize the remaining catalog only after the user asks for them. Summary
counts are reduced in SQLite, so a large media library does not block the first
screen on thumbnail/card construction.
The publishing queue follows the same bounded-page model: recent publish tasks
appear first, older rows load on demand, and a text search expands the query
scope only when needed. Live status signals update an already visible row
without rebuilding the entire queue.
The `设置 → 外观` section provides `跟随系统`, `浅色` and `深色` themes;
the system option follows Qt/Windows color-scheme changes while the program is
running and the choice is stored in `data/settings.ini`.

“下载环境”按钮执行纯本地预检，不会访问 GitHub 或视频站点：它会显示实际使用的
外置 yt-dlp 或内置回退模块、FFmpeg/FFprobe、推荐的 Deno、下载目录和下载 Cookie 状态。若下载核心或
FFmpeg/FFprobe、已配置的 Cookie/目录不可用，粘贴链接后不会创建必然失败的任务，并会提示打开该检查页。
这个预检只解析运行路径并读取 Windows EXE 文件头，不会在界面线程执行外部工具的
`--version`，所以损坏或响应缓慢的用户自定义工具不会让窗口假卡；完整版本检测仍在
“检查运行组件”的后台线程中完成。诊断包同样使用快速路径解析，不会因外部命令阻塞导出界面。

Parallel workers share one per-volume disk reservation manager. Before yt-dlp
opens the selected media files, the application conservatively reserves the
estimated download, merge and final-output peak while preserving a 1 GiB
physical free-space floor. Tasks targeting the same disk wait instead of
overcommitting it; the task card explicitly shows `等待其他下载任务释放磁盘空间`
and continues automatically after the earlier task releases its reservation.
Different volumes remain independent. Unknown-size media runs exclusively on
its target volume and is monitored during progress; physical free space is
rechecked at least every two seconds or 64 MiB and again before FFmpeg
post-processing. Pause, cancel, delete and application shutdown all interrupt a
capacity wait without leaking the reservation.

The proxy setting is used only by the downloader. Publishing commands are
started with proxy environment variables removed, so a download proxy cannot
silently affect platform publishing.

Publishing uses the vendored `social-auto-upload` source directly inside the
packaged Python runtime. The per-platform `登录` button starts the same app-local
Playwright Chromium used for account validation and publishing; there is no
QtWebEngine/CDP bridge, external `sau.exe`, terminal login, or dependency on a
user-installed browser. Bilibili Web Cookies are exchanged for the dedicated
biliup credential format before the account file is saved locally.
After the cookie synchronization, the application immediately runs the matching cookie check. It
also checks the same platform/account again immediately before every upload;
missing or expired cookies stop the task with an actionable re-login message.
SAU's required Playwright state stays under `data/browser/sau-cookies/`; the download profile also keeps
a DPAPI-encrypted copy under `data/browser/cookies/`. Cookie values are never copied
into the downloader database, settings, diagnostics or logs. Platform creator
login and publishing always reuse the software-managed Chromium, so they never depend on the user's
default browser or a separately installed Chrome/Edge.

The download page includes a `支持站点` dialog showing the extractors shipped
with the installed yt-dlp build, so the UI is not limited to YouTube. The
download task list supports title/URL/ID search, sorting by time/title/status,
global pause/resume, completed-record cleanup, and a `查看下载日志` action. Each task log records
解析、获取格式、视频下载、音频下载、合并、封面、元数据、校验、完成、重连、
进度和网络/代理错误 without storing cookies or tokens. The task card shows the
current stage, full pipeline, video/audio sub-progress, speed, ETA, elapsed time
and reconnect countdown. The log dialog also provides an initial diagnosis
category so network failures can be distinguished from rate limits, login,
CAPTCHA, format, and tool errors.

Each task card also shows a source-platform badge (for example YouTube,
Bilibili, 抖音 or other supported hosts) with a tooltip containing the full
source URL. The badge is derived from the URL at display time and does not add
or change any database field.

The Settings page also provides `打开日志目录` and `导出诊断包`. The exported
ZIP contains redacted task logs and a runtime summary for troubleshooting;
cookies, tokens, authorization headers and URL query parameters are excluded.

封面设置统一控制完成列表和封面工作室的默认输出：横版 16:9、竖版 9:16 或
方形 1:1，裁剪/留白方式、水平与垂直裁剪焦点以及 JPG 质量。完成列表的“复制封面”
和“另存封面”都会使用同一套设置，不再一个复制原图、另一个输出预设尺寸。封面工作室
可以临时微调焦点；AI 二创结果不会覆盖下载的原封面，尚未另存时关闭、恢复原图或继续
生成都会先提示确认，避免误丢失生成结果。

Transient network failures during解析 or下载 are retried up to two times with
exponential backoff. Rate-limit, CAPTCHA, login and other風控类 errors are not
blindly retried, which avoids making platform risk controls worse.
The Settings page also exposes a configurable request interval (0–60 seconds)
for cautious downloads; 0 keeps the original no-extra-delay behavior.

The application no longer uses the user's Documents directory for its own
database or settings. Existing data from the legacy `.youtube-release-studio`
location is migrated to `data/app.db` on first launch. The migration is marked
as complete, so deleting `data/app.db` later creates a fresh empty database
instead of restoring the old legacy task records. If the database is deleted
while the app is open, the task view detects the reset and clears its in-memory
task selection after the file disappears.

The first version provides:

- queued downloads with progress, speed and ETA;
- a task dashboard with clickable live overview cards for all/running/queued/paused/completed/problem tasks, grouped download controls, per-task title, URL, thumbnail placeholder, status, progress, speed, ETA, cancel/retry and folder actions;
- a matching completed-media dashboard with clickable distribution filters for pending coverage, successful platforms, active uploads, retryable failures and fully distributed videos;
- persistent download directory, download-only proxy, request pacing, filename template and quality preferences;
- quality presets for best available, 1080p maximum and 720p maximum;
- custom quality mode that parses available formats before download;
- metadata/thumbnail/info.json persistence in SQLite + files;
- an account hub that keeps platform targets, account names and Cookie checks
  in one place, using the app-local SAU/Playwright Chromium for visible login;
- per-platform publishing account names for `sau`, with persistent SAU Cookie
  storage, post-login validation and an automatic pre-upload
  cookie check;
- completed media list with a context menu to create a publishing task;
- asynchronous publishing queue execution with failed-task retry and duplicate-task protection;
- platform adapters for Douyin, Kuaishou, Bilibili, WeChat Channels,
  Xiaohongshu, Baijiahao, Alipay, Weibo, Hupu, TikTok and YouTube (via `sau`),
  plus a separate Toutiao adapter stub;
- encrypted/OS-backed secret storage through `keyring` when available.

Only download and publish content you are authorized to use. Platform automation can require manual CAPTCHA/2FA intervention.

发布能力说明：下载完成的视频可以创建发布任务并进入发布队列。除今日头条外，
各平台发布由软件本地、可独立更新的 `social-auto-upload` CLI 核心执行；核心未安装时，
“检查运行组件”可自动准备 Python 3.12、SAU 和 Chromium。每个平台和账号使用独立 Cookie，登录后
立即校验，正式发布前再次校验；Cookie 缺失或过期时会阻止上传并提示重新登录。
Cookie 内容不会写入数据库、设置或日志。今日头条目前提供浏览器手动发布流程。

账号中心中的“默认发布目标”决定完成列表的覆盖率分母。完成卡片会分别显示已发布、队列中、
待重试和未创建的平台数：`继续分发`只会预选从未创建任务的平台；已失败的平台保留原任务，
可直接进入仅筛选当前视频的发布队列查看原因并重试，避免误建重复发布任务。全部目标平台
成功后，主操作会显示“目标平台已完成”；如需重新发布，仍可通过右键菜单手动创建新任务。

# 汇发

Windows 10/11 desktop application for downloading YouTube media with `yt-dlp` and preparing multi-platform publishing tasks.

## Quick start

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m app.main
```

The repository includes bundled FFmpeg 9.0.1 runtimes under `tools/ffmpeg/x64/` and `tools/ffmpeg/x86/`. The downloader automatically selects the build matching the Python process bitness; you can override the path in Settings.

## Portable data directory

Runtime data is stored beside the application under `data/`:

- `data/app.db` — download, media and publishing task database;
- `data/settings.ini` — download directory, quality, proxy and tool settings;
- `data/browser/` — embedded browser cache and login profile;
- `data/downloads/` — default download directory.
- `data/logs/downloads/` — per-task diagnostic logs in JSON Lines format;

The Settings page is the single place for selecting the download directory.
The download page only displays the current directory and provides shortcuts
to open it or jump to Settings, so the two pages cannot drift out of sync.
Settings are grouped into download, network and tool sections, and invalid or
unwritable download paths are rejected with an explicit message.

The download task list supports title/URL/ID search, sorting by time/title/status,
global pause/resume, completed-record cleanup, and a `查看下载日志` action. Each task log records
解析、格式选择、进度、网络/代理错误、风控/登录错误和完成状态 without
storing cookies or tokens. The log dialog also provides an initial diagnosis
category so network failures can be distinguished from rate limits, login,
CAPTCHA, format, and tool errors.

The application no longer uses the user's Documents directory for its own
database or settings. Existing data from the legacy `.youtube-release-studio`
location is migrated to `data/app.db` on first launch.

The first version provides:

- queued downloads with progress, speed and ETA;
- a task dashboard with grouped download controls, per-task title, URL, thumbnail placeholder, status, progress, speed, ETA, cancel/retry and folder actions;
- persistent download directory, proxy, filename template and quality preferences;
- quality presets for best available, 1080p maximum and 720p maximum;
- custom quality mode that parses available formats before download;
- metadata/thumbnail/info.json persistence in SQLite + files;
- embedded browser profiles for YouTube and publishing sites;
- embedded browser navigation controls (back, forward, refresh, stop and address bar);
- completed media list with a context menu to create a publishing task;
- asynchronous publishing queue execution with failed-task retry and duplicate-task protection;
- platform adapters for Douyin, Kuaishou, Bilibili, WeChat Channels (via `sau`) and a separate Toutiao adapter stub;
- encrypted/OS-backed secret storage through `keyring` when available.

Only download and publish content you are authorized to use. Platform automation can require manual CAPTCHA/2FA intervention.

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

The download page is the single place for selecting the download directory;
the Settings page contains shared network and tool settings only.

The application no longer uses the user's Documents directory for its own
database or settings. Existing data from the legacy `.youtube-release-studio`
location is migrated to `data/app.db` on first launch.

The first version provides:

- queued downloads with progress, speed and ETA;
- a task dashboard with per-task title, URL, thumbnail placeholder, status, progress, speed, ETA, cancel/retry and folder actions;
- persistent download directory, proxy, filename template and quality preferences;
- quality presets for best available, 1080p maximum and 720p maximum;
- custom quality mode that parses available formats before download;
- metadata/thumbnail/info.json persistence in SQLite + files;
- embedded browser profiles for YouTube and publishing sites;
- completed media list with a context menu to create a publishing task;
- platform adapters for Douyin, Kuaishou, Bilibili, WeChat Channels (via `sau`) and a separate Toutiao adapter stub;
- encrypted/OS-backed secret storage through `keyring` when available.

Only download and publish content you are authorized to use. Platform automation can require manual CAPTCHA/2FA intervention.

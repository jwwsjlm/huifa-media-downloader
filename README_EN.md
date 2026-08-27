# Huifa Media Downloader

<p align="center">
  <strong>A Windows downloader, media processor, and multi-platform publishing tool powered by yt-dlp</strong>
</p>

<p align="center">
  <a href="README.md">简体中文</a> |
  <a href="README_EN.md">English</a>
</p>

<p align="center">
  <a href="https://github.com/jwwsjlm/huifa-media-downloader/releases/latest">Download the latest release</a> ·
  <a href="https://github.com/jwwsjlm/huifa-media-downloader/issues">Report an issue</a>
</p>

![Huifa Media Downloader task view](docs/images/download-tasks.png)

## Overview

Huifa Media Downloader is a desktop application for Windows 10/11 x64. It uses `yt-dlp` to resolve videos, playlists, channels, and other collection URLs, uses FFmpeg for merging and transcoding, and provides cookie-based sign-in, thumbnail processing, subtitles, task management, and multi-platform publishing workflows.

Official releases provide both a portable ZIP and an installer ZIP. Both editions contain the Python runtime and required application components, so end users do not need to install Python, Chrome, FFmpeg, or yt-dlp.

## Features

- Download individual videos, audio, playlists, channels, and collections from sites supported by yt-dlp.
- Stream collection parsing results into a selectable list and manage selected entries as parent/child tasks.
- Choose the best quality, a common resolution, or an exact format, with subtitle, audio-track, and audio-only options.
- Track parsing, downloading, merging, transcoding, thumbnail, and validation stages with progress, speed, and ETA.
- Pause, resume, retry, search, batch-manage, and organize completed media.
- Merge and convert media with FFmpeg/FFprobe while exposing the CPU/GPU encoders and hardware decoding capabilities available on the current computer.
- Convert thumbnails to JPG, adjust crop focus and aspect ratio, organize one task per folder, and use a custom processing directory.
- Sign in through the app-managed Playwright Chromium and preserve cookies in the portable application data directory.
- Independently install or update yt-dlp, FFmpeg/FFprobe, Deno, yt-dlp-ejs, and other runtime components.
- Check for application updates through the GitHub Releases API and verify downloads with GitHub-provided SHA-256 digests.
- Create and manage multi-platform publishing jobs from completed downloads.

## Download and use

1. Open the [latest release](https://github.com/jwwsjlm/huifa-media-downloader/releases/latest).
2. For portable use, download `HuifaMediaDownloader-<version>-portable-win-x64.zip`, extract the complete archive, and run the root `HuifaVideoDownloader.exe`. Do not copy only the EXE.
3. For an installed edition, download `HuifaMediaDownloader-<version>-installer-win-x64.zip`, extract it, and run `HuifaMediaDownloader-Setup.exe`.
4. Portable-edition state stays in `data/`, while downloads default to the root `downloads/` folder. Move the complete folder when relocating it. The installed edition downloads to `Huifa Video Downloader/` under the system Downloads folder by default, keeping media outside the application's uninstall scope.

The portable edition uses a self-updating directory layout. FFmpeg, FFprobe, yt-dlp, Deno, yt-dlp-ejs, and Chromium are shipped in the application's own `current/tools/` directory. If Windows SmartScreen reports an unknown publisher, verify that the file came from this repository's Release page before deciding whether to run it.

## Portable data

The application keeps its main runtime data beside the executable instead of requiring the Documents folder:

- `data/app.db`: download, media, and publishing task database.
- `data/settings.ini`: application settings.
- `data/browser/`: app-managed browser profiles and encrypted cookie copies.
- `data/logs/`: task diagnostics.
- `data/tools/`: independently updatable local runtime components.

The portable edition's default media folder is the root `downloads/` directory, separate from private runtime state such as the database and cookies. yt-dlp-ejs updates follow the exact companion version declared by the bundled yt-dlp; the official external `yt-dlp.exe` already includes EJS.

Do not publicly upload the complete `data/` directory. It may contain signed-in account state and private task information.

## Run from source

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
python -m app.main
```

See the [development and release notes](docs/DEVELOPMENT.md) for build details, runtime layout, automatic updates, and the GitHub Release workflow.

## Automated releases

Pushing a `v<version>` tag matching `APP_VERSION` makes GitHub Actions run the test suite, prepare official runtimes, build the directory-updatable portable and installed editions, create a SHA-256 manifest, and publish a GitHub Release on a Windows runner.

## Responsible use

Only download, process, and publish content you are authorized to use. Some platforms may require sign-in, CAPTCHA, two-factor authentication, or manual confirmation, and their rules or available formats may change over time.

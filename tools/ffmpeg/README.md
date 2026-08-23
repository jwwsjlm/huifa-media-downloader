# Bundled FFmpeg builds

This directory contains the Windows shared FFmpeg 9.0.1 builds used by the
application:

- `x64/` — 64-bit Windows build from [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds/releases/tag/latest)
- `x86/` — 32-bit Windows build from [defisym/FFmpeg-Builds-Win32](https://github.com/defisym/FFmpeg-Builds-Win32/releases/tag/latest)

Both builds are FFmpeg `n9.0.1-6-g9d4ca21220-20260822`, GPL shared builds. The
application selects `x64` or `x86` based on the Python process bitness and
passes the matching `ffmpeg.exe` to `yt-dlp`.

Please keep the upstream FFmpeg license/notices when redistributing the
application.

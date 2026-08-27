# Bundled FFmpeg builds

This directory contains the Windows shared FFmpeg 9.0.1 builds used by the
application. Runtime update detection now uses the yt-dlp maintained
[yt-dlp/FFmpeg-Builds](https://github.com/yt-dlp/FFmpeg-Builds) release source
recommended by yt-dlp. The checked-in binaries predate that source change and
will be replaced on the next explicit release build.

- `x64/` — 64-bit Windows build currently bundled with the application
- `x86/` — legacy 32-bit compatibility runtime; the primary single-EXE release bundles x64 only

Both builds are FFmpeg `n9.0.1-6-g9d4ca21220-20260822`, GPL shared builds. The
application selects `x64` or `x86` based on the Python process bitness and
passes the matching `ffmpeg.exe` to `yt-dlp`.

Please keep the upstream FFmpeg license/notices when redistributing the
application.

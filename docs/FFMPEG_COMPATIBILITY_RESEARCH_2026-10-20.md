# FFmpeg compatibility builds

Checked on 2026-10-20 against the providers' GitHub release APIs and versioned build sources. No complete binary assets were downloaded for this research. macOS deployment targets were read from 16 KiB HTTP Range responses containing the Mach-O load commands.

The compatibility profile uses the curated FFmpeg and FFprobe entries in `runtime-tool-lock.json`. New installations should use this profile by default. The efficiency profile can use the newer existing providers, while retaining an explicit capability probe for the selected GPU and source format. A newer FFmpeg version alone does not establish GPU compatibility or a performance improvement.

## NVIDIA candidates

The fixed [BtbN release `autobuild-2026-02-28-12-59`](https://github.com/BtbN/FFmpeg-Builds/releases/tag/autobuild-2026-02-28-12-59) contains both release branches below. Its [versioned build script](https://github.com/BtbN/FFmpeg-Builds/blob/c482a075076c29edf6dd9c5a7c9d3113d0babed6/scripts.d/50-ffnvcodec.sh) selects SDK 12.2 headers for FFmpeg 7.1 and SDK 13.0 headers for FFmpeg 8.0. It enables `ffnvcodec` and CUDA LLVM on supported targets and excludes Windows ARM64.

| Candidate | Fixed Windows x64 shared asset | Header driver minimum | Decision |
| --- | --- | --- | --- |
| FFmpeg 7.1.3 | `ffmpeg-n7.1.3-42-g39ee683e8f-win64-gpl-shared-7.1.zip` | Windows 551.76; Linux 550.54.14 | Default compatibility build |
| FFmpeg 8.0.1 | `ffmpeg-n8.0.1-66-g27b8d1a017-win64-gpl-shared-8.0.zip` | Windows/Linux 570.0 | Feasible alternative for driver 581.57; not the default pin |

Driver requirements come from the exact pinned [SDK 12.2 header commit](https://github.com/FFmpeg/nv-codec-headers/blob/9934f17316b66ce6de12f3b82203a298bc9351d8/README) and [SDK 13.0 header commit](https://github.com/FFmpeg/nv-codec-headers/blob/876af32a202d0de83bd1d36fe74ee0f7fcf86b0d/README). Both requirements are below the local NVIDIA driver 581.57. By comparison, the official [SDK 13.1 headers](https://github.com/FFmpeg/nv-codec-headers/blob/n13.1.15.0/README) require driver 610.0. The application must still run an actual HEVC NVENC probe on the GTX 1080 Ti before reporting that encoding is available.

## Compatibility pins

All eight pinned assets below were checked against `name`, `browser_download_url` and `digest` from the official GitHub release API. The download code can preserve its current SHA-256 verification; no checksum fallback is necessary.

| Platform / tool | Repository and release | Asset | SHA-256 |
| --- | --- | --- | --- |
| Windows x64, FFmpeg + FFprobe | BtbN/FFmpeg-Builds, `autobuild-2026-02-28-12-59` | `ffmpeg-n7.1.3-42-g39ee683e8f-win64-gpl-shared-7.1.zip` | `091dd8f5f5b0909fbd0e915b48162cf1f6b1c8616dfe225218047a8b91236b7d` |
| Linux x64, FFmpeg + FFprobe | Same | `ffmpeg-n7.1.3-42-g39ee683e8f-linux64-gpl-7.1.tar.xz` | `72f2b6d13e87c9bda02b07c6079b6c97457295b0ac940279d9d83a70c1ac163d` |
| Linux ARM64, FFmpeg + FFprobe | Same | `ffmpeg-n7.1.3-42-g39ee683e8f-linuxarm64-gpl-7.1.tar.xz` | `269f7207e5f4fa1ab41e6de273a0cd6c74b9bf35cf685f70adae0d51750c2d7f` |
| Windows ARM64, FFmpeg + FFprobe | [tordona/ffmpeg-win-arm64, `7.1.5`](https://github.com/tordona/ffmpeg-win-arm64/releases/tag/7.1.5) | `ffmpeg-7.1.5-essentials-static-win-arm64.7z` | `4c4fcdf80824ca12368c998e5fbf7c368a72bdb2376e9517dbf335364d5de2d8` |
| macOS x64, FFmpeg | [shaka-project/static-ffmpeg-binaries, `n8.0.3-1`](https://github.com/shaka-project/static-ffmpeg-binaries/releases/tag/n8.0.3-1) | `ffmpeg-osx-x64` | `b2ea2ac9b4cb060acb70874648f4c48863ab5c61d5c71dbfe3d65fd5292e40ef` |
| macOS ARM64, FFmpeg | Same | `ffmpeg-osx-arm64` | `3ef8a1b1ff20b28f7b17ec0bf42105434b7997f4a02525f818e472e7baa86749` |
| macOS x64, FFprobe | Same | `ffprobe-osx-x64` | `72bb3454b9d63d1c1c597e459114f3f82251a859a27e6dc48f8179fa73c2170f` |
| macOS ARM64, FFprobe | Same | `ffprobe-osx-arm64` | `611aebc88eda026ef21418aeca45533959b8527dfcce3e28baa3e78daeaf4b82` |

The [Windows ARM64 provider's versioned documentation](https://github.com/tordona/ffmpeg-win-arm64/blob/7.1.5/README.md) lists libx264 and libx265 in its essentials build. It lists Windows hardware support including Media Foundation and AMF; it does not advertise NVENC. Native ARM64 execution still requires testing on that platform.

The [macOS provider's versioned configuration](https://github.com/shaka-project/static-ffmpeg-binaries/blob/n8.0.3-1/build-scripts/90-ffmpeg.sh) explicitly enables VideoToolbox, libx264 and libx265. Both `n8.0.3-1` FFmpeg binaries declare macOS 15.0 in `LC_BUILD_VERSION`, matching the application's existing minimum. The previously bundled `n8.1.2-1` binaries declare the same minimum. Historical `n7.1-2` declares macOS 13.0 on x64 and 14.0 on ARM64, but its assets lack GitHub SHA-256 digests; it is therefore not used by this profile.

The macOS CPU encoder has a separate bit-depth limitation. Both the compatibility `n8.0.3-1` and efficiency `n8.1.2-1` provider tags select x265 4.2. Their [x265 build script](https://github.com/shaka-project/static-ffmpeg-binaries/blob/n8.0.3-1/build-scripts/04-x265.sh) performs one default CMake build, with no high-bit-depth or multilib build later in either workflow. The [upstream x265 4.2 CMake defaults](https://bitbucket.org/multicoreware/x265_git/src/4.2/source/CMakeLists.txt) set `HIGH_BIT_DEPTH`, `LINKED_10BIT` and `LINKED_12BIT` to `OFF`. The provider also unlinks preinstalled Homebrew x265 before compiling its static library. These builds therefore supply an 8-bit CPU x265 encoder. Compression must reject a higher-bit-depth source on that CPU path, while separately probing VideoToolbox capability. Native tests on these builds must assert refusal with a genuinely 10-bit fixture instead of silently producing an 8-bit HDR test input. The ordinary 8-bit CPU compression test still runs on all desktop release platforms.

## Existing packaging implications

`scripts/acquire-rust-tools.mjs` already verifies pinned SHA-256 values, extracts the FFmpeg/FFprobe pair from the BtbN or Windows ARM64 archive, includes the shared Windows DLLs, and verifies native executable architecture. Its lock refresh previously chose a dated current master build; compatibility pins must survive refreshes of unrelated runtime components.

The Docker Rust builder also needs this lock because the runtime policy embeds it at compile time. The container installer now downloads the Linux compatibility archive from the same lock, verifies SHA-256, stages both programs, checks matching versions and the required HEVC encoder, and installs the pair after validation. It no longer relies on the distribution's FFmpeg package. The shell integration tests cover both Linux architecture selections and preserve an existing pair when validation fails; they do not replace a native image build.

`crates/core/src/tools.rs` previously supported explicit channel/version choice only for yt-dlp and resolved FFmpeg to the current provider release. Profile selection should resolve the curated compatibility pins separately from the efficiency update path, install FFmpeg and FFprobe together, and keep existing immutable task snapshots intact.

A new local compression task must snapshot the currently selected runtime pair, even when its input belongs to an older completed task. Its preview must probe the same selected runtime. Inheriting the completed task's old FFmpeg snapshot would leave new compression attempts on the incompatible build after a profile switch. Existing queued or running tasks retain their snapshots.

The [FFmpeg 7.1.3 libx265 wrapper](https://github.com/FFmpeg/FFmpeg/blob/n7.1.3/libavcodec/libx265.c) does not expose the newer `-x265-stats` option. Two-pass encoding must use an older supported mechanism, such as the x265 `stats` parameter with a task-local working directory and a relative filename, and be tested with the pinned executable.

BtbN's [documented retention policy](https://github.com/BtbN/FFmpeg-Builds/blob/c482a075076c29edf6dd9c5a7c9d3113d0babed6/README.md) keeps monthly builds for two years. A future maintenance update must refresh or preserve these fixed compatibility assets before that retention window expires. The historical Linux builds target glibc 2.28 and Linux 4.18 or newer.

## Implementation validation

- Windows x64, GTX 1080 Ti with driver 581.57: the pinned 7.1.3 build passed the application's complete CPU and NVENC compression flows. NVENC produced 2,532,040 bytes against a 2,570,495-byte target. The fixtures also verified downscaling, fractional frame rates, HDR10/HLG, two audio tracks, subtitles, attachments, chapters and unchanged source files.
- FFmpeg 7.1 shifts negative input timestamps and chapter boundaries unless the compression command uses `-copyts`. The regression fixture catches the original 21 ms chapter shift without relaxing the preservation tolerance.
- The real managed-tool installation test downloaded and verified the compatibility archive, installed FFmpeg/FFprobe together, preserved previous task snapshots, and queried both an explicit official release tag and the efficiency profile.
- Linux x64, WSL Ubuntu 24.04: the pinned archive's SHA-256 matched, and both binaries ran natively. A 3-second CPU HEVC two-pass encode produced 487,330 bytes against a 600,000-byte target, preserving 640×360, 30000/1001 fps and the copied AAC track's hash. This was a CLI check, not the full Rust application suite on Linux.
- macOS and ARM binaries were not executed on native hardware. Docker's installer passed isolated script tests; a complete image build was unavailable because the local Docker engine was unavailable.

## Local Linux execution

The locked Linux x64 archive was downloaded and its SHA-256 matched `72f2b6d13e87c9bda02b07c6079b6c97457295b0ac940279d9d83a70c1ac163d`. Both extracted programs ran as `n7.1.3-42-g39ee683e8f-20260228` on WSL Ubuntu 24.04, using a separate temporary directory without replacing installed system tools.

A three-second 640 × 360 test pattern at `30000/1001` fps with one English AAC sine-wave track was compressed with libx265 in two passes. The encoder used `stats=compression-stats` relative to its working directory and copied the audio stream.

| Check | Result |
| --- | --- |
| Source size | 1,302,120 bytes |
| Target maximum | 600,000 bytes |
| Compressed size | 487,330 bytes |
| Source / output duration | 3.021 / 3.050 seconds |
| Output video | HEVC, 640 × 360, `30000/1001` fps |
| Output audio | One AAC track, language `eng` |
| Copied audio SHA-256, both files | `68ecb72d2e7885bdaf95bce82d96989becec067af8a00cb75d4600798a7b20e7` |
| Source file SHA-256, before and after | `5a8ed0bd395df094641e602f03fce15af5778db8461af85668baa5070a0b7f63` |

This confirms the Linux x64 compatibility executable and the older two-pass option mechanism. ARM64 and macOS executable behavior still depends on native testing on those platforms.

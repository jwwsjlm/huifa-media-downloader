# Docker web application

The Rust web host serves the same React build as the desktop app on port 19491.
Both `linux/amd64` and `linux/arm64` use their corresponding official standalone
yt-dlp and Deno binaries, verified against release checksums; FFmpeg and Chromium
come from Debian packages. Python application code is not used at runtime.

Set `HUIFA_ACCOUNT` and `HUIFA_PASSWORD`, then run `docker compose up --build -d`.
The application refuses startup when either credential is empty. Set
`HUIFA_PUBLIC_ORIGIN` to the exact URL you open, such as `http://server:19491`.
The default is `http://localhost:19491`. For an HTTPS reverse proxy, use its public
HTTPS origin; the session cookie then gains the Secure flag. A direct HTTP URL is
supported as requested, and its login traffic is plaintext on the network.

The new named volumes `rust-data` and `rust-downloads` are separate from legacy
application data. Back up `/data` together with its `.credentials.key`: encrypted
Cookies and AI keys cannot be restored without that key. Mounted host directories
must be owned by UID/GID 10001. `/app` runs read-only, with a non-root UID, dropped
capabilities, and a writable temporary filesystem. No Docker socket is mounted.
The `/tmp` tmpfs uses `exec,nosuid,nodev`: the official standalone yt-dlp binary
extracts bundled native libraries there and needs executable memory mappings.

Remote login starts one disposable headless Chromium profile on demand. Its
screencast and bounded mouse/keyboard input use authenticated endpoints on the
same port. The CDP port binds to 127.0.0.1 and is never published. Save imports
Cookies into the encrypted Rust credential store, stops Chromium and deletes the
profile. Closing the session, 15 minutes of idle time, or SIGTERM also cleans it
up. Container Chromium uses `--no-sandbox` because user namespaces are commonly
disabled; container isolation and the non-root user are its containment boundary.
Some websites reject headless login or require device verification; a Netscape
Cookie file remains available as an import option.

`runtime-tool-lock.json` pins the official yt-dlp and Deno assets and SHA-256
checksums for both architectures. Build both architectures with
`docker buildx build --platform linux/amd64,linux/arm64`.

Release CI supplies `--build-arg USE_PREBUILT_FRONTEND=1` after placing the shared
React build in `frontend/dist`. A normal local build runs `npm ci` and builds it.
`scripts/check-rust-container.mjs` checks an actual download/transcode and volume
restart; `docker/check-browser.mjs` checks the actual remote login flow. Both take
`HUIFA_IMAGE` and `HUIFA_PLATFORM` environment variables and clean up their own
containers and test volumes.

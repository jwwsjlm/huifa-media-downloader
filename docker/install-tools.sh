#!/bin/sh
set -eu

architecture=${1:?target architecture required}
lock=${2:?runtime tool lock required}
case "$architecture" in
  amd64) target=linux-x86_64 ;;
  arm64) target=linux-aarch64 ;;
  *) echo "Unsupported target architecture: $architecture" >&2; exit 1 ;;
esac

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT HUP INT TERM
cd "$work"
test "$(jq -r .schemaVersion "$lock")" = 1
for tool in yt-dlp qjs; do
  case "$tool" in
    yt-dlp) repository=yt-dlp/yt-dlp ;;
    qjs) repository=quickjs-ng/quickjs ;;
  esac
  test "$(jq -r --arg tool "$tool" '.tools[$tool].repository' "$lock")" = "$repository"
  url=$(jq -r --arg tool "$tool" --arg target "$target" '.tools[$tool].assets[$target].url' "$lock")
  checksum=$(jq -r --arg tool "$tool" --arg target "$target" '.tools[$tool].assets[$target].sha256' "$lock")
  case "$url" in "https://github.com/$repository/releases/download/"*) ;; *) echo "Invalid tool origin" >&2; exit 1 ;; esac
  test "${#checksum}" = 64
  case "$checksum" in *[!0-9a-f]*) echo "Invalid tool checksum" >&2; exit 1 ;; esac
  curl --fail --location --retry 3 --proto '=https' "$url" -o "$tool.archive"
  printf '%s  %s\n' "$checksum" "$tool.archive" | sha256sum --check -
  mv "$tool.archive" "$tool"
  install -m 0755 "$tool" "/usr/local/bin/$tool"
done

/usr/local/bin/yt-dlp --version
/usr/local/bin/qjs --version
ffmpeg -version | head -n 1
chromium --version

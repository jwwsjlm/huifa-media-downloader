#!/bin/sh
set -eu

architecture=${1:?target architecture required}
lock=${2:?runtime tool lock required}
destination=${3:-/usr/local/bin}
case "$architecture" in
  amd64) target=linux-x86_64 ;;
  arm64) target=linux-aarch64 ;;
  *) echo "Unsupported target architecture: $architecture" >&2; exit 1 ;;
esac

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT HUP INT TERM
cd "$work"
test "$(jq -r .schemaVersion "$lock")" = 1
mkdir -p "$destination"
for tool in yt-dlp qjs ffmpeg; do
  case "$tool" in
    yt-dlp) repository=yt-dlp/yt-dlp ;;
    qjs) repository=quickjs-ng/quickjs ;;
    ffmpeg) repository=BtbN/FFmpeg-Builds ;;
  esac
  test "$(jq -r --arg tool "$tool" '.tools[$tool].repository' "$lock")" = "$repository"
  url=$(jq -r --arg tool "$tool" --arg target "$target" '.tools[$tool].assets[$target].url' "$lock")
  checksum=$(jq -r --arg tool "$tool" --arg target "$target" '.tools[$tool].assets[$target].sha256' "$lock")
  filename=$(jq -r --arg tool "$tool" --arg target "$target" '.tools[$tool].assets[$target].name' "$lock")
  version=$(jq -r --arg tool "$tool" --arg target "$target" '.tools[$tool].assets[$target].version // .tools[$tool].version' "$lock")
  case "$url" in "https://github.com/$repository/releases/download/"*) ;; *) echo "Invalid tool origin" >&2; exit 1 ;; esac
  test "$url" = "https://github.com/$repository/releases/download/$version/$filename"
  test "${#checksum}" = 64
  case "$checksum" in *[!0-9a-f]*) echo "Invalid tool checksum" >&2; exit 1 ;; esac
  curl --fail --location --retry 3 --proto '=https' --proto-redir '=https' "$url" -o "$tool.archive"
  printf '%s  %s\n' "$checksum" "$tool.archive" | sha256sum --check -
  if [ "$tool" = ffmpeg ]; then
    members=$(tar -tf ffmpeg.archive)
    for executable in ffmpeg ffprobe; do
      member=$(printf '%s\n' "$members" | awk -v executable="$executable" '
        /^\// || /\\/ || /(^|\/)\.\.(\/|$)/ { invalid = 1 }
        $0 ~ "(^|/)bin/" executable "$" { if (member != "") duplicate = 1; member = $0 }
        END { if (invalid || duplicate || member == "") exit 1; print member }
      ') || { echo "FFmpeg archive must contain one safe bin/$executable" >&2; exit 1; }
      tar -xOf ffmpeg.archive -- "$member" > "$executable"
      chmod 0755 "$executable"
    done
    ffmpeg_version_output=$(./ffmpeg -version)
    ffprobe_version_output=$(./ffprobe -version)
    ffmpeg_version=$(printf '%s\n' "$ffmpeg_version_output" | awk 'NR == 1 { print $3 }')
    ffprobe_version=$(printf '%s\n' "$ffprobe_version_output" | awk 'NR == 1 { print $3 }')
    if [ -z "$ffmpeg_version" ] || [ "$ffmpeg_version" != "$ffprobe_version" ]; then
      echo "FFmpeg and FFprobe versions differ; installation refused" >&2
      exit 1
    fi
    encoders=$(./ffmpeg -hide_banner -encoders)
    for encoder in libx264 libx265 libvpx-vp9 libopus; do
      printf '%s\n' "$encoders" | awk -v encoder="$encoder" '
        $2 == encoder { found = 1 }
        END { exit !found }
      ' || { echo "FFmpeg is missing required encoder: $encoder" >&2; exit 1; }
    done
    install -m 0755 ffmpeg "$destination/ffmpeg"
    install -m 0755 ffprobe "$destination/ffprobe"
  else
    mv "$tool.archive" "$tool"
    install -m 0755 "$tool" "$destination/$tool"
  fi
done

"$destination/yt-dlp" --version
"$destination/qjs" --version
"$destination/ffmpeg" -version | head -n 1
"$destination/ffprobe" -version | head -n 1
chromium --version

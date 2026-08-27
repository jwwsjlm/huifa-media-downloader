from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping


PROGRESS_PREFIX = "__HUIFA_PROGRESS__"
POSTPROCESS_PREFIX = "__HUIFA_POSTPROCESS__"
RESULT_PREFIX = "__HUIFA_RESULT__"
_VERSION_CACHE_LOCK = threading.RLock()
_VERSION_PATH_CACHE: dict[str, str] = {}
_OUTPUT_POLL_SECONDS = 0.1
_OUTPUT_DRAIN_GRACE_SECONDS = 1.0
_PROCESS_WAIT_TIMEOUT_SECONDS = 5.0
_READER_JOIN_TIMEOUT_SECONDS = 1.0


class ExternalYtdlpError(RuntimeError):
    pass


def _version_cache_path(executable: str | Path) -> str:
    try:
        return str(Path(executable).resolve())
    except OSError:
        return str(Path(executable).absolute())


def cached_external_ytdlp_version(executable: str | Path) -> str | None:
    """Return the startup probe result without launching or statting yt-dlp.

    ``None`` means this process has not checked the executable yet. Download
    and collection workers may optimistically launch it in that case; the
    background startup check will populate the cache for subsequent tasks.
    """
    key = _version_cache_path(executable)
    with _VERSION_CACHE_LOCK:
        return _VERSION_PATH_CACHE.get(key)


def remember_external_ytdlp_version(executable: str | Path, version: str) -> None:
    """Publish a version already obtained by the startup diagnostics worker."""
    key = _version_cache_path(executable)
    with _VERSION_CACHE_LOCK:
        _VERSION_PATH_CACHE[key] = str(version or "")


def clear_external_ytdlp_version_cache(executable: str | Path | None = None) -> None:
    """Invalidate cached startup checks after replacing the standalone core."""
    with _VERSION_CACHE_LOCK:
        if executable is None:
            _VERSION_PATH_CACHE.clear()
            return
        path_key = _version_cache_path(executable)
        _VERSION_PATH_CACHE.pop(path_key, None)


def _cookies_from_browser_argument(value: Any) -> str:
    if not isinstance(value, (tuple, list)) or not value:
        return ""
    browser = str(value[0] or "").strip()
    if not browser:
        return ""
    profile = str(value[1] or "").strip() if len(value) > 1 else ""
    keyring = str(value[2] or "").strip() if len(value) > 2 else ""
    container = str(value[3] or "").strip() if len(value) > 3 else ""
    argument = browser + (f"+{keyring}" if keyring else "")
    if profile or container:
        argument += f":{profile}"
    if container:
        argument += f"::{container}"
    return argument


def _option_values(value: Any) -> tuple[Any, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, (str, bytes)):
        return (value,)
    if isinstance(value, Mapping):
        return ()
    try:
        return tuple(value)
    except TypeError:
        return (value,)


def _append_output_and_selection_options(
    command: list[str],
    options: Mapping[str, Any],
) -> None:
    paths = options.get("paths")
    if isinstance(paths, Mapping):
        for path_type in ("home", "temp"):
            path = str(paths.get(path_type) or "").strip()
            if path:
                command.extend(("--paths", f"{path_type}:{path}"))
    scalar_options = (
        ("outtmpl", "--output"),
        ("trim_file_name", "--trim-filenames"),
        ("format", "--format"),
        ("merge_output_format", "--merge-output-format"),
        ("playlistend", "--playlist-end"),
        ("playliststart", "--playlist-start"),
        ("playlist_items", "--playlist-items"),
        ("dateafter", "--dateafter"),
        ("datebefore", "--datebefore"),
    )
    for key, flag in scalar_options:
        if options.get(key):
            command.extend((flag, str(options[key])))
    if options.get("windowsfilenames"):
        command.append("--windows-filenames")
    if options.get("allow_multiple_audio_streams"):
        command.append("--audio-multistreams")
    format_sort = options.get("format_sort") or ()
    if isinstance(format_sort, str):
        format_sort = (format_sort,)
    if format_sort:
        command.extend(("--format-sort", ",".join(str(item) for item in format_sort)))
    for key, flag in (
        ("noplaylist", "--no-playlist"),
        ("ignoreerrors", "--ignore-errors"),
        ("extract_flat", "--flat-playlist"),
        ("lazy_playlist", "--lazy-playlist"),
        ("playlistreverse", "--playlist-reverse"),
        ("playlistrandom", "--playlist-random"),
        ("live_from_start", "--live-from-start"),
    ):
        if options.get(key):
            command.append(flag)
    wait_for_video = options.get("wait_for_video")
    if isinstance(wait_for_video, (tuple, list)) and wait_for_video:
        interval = str(wait_for_video[0])
        if len(wait_for_video) > 1:
            interval += f"-{wait_for_video[1]}"
        command.extend(("--wait-for-video", interval))
    for section in _option_values(options.get("download_sections")):
        command.extend(("--download-sections", str(section)))


def _append_network_and_runtime_options(
    command: list[str],
    options: Mapping[str, Any],
) -> None:
    if options.get("ratelimit_text") or options.get("ratelimit"):
        command.extend((
            "--limit-rate",
            str(options.get("ratelimit_text") or options.get("ratelimit")),
        ))
    if options.get("concurrent_fragment_downloads"):
        command.extend((
            "--concurrent-fragments",
            str(options["concurrent_fragment_downloads"]),
        ))
    for key, flag in (
        ("retries", "--retries"),
        ("fragment_retries", "--fragment-retries"),
        ("extractor_retries", "--extractor-retries"),
        ("file_access_retries", "--file-access-retries"),
        ("socket_timeout", "--socket-timeout"),
        ("sleep_interval_requests", "--sleep-requests"),
    ):
        if options.get(key) is not None:
            command.extend((flag, str(options[key])))
    if options.get("continuedl"):
        command.append("--continue")
    for key, flag in (
        ("ffmpeg_location", "--ffmpeg-location"),
        ("proxy", "--proxy"),
        ("cookiefile", "--cookies"),
    ):
        if options.get(key):
            command.extend((flag, str(options[key])))
    browser_argument = _cookies_from_browser_argument(options.get("cookiesfrombrowser"))
    if browser_argument:
        command.extend(("--cookies-from-browser", browser_argument))
    runtimes = options.get("js_runtimes") or {}
    if isinstance(runtimes, Mapping):
        for runtime, config in runtimes.items():
            path = (
                str((config or {}).get("path") or "").strip()
                if isinstance(config, Mapping) else ""
            )
            command.extend(("--js-runtimes", f"{runtime}:{path}" if path else str(runtime)))
    for component in sorted(
        str(item) for item in _option_values(options.get("remote_components"))
    ):
        command.extend(("--remote-components", component))


def _append_download_file_options(
    command: list[str],
    options: Mapping[str, Any],
) -> None:
    for key, flag in (
        ("writethumbnail", "--write-thumbnail"),
        ("writeinfojson", "--write-info-json"),
        ("writesubtitles", "--write-subs"),
        ("writeautomaticsub", "--write-auto-subs"),
        ("writedescription", "--write-description"),
        ("getcomments", "--write-comments"),
    ):
        if options.get(key):
            command.append(flag)
    subtitle_languages = _option_values(options.get("subtitleslangs"))
    if subtitle_languages:
        command.extend(("--sub-langs", ",".join(str(item) for item in subtitle_languages)))
    if options.get("subtitlesformat"):
        command.extend(("--sub-format", str(options["subtitlesformat"])))


def _append_postprocessor_options(
    command: list[str],
    options: Mapping[str, Any],
) -> None:
    processors = [
        processor
        for processor in (options.get("postprocessors") or ())
        if isinstance(processor, Mapping)
    ]
    removed_categories = {
        str(item)
        for processor in processors
        if str(processor.get("key") or "") == "ModifyChapters"
        for item in _option_values(processor.get("remove_sponsor_segments"))
    }
    for processor in processors:
        key = str(processor.get("key") or "")
        if key == "FFmpegExtractAudio":
            command.append("--extract-audio")
            codec = str(processor.get("preferredcodec") or "").strip()
            if codec:
                command.extend(("--audio-format", codec))
        elif key == "FFmpegVideoRemuxer":
            container = str(processor.get("preferedformat") or "").strip()
            if container:
                command.extend(("--remux-video", container))
        elif key == "FFmpegSplitChapters":
            command.append("--split-chapters")
        elif key == "FFmpegMetadata":
            if processor.get("add_metadata"):
                command.append("--embed-metadata")
            if processor.get("add_chapters"):
                command.append("--embed-chapters")
        elif key == "FFmpegEmbedSubtitle":
            command.append("--embed-subs")
        elif key == "EmbedThumbnail":
            command.append("--embed-thumbnail")
        elif key == "SponsorBlock":
            categories = sorted(
                str(item)
                for item in _option_values(processor.get("categories"))
                if str(item) not in removed_categories
            )
            if categories:
                command.extend(("--sponsorblock-mark", ",".join(categories)))
        elif key == "ModifyChapters" and processor.get("remove_sponsor_segments"):
            categories = sorted(str(item) for item in _option_values(
                processor["remove_sponsor_segments"]
            ))
            command.extend(("--sponsorblock-remove", ",".join(categories)))


def build_external_ytdlp_command(
    executable: str | Path,
    url: str,
    options: Mapping[str, Any],
    *,
    download: bool,
) -> list[str]:
    """Translate the app's bounded YoutubeDL options to official CLI flags."""

    command = [str(executable), "--ignore-config", "--encoding", "utf-8", "--no-color"]
    _append_output_and_selection_options(command, options)
    _append_network_and_runtime_options(command, options)

    if download:
        _append_download_file_options(command, options)
        _append_postprocessor_options(command, options)
        command.extend(
            (
                "--no-simulate",
                "--quiet",
                "--progress",
                "--newline",
                "--progress-template",
                f"download:{PROGRESS_PREFIX}%()j",
                "--progress-template",
                f"postprocess:{POSTPROCESS_PREFIX}%()j",
                "--print",
                f"after_move:{RESULT_PREFIX}%()j",
            )
        )
    else:
        command.extend((("--dump-json" if options.get("dump_json") else "--dump-single-json"), "--skip-download"))
    command.extend(("--", str(url)))
    return command


@dataclass(slots=True)
class _ExternalOutputState:
    download: bool
    results: list[dict[str, Any]] = field(default_factory=list)
    probe_results: list[dict[str, Any]] = field(default_factory=list)
    recent_lines: list[str] = field(default_factory=list)

    @staticmethod
    def _json_object(value: str) -> dict[str, Any] | None:
        try:
            decoded = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return None
        return decoded if isinstance(decoded, dict) else None

    def consume(
        self,
        line: str,
        *,
        progress_hook: Callable[[dict[str, Any]], None] | None,
        postprocess_hook: Callable[[dict[str, Any]], None] | None,
        log_line: Callable[[str], None] | None,
    ) -> None:
        for prefix, callback in (
            (PROGRESS_PREFIX, progress_hook),
            (POSTPROCESS_PREFIX, postprocess_hook),
            (RESULT_PREFIX, None),
        ):
            if not line.startswith(prefix):
                continue
            payload = self._json_object(line[len(prefix):])
            if payload is None:
                break
            if prefix == RESULT_PREFIX:
                self.results.append(payload)
            elif callback is not None:
                callback(payload)
            return

        if not self.download:
            payload = self._json_object(line)
            if payload is not None:
                self.probe_results.append(payload)
                return
        self.recent_lines.append(line)
        del self.recent_lines[:-30]
        if log_line is not None:
            log_line(line)

    def result(self, url: str) -> dict[str, Any]:
        values = self.results if self.download else self.probe_results
        if not values:
            message = (
                "外置 yt-dlp 未返回下载完成的媒体信息"
                if self.download
                else "外置 yt-dlp 未返回可解析的视频信息"
            )
            raise ExternalYtdlpError(message)
        if len(values) == 1:
            return values[0]
        first = values[0]
        playlist_id = str(first.get("playlist_id") or "")
        playlist_title = str(first.get("playlist_title") or first.get("playlist") or "")
        return {
            "_type": "playlist",
            "id": playlist_id,
            "title": playlist_title,
            "extractor": first.get("extractor"),
            "extractor_key": first.get("extractor_key"),
            "entries": values,
            "playlist_count": len(values),
            "webpage_url": str(url),
        }


def _read_process_output(
    process: subprocess.Popen[str],
    lines: queue.Queue[str | None],
) -> None:
    try:
        assert process.stdout is not None
        for raw_line in process.stdout:
            lines.put(raw_line.rstrip("\r\n"))
    except (OSError, ValueError):
        pass
    finally:
        lines.put(None)


def terminate_external_ytdlp_process(process: subprocess.Popen[str]) -> None:
    """Stop yt-dlp and descendants without leaving a background process tree."""

    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
                timeout=5,
            )
            if result.returncode == 0:
                return
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        process.terminate()
    except OSError:
        return
    try:
        process.wait(timeout=3)
        return
    except OSError:
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        process.kill()
    except OSError:
        return
    try:
        process.wait(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        pass


def start_external_ytdlp_process(command: list[str]) -> subprocess.Popen[str]:
    """Start yt-dlp with the process-group policy shared by every workflow."""

    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        return subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise ExternalYtdlpError(f"无法启动外置 yt-dlp：{exc}") from exc


def start_external_ytdlp_output_reader(
    process: subprocess.Popen[str],
) -> tuple[queue.Queue[str | None], threading.Thread]:
    lines: queue.Queue[str | None] = queue.Queue()
    reader = threading.Thread(
        target=_read_process_output,
        args=(process, lines),
        name="external-ytdlp-output",
        daemon=True,
    )
    try:
        reader.start()
    except RuntimeError as exc:
        terminate_external_ytdlp_process(process)
        if process.stdout is not None:
            try:
                process.stdout.close()
            except (OSError, ValueError):
                pass
        raise ExternalYtdlpError(f"无法读取外置 yt-dlp 输出：{exc}") from exc
    return lines, reader


def pump_external_ytdlp_output(
    process: subprocess.Popen[str],
    reader: threading.Thread,
    lines: queue.Queue[str | None],
    *,
    cancel_event: threading.Event,
    consume_line: Callable[[str], None],
) -> int:
    """Pump complete output lines until yt-dlp exits or cancellation is requested."""

    exit_observed_at: float | None = None
    while True:
        if cancel_event.is_set():
            raise InterruptedError("用户取消下载")

        return_code = process.poll()
        if return_code is not None:
            if exit_observed_at is None:
                exit_observed_at = time.monotonic()
            if not reader.is_alive() and lines.empty():
                return return_code
            if time.monotonic() - exit_observed_at >= _OUTPUT_DRAIN_GRACE_SECONDS:
                # A descendant may have inherited stdout after yt-dlp exited.
                # Do not keep the task worker blocked on that foreign handle.
                return return_code

        try:
            line = lines.get(timeout=_OUTPUT_POLL_SECONDS)
        except queue.Empty:
            continue
        if line is None:
            return process.wait(timeout=_PROCESS_WAIT_TIMEOUT_SECONDS)
        if line:
            consume_line(line)


def finish_external_ytdlp_output_reader(
    process: subprocess.Popen[str],
    reader: threading.Thread,
) -> None:
    reader.join(timeout=_READER_JOIN_TIMEOUT_SECONDS)
    stdout = process.stdout
    if stdout is None or reader.is_alive():
        # Closing a stream while another thread is blocked in read() can block
        # the task thread too. The daemon reader owns this rare lingering
        # handle and will release it when the descendant finally exits.
        return
    try:
        stdout.close()
    except (OSError, ValueError):
        pass


def _external_process_error(output: _ExternalOutputState, return_code: int) -> ExternalYtdlpError:
    detail = next(
        (
            line
            for line in reversed(output.recent_lines)
            if "error" in line.casefold()
        ),
        "",
    )
    return ExternalYtdlpError(detail or f"外置 yt-dlp 退出，代码 {return_code}")


def run_external_ytdlp(
    executable: str | Path,
    url: str,
    options: Mapping[str, Any],
    *,
    download: bool,
    cancel_event: threading.Event,
    log_line: Callable[[str], None] | None = None,
    progress_hook: Callable[[dict[str, Any]], None] | None = None,
    postprocess_hook: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run the official standalone executable and return its JSON metadata."""
    command = build_external_ytdlp_command(executable, url, options, download=download)
    process = start_external_ytdlp_process(command)
    lines, reader = start_external_ytdlp_output_reader(process)
    output = _ExternalOutputState(download=download)
    try:
        return_code = pump_external_ytdlp_output(
            process,
            reader,
            lines,
            cancel_event=cancel_event,
            consume_line=lambda line: output.consume(
                line,
                progress_hook=progress_hook,
                postprocess_hook=postprocess_hook,
                log_line=log_line,
            ),
        )
    except BaseException:
        terminate_external_ytdlp_process(process)
        raise
    finally:
        finish_external_ytdlp_output_reader(process, reader)

    if return_code != 0:
        raise _external_process_error(output, return_code)
    return output.result(str(url))

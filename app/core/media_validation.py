from __future__ import annotations

import json
import math
import os
import re
import stat
import subprocess
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping, Sequence


DEFAULT_MEDIA_VALIDATION_TIMEOUT_SECONDS = 20.0
MAX_DIAGNOSTIC_LENGTH = 240
_PROBE_CANCEL_POLL_SECONDS = 0.1
_PROBE_TERMINATE_GRACE_SECONDS = 1.0
_IS_WINDOWS = os.name == "nt"


class MediaValidationErrorCode(str, Enum):
    """Stable error codes for UI, logging and retry decisions."""

    INVALID_ARGUMENT = "invalid_argument"
    CANCELLED = "cancelled"
    FILE_NOT_FOUND = "file_not_found"
    NOT_REGULAR_FILE = "not_regular_file"
    EMPTY_FILE = "empty_file"
    FILE_UNREADABLE = "file_unreadable"
    FFPROBE_NOT_FOUND = "ffprobe_not_found"
    FFPROBE_FAILED = "ffprobe_failed"
    FFPROBE_TIMEOUT = "ffprobe_timeout"
    FFPROBE_INVALID_OUTPUT = "ffprobe_invalid_output"
    NO_VIDEO_STREAM = "no_video_stream"
    NO_AUDIO_STREAM = "no_audio_stream"
    INVALID_DURATION = "invalid_duration"


class MediaValidationError(RuntimeError):
    """A structured, UI-safe media validation failure.

    ``diagnostic`` is intentionally limited to sanitized, short information.
    Callers must not attach the original ffprobe stdout/stderr to this error.
    """

    def __init__(
        self,
        code: MediaValidationErrorCode,
        message: str,
        action: str,
        *,
        diagnostic: str = "",
    ) -> None:
        self.code = code
        self.message = str(message).strip()
        self.action = str(action).strip()
        self.diagnostic = str(diagnostic).strip()
        super().__init__(f"{self.message} {self.action}".strip())

    def as_dict(self) -> dict[str, str]:
        """Return fields suitable for a UI event or structured application log."""

        return {
            "code": self.code.value,
            "message": self.message,
            "action": self.action,
            "diagnostic": self.diagnostic,
        }


@dataclass(frozen=True, slots=True)
class MediaValidationResult:
    """Immutable facts collected from a successfully validated media file."""

    file_path: str
    size_bytes: int
    duration_seconds: float
    container: str
    container_description: str
    stream_count: int
    video_stream_count: int
    audio_stream_count: int
    subtitle_stream_count: int
    other_stream_count: int
    # Keep the already parsed FFprobe document so callers that need richer
    # stream topology do not launch FFprobe a second time for the same file.
    probe_payload: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def has_video(self) -> bool:
        return self.video_stream_count > 0

    @property
    def has_audio(self) -> bool:
        return self.audio_stream_count > 0


@dataclass(frozen=True, slots=True)
class _ProbeStreamSummary:
    streams: tuple[Mapping[str, Any], ...]
    video_stream_count: int
    audio_stream_count: int
    subtitle_stream_count: int
    other_stream_count: int
    media_durations: tuple[float, ...]


def _failure(
    code: MediaValidationErrorCode,
    message: str,
    action: str,
    *,
    diagnostic: str = "",
) -> MediaValidationError:
    return MediaValidationError(code, message, action, diagnostic=diagnostic)


def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise _failure(
            MediaValidationErrorCode.CANCELLED,
            "媒体成品校验已取消。",
            "需要时可重新开始该任务。",
        )


def _safe_text(value: Any, *, maximum: int) -> str:
    text = str(value or "")
    text = " ".join(text.split())
    text = "".join(character for character in text if character.isprintable())
    if len(text) > maximum:
        return text[: maximum - 1].rstrip() + "…"
    return text


def _sanitize_probe_diagnostic(value: Any, media_path: Path) -> str:
    """Return a short diagnostic without URLs, credentials or the input path."""

    text = str(value or "")
    if not text:
        return ""

    path_variants = {
        str(media_path),
        str(media_path.absolute()),
        str(media_path).replace("\\", "/"),
        str(media_path).replace("/", "\\"),
    }
    for path_value in sorted(path_variants, key=len, reverse=True):
        if path_value:
            text = re.sub(re.escape(path_value), "<输入文件>", text, flags=re.IGNORECASE)

    text = re.sub(r"(?i)\b(?:https?|ftp)://[^\s\]\[<>\"']+", "<网址已隐藏>", text)
    text = re.sub(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+", "Bearer <已隐藏>", text)
    secret_pattern = re.compile(
        r"(?i)\b(authorization|proxy-authorization|cookie|set-cookie|"
        r"access[_-]?token|refresh[_-]?token|token|api[_-]?key|password|passwd)"
        r"\s*[:=]\s*([^\s,;]+)"
    )
    text = secret_pattern.sub(lambda match: f"{match.group(1)}=<已隐藏>", text)
    return _safe_text(text, maximum=MAX_DIAGNOSTIC_LENGTH)


def _validate_source_file(media_path: Path) -> int:
    try:
        file_stat = media_path.stat()
    except FileNotFoundError as exc:
        raise _failure(
            MediaValidationErrorCode.FILE_NOT_FOUND,
            "下载成品不存在，可能已被移动或删除。",
            "请确认下载保存目录可用后重新下载。",
        ) from exc
    except OSError as exc:
        raise _failure(
            MediaValidationErrorCode.FILE_UNREADABLE,
            "无法读取下载成品的文件信息。",
            "请检查文件权限、磁盘连接和安全软件拦截后重试。",
            diagnostic=f"系统错误码：{getattr(exc, 'errno', None) or '未知'}",
        ) from exc

    if not stat.S_ISREG(file_stat.st_mode):
        raise _failure(
            MediaValidationErrorCode.NOT_REGULAR_FILE,
            "下载结果不是普通文件。",
            "请选择有效的媒体文件，或删除任务后重新下载。",
        )
    if file_stat.st_size <= 0:
        raise _failure(
            MediaValidationErrorCode.EMPTY_FILE,
            "下载成品是空文件。",
            "请检查网络与剩余磁盘空间，然后重新下载。",
        )

    try:
        with media_path.open("rb") as stream:
            if not stream.read(1):
                raise OSError("unexpected end of file")
    except OSError as exc:
        raise _failure(
            MediaValidationErrorCode.FILE_UNREADABLE,
            "下载成品无法读取。",
            "请检查文件权限、磁盘连接和安全软件拦截后重试。",
            diagnostic=f"系统错误码：{getattr(exc, 'errno', None) or '未知'}",
        ) from exc
    return int(file_stat.st_size)


def _timeout_value(value: float) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise _failure(
            MediaValidationErrorCode.INVALID_ARGUMENT,
            "媒体校验超时设置无效。",
            "请将超时时间设置为大于 0 的秒数。",
        ) from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise _failure(
            MediaValidationErrorCode.INVALID_ARGUMENT,
            "媒体校验超时设置无效。",
            "请将超时时间设置为大于 0 的秒数。",
        )
    return timeout


def _ffprobe_command(executable: str, media_path: Path) -> list[str]:
    return [
        executable,
        "-hide_banner",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_entries",
        (
            "format=format_name,format_long_name,duration:"
            "stream=index,codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,"
            "duration,duration_ts,time_base:stream_tags=language,title:"
            "stream_disposition:chapter=start_time,end_time:chapter_tags=title"
        ),
        "-i",
        str(media_path),
    ]


def _ffprobe_process_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "stdin": subprocess.DEVNULL,
    }
    if _IS_WINDOWS:
        kwargs["creationflags"] = getattr(
            subprocess,
            "CREATE_NO_WINDOW",
            0x08000000,
        )
    return kwargs


def _probe_timeout_failure(timeout_seconds: float) -> MediaValidationError:
    return _failure(
        MediaValidationErrorCode.FFPROBE_TIMEOUT,
        f"媒体成品校验超过 {timeout_seconds:g} 秒，已安全停止。",
        "请检查磁盘状态后重试；若持续超时，请重新下载该任务。",
    )


def _stop_probe_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except OSError:
        pass
    try:
        process.communicate(timeout=_PROBE_TERMINATE_GRACE_SECONDS)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.communicate()
    except OSError:
        pass


def _run_cancelable_ffprobe(
    command: list[str],
    process_kwargs: Mapping[str, Any],
    *,
    timeout_seconds: float,
    cancel_event: threading.Event,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **dict(process_kwargs),
    )
    deadline = time.monotonic() + timeout_seconds
    while True:
        if cancel_event.is_set():
            _stop_probe_process(process)
            _raise_if_cancelled(cancel_event)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _stop_probe_process(process)
            raise _probe_timeout_failure(timeout_seconds)
        try:
            stdout, stderr = process.communicate(
                timeout=min(_PROBE_CANCEL_POLL_SECONDS, remaining)
            )
        except subprocess.TimeoutExpired:
            continue
        return subprocess.CompletedProcess(
            command,
            process.returncode,
            stdout or "",
            stderr or "",
        )


def _run_ffprobe(
    ffprobe_executable: str | Path,
    media_path: Path,
    *,
    timeout_seconds: float,
    cancel_event: threading.Event | None = None,
) -> subprocess.CompletedProcess[str]:
    executable = str(ffprobe_executable or "").strip()
    if not executable:
        raise _failure(
            MediaValidationErrorCode.FFPROBE_NOT_FOUND,
            "未找到 FFprobe，无法验证下载成品。",
            "请在设置中配置 FFmpeg，或将 ffprobe.exe 放到软件目录。",
        )

    command = _ffprobe_command(executable, media_path)
    process_kwargs = _ffprobe_process_kwargs()

    try:
        if cancel_event is not None:
            return _run_cancelable_ffprobe(
                command,
                process_kwargs,
                timeout_seconds=timeout_seconds,
                cancel_event=cancel_event,
            )
        return subprocess.run(
            command,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
            **process_kwargs,
        )
    except subprocess.TimeoutExpired:
        raise _probe_timeout_failure(timeout_seconds) from None
    except FileNotFoundError as exc:
        raise _failure(
            MediaValidationErrorCode.FFPROBE_NOT_FOUND,
            "未找到 FFprobe，无法验证下载成品。",
            "请在设置中配置 FFmpeg，或将 ffprobe.exe 放到软件目录。",
        ) from exc
    except PermissionError as exc:
        raise _failure(
            MediaValidationErrorCode.FFPROBE_FAILED,
            "FFprobe 无法启动。",
            "请检查工具文件权限和安全软件拦截后重试。",
            diagnostic="系统拒绝启动校验工具",
        ) from exc
    except OSError as exc:
        raise _failure(
            MediaValidationErrorCode.FFPROBE_FAILED,
            "FFprobe 启动失败。",
            "请重新配置或更新 FFmpeg 后重试。",
            diagnostic=f"系统错误码：{getattr(exc, 'errno', None) or '未知'}",
        ) from exc


def _positive_number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    return parsed


def _stream_duration(stream: Mapping[str, Any]) -> float | None:
    duration = _positive_number(stream.get("duration"))
    if duration is not None:
        return duration

    duration_ticks = _positive_number(stream.get("duration_ts"))
    time_base = str(stream.get("time_base") or "").strip()
    if duration_ticks is None or not time_base:
        return None
    try:
        duration = float(Fraction(time_base)) * duration_ticks
    except (ValueError, ZeroDivisionError, OverflowError):
        return None
    return duration if math.isfinite(duration) and duration > 0 else None


def _valid_codec(stream: Mapping[str, Any]) -> bool:
    codec_name = str(stream.get("codec_name") or "").strip().lower()
    return codec_name not in {"", "none", "unknown", "n/a"}


def _positive_integer(value: Any) -> bool:
    try:
        return int(value) > 0
    except (TypeError, ValueError, OverflowError):
        return False


def _is_attached_picture(stream: Mapping[str, Any]) -> bool:
    disposition = stream.get("disposition")
    if not isinstance(disposition, Mapping):
        return False
    value = disposition.get("attached_pic")
    return value is True or str(value).strip().lower() in {"1", "true", "yes"}


def _stream_type(stream: Mapping[str, Any]) -> str:
    return str(stream.get("codec_type") or "").strip().casefold()


def _is_playable_video_stream(stream: Mapping[str, Any]) -> bool:
    return (
        _stream_type(stream) == "video"
        and _valid_codec(stream)
        and _positive_integer(stream.get("width"))
        and _positive_integer(stream.get("height"))
        and not _is_attached_picture(stream)
    )


def _is_playable_audio_stream(stream: Mapping[str, Any]) -> bool:
    return _stream_type(stream) == "audio" and _valid_codec(stream)


def _summarize_probe_streams(
    streams_value: Sequence[Any],
) -> _ProbeStreamSummary:
    streams = tuple(
        stream for stream in streams_value if isinstance(stream, Mapping)
    )
    video_stream_count = 0
    audio_stream_count = 0
    subtitle_stream_count = 0
    media_durations: list[float] = []
    for stream in streams:
        playable_video = _is_playable_video_stream(stream)
        playable_audio = _is_playable_audio_stream(stream)
        if playable_video:
            video_stream_count += 1
        elif playable_audio:
            audio_stream_count += 1
        elif _stream_type(stream) == "subtitle":
            subtitle_stream_count += 1
        if playable_video or playable_audio:
            duration = _stream_duration(stream)
            if duration is not None:
                media_durations.append(duration)

    other_stream_count = max(
        0,
        len(streams) - video_stream_count - audio_stream_count - subtitle_stream_count,
    )
    return _ProbeStreamSummary(
        streams=streams,
        video_stream_count=video_stream_count,
        audio_stream_count=audio_stream_count,
        subtitle_stream_count=subtitle_stream_count,
        other_stream_count=other_stream_count,
        media_durations=tuple(media_durations),
    )


def _raise_for_probe_process_failure(
    process: subprocess.CompletedProcess[str],
    media_path: Path,
) -> None:
    if process.returncode == 0:
        return
    diagnostic = _sanitize_probe_diagnostic(process.stderr, media_path)
    return_code = int(process.returncode) if isinstance(process.returncode, int) else "未知"
    prefix = f"FFprobe 返回码：{return_code}"
    diagnostic = _safe_text(
        f"{prefix}；{diagnostic}" if diagnostic else prefix,
        maximum=MAX_DIAGNOSTIC_LENGTH,
    )
    raise _failure(
        MediaValidationErrorCode.FFPROBE_FAILED,
        "下载成品无法被媒体工具识别，文件可能不完整或已损坏。",
        "请重新下载该任务；若问题重复出现，请更新 FFmpeg。",
        diagnostic=diagnostic,
    )


def _decode_probe_payload(process: subprocess.CompletedProcess[str]) -> Mapping[str, Any]:
    try:
        payload = json.loads(process.stdout or "")
    except (json.JSONDecodeError, TypeError, ValueError):
        raise _failure(
            MediaValidationErrorCode.FFPROBE_INVALID_OUTPUT,
            "媒体工具返回了无法识别的校验结果。",
            "请更新或重新配置 FFmpeg 后重试。",
            diagnostic="FFprobe JSON 输出无效",
        ) from None
    if not isinstance(payload, Mapping):
        raise _failure(
            MediaValidationErrorCode.FFPROBE_INVALID_OUTPUT,
            "媒体工具返回了不完整的校验结果。",
            "请更新或重新配置 FFmpeg 后重试。",
            diagnostic="FFprobe JSON 顶层不是对象",
        )
    return payload


def _probe_sections(
    payload: Mapping[str, Any],
) -> tuple[Mapping[str, Any], Sequence[Any]]:
    format_info = payload.get("format")
    streams_value = payload.get("streams")
    if (
        not isinstance(format_info, Mapping)
        or not isinstance(streams_value, Sequence)
        or isinstance(streams_value, (str, bytes, bytearray))
    ):
        raise _failure(
            MediaValidationErrorCode.FFPROBE_INVALID_OUTPUT,
            "媒体工具返回了不完整的校验结果。",
            "请更新或重新配置 FFmpeg 后重试。",
            diagnostic="FFprobe 缺少 format 或 streams 数据",
        )
    return format_info, streams_value


def _validate_required_streams(
    summary: _ProbeStreamSummary,
    *,
    require_video: bool,
    require_audio: bool,
) -> None:
    if require_video and summary.video_stream_count <= 0:
        raise _failure(
            MediaValidationErrorCode.NO_VIDEO_STREAM,
            "下载成品中没有可播放的视频流。",
            "请重新下载该任务，或尝试更换画质后重试。",
        )
    if require_audio and summary.audio_stream_count <= 0:
        raise _failure(
            MediaValidationErrorCode.NO_AUDIO_STREAM,
            "下载成品中没有可播放的音频流。",
            "请重新下载该任务，并确认所选格式包含音频。",
        )


def _validated_duration(
    format_info: Mapping[str, Any],
    summary: _ProbeStreamSummary,
) -> float:
    valid_durations = list(summary.media_durations)
    format_duration = _positive_number(format_info.get("duration"))
    if format_duration is not None:
        valid_durations.append(format_duration)
    if not valid_durations:
        raise _failure(
            MediaValidationErrorCode.INVALID_DURATION,
            "下载成品的播放时长无效。",
            "文件可能尚未写入完整，请重新下载该任务。",
        )
    return max(valid_durations)


def _parse_probe_result(
    process: subprocess.CompletedProcess[str],
    media_path: Path,
    *,
    size_bytes: int,
    require_video: bool,
    require_audio: bool,
) -> MediaValidationResult:
    _raise_for_probe_process_failure(process, media_path)
    payload = _decode_probe_payload(process)
    format_info, streams_value = _probe_sections(payload)
    summary = _summarize_probe_streams(streams_value)
    _validate_required_streams(
        summary,
        require_video=require_video,
        require_audio=require_audio,
    )
    duration_seconds = _validated_duration(format_info, summary)
    return MediaValidationResult(
        file_path=str(media_path),
        size_bytes=size_bytes,
        duration_seconds=duration_seconds,
        container=_safe_text(format_info.get("format_name"), maximum=128) or "未知",
        container_description=_safe_text(format_info.get("format_long_name"), maximum=256),
        stream_count=len(summary.streams),
        video_stream_count=summary.video_stream_count,
        audio_stream_count=summary.audio_stream_count,
        subtitle_stream_count=summary.subtitle_stream_count,
        other_stream_count=summary.other_stream_count,
        probe_payload=dict(payload),
    )


def validate_media_file(
    media_file: str | Path,
    ffprobe_executable: str | Path,
    *,
    require_video: bool = True,
    require_audio: bool = False,
    timeout_seconds: float = DEFAULT_MEDIA_VALIDATION_TIMEOUT_SECONDS,
    cancel_event: threading.Event | None = None,
) -> MediaValidationResult:
    """Validate a completed media file with a bounded ffprobe subprocess.

    The file must exist, be a readable non-empty regular file, contain the
    requested media streams and report a positive duration. ffprobe output is
    parsed as JSON and never forwarded verbatim to user-facing exceptions.
    """

    _raise_if_cancelled(cancel_event)
    try:
        media_path = Path(media_file).expanduser()
    except (TypeError, ValueError, OSError) as exc:
        raise _failure(
            MediaValidationErrorCode.INVALID_ARGUMENT,
            "下载成品路径无效。",
            "请确认下载保存目录后重新下载。",
        ) from exc

    size_bytes = _validate_source_file(media_path)
    timeout = _timeout_value(timeout_seconds)
    _raise_if_cancelled(cancel_event)
    process = _run_ffprobe(
        ffprobe_executable,
        media_path,
        timeout_seconds=timeout,
        cancel_event=cancel_event,
    )
    _raise_if_cancelled(cancel_event)
    return _parse_probe_result(
        process,
        media_path,
        size_bytes=size_bytes,
        require_video=bool(require_video),
        require_audio=bool(require_audio),
    )


__all__ = [
    "DEFAULT_MEDIA_VALIDATION_TIMEOUT_SECONDS",
    "MediaValidationError",
    "MediaValidationErrorCode",
    "MediaValidationResult",
    "validate_media_file",
]

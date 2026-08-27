from __future__ import annotations

import json
import math
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


class TranscodeError(RuntimeError):
    """Raised when a requested post-download conversion cannot complete."""


@dataclass(frozen=True, slots=True)
class MediaStreamInfo:
    codec_type: str
    codec: str
    language: str = ""
    disposition: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ChapterInfo:
    start_seconds: float
    end_seconds: float
    title: str = ""


@dataclass(frozen=True, slots=True)
class VideoStreamInfo:
    codec: str
    width: int
    height: int
    duration_seconds: float
    frame_rate: float = 0.0
    has_audio: bool = False
    audio_stream_count: int = 0
    subtitle_stream_count: int = 0
    attachment_stream_count: int = 0
    data_stream_count: int = 0
    streams: tuple[MediaStreamInfo, ...] = ()
    chapters: tuple[ChapterInfo, ...] = ()
    # Ordinal among FFmpeg's video streams (``0:v:N``), not the global stream
    # index. This skips attached cover-art streams before the real video track.
    primary_video_ordinal: int = 0


def normalize_video_codec_name(value: str | None) -> str:
    codec = str(value or "").strip().casefold().split(".", 1)[0]
    return {
        "avc": "h264",
        "avc1": "h264",
        "h264": "h264",
        "hevc": "h265",
        "hev1": "h265",
        "hvc1": "h265",
        "h265": "h265",
        "av01": "av1",
        "av1": "av1",
    }.get(codec, codec)


def video_stream_info_from_probe_payload(payload: Mapping[str, Any]) -> VideoStreamInfo:
    """Build transcode topology from an FFprobe document already in memory."""

    try:
        streams = [
            item for item in (payload.get("streams") or ())
            if isinstance(item, Mapping)
        ]
        stream, primary_video_ordinal = _primary_video_stream(streams)
        codec = normalize_video_codec_name(stream.get("codec_name"))
        width = _nonnegative_int(stream.get("width"))
        height = _nonnegative_int(stream.get("height"))
        if _rotation(stream) in {90, 270}:
            width, height = height, width
        format_info = payload.get("format")
        duration = _nonnegative_float(
            format_info.get("duration") if isinstance(format_info, Mapping) else 0.0
        )
        frame_rate = _frame_rate(stream)
        media_streams = _media_streams(streams)
        audio_stream_count = sum(item.codec_type == "audio" for item in media_streams)
        subtitle_stream_count = sum(item.codec_type == "subtitle" for item in media_streams)
        attachment_stream_count = sum(item.codec_type == "attachment" for item in media_streams)
        data_stream_count = sum(item.codec_type == "data" for item in media_streams)
        chapters = _chapters(payload)
    except (TypeError, ValueError, OverflowError) as exc:
        raise TranscodeError("FFprobe 未返回可用的视频流信息") from exc
    if not codec:
        raise TranscodeError("FFprobe 未返回当前视频编码格式")
    return VideoStreamInfo(
        codec,
        width,
        height,
        duration,
        frame_rate,
        audio_stream_count > 0,
        audio_stream_count,
        subtitle_stream_count,
        attachment_stream_count,
        data_stream_count,
        media_streams,
        chapters,
        primary_video_ordinal,
    )


def probe_video_stream(
    input_path: str | Path,
    ffprobe_path: str,
    *,
    timeout: float = 20.0,
) -> VideoStreamInfo:
    """Read the primary video codec, geometry and stream topology."""

    source = Path(input_path)
    if not source.is_file():
        raise TranscodeError(f"待转换媒体文件不存在：{source}")
    executable = str(ffprobe_path or "").strip()
    if not executable:
        raise TranscodeError("未找到 FFprobe，无法识别当前视频格式")
    command = [
        executable,
        "-v", "error",
        "-show_entries",
        "stream=index,codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate:stream_tags=language,title,rotate:stream_disposition:stream_side_data=rotation:chapter=start_time,end_time:chapter_tags=title:format=duration",
        "-of", "json",
        str(source),
    ]
    kwargs = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": _probe_timeout(timeout),
        "check": False,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run(command, **kwargs)
    except (OSError, subprocess.SubprocessError) as exc:
        raise TranscodeError(f"无法运行 FFprobe：{exc}") from exc
    if result.returncode != 0:
        raise TranscodeError("FFprobe 无法识别当前视频格式")
    try:
        payload = json.loads(result.stdout or "{}")
    except (TypeError, ValueError) as exc:
        raise TranscodeError("FFprobe 未返回可用的视频流信息") from exc
    if not isinstance(payload, Mapping):
        raise TranscodeError("FFprobe 未返回可用的视频流信息")
    return video_stream_info_from_probe_payload(payload)


def validate_transcode_topology(source: VideoStreamInfo, output: VideoStreamInfo) -> None:
    """Reject a conversion that silently dropped or reassigned stream data."""

    for label, source_count, output_count in (
        ("音轨", source.audio_stream_count, output.audio_stream_count),
        ("字幕流", source.subtitle_stream_count, output.subtitle_stream_count),
        ("附件流", source.attachment_stream_count, output.attachment_stream_count),
        ("数据流", source.data_stream_count, output.data_stream_count),
    ):
        if output_count < source_count:
            raise TranscodeError(
                f"转换成品{label}数量不完整：原文件 {source_count} 条，成品 {output_count} 条"
            )
    if len(output.chapters) < len(source.chapters):
        raise TranscodeError(
            f"转换成品章节数量不完整：原文件 {len(source.chapters)} 个，成品 {len(output.chapters)} 个"
        )

    for stream_type in ("audio", "subtitle"):
        expected = [
            stream for stream in source.streams
            if stream.codec_type == stream_type
        ]
        actual = [
            stream for stream in output.streams
            if stream.codec_type == stream_type
        ]
        _validate_stream_metadata(stream_type, expected, actual)


def _validate_stream_metadata(
    stream_type: str,
    expected: list[MediaStreamInfo],
    actual: list[MediaStreamInfo],
) -> None:
    remaining = list(actual)
    for stream in expected:
        matched_index = next((
            index
            for index, candidate in enumerate(remaining)
            if (not stream.language or candidate.language == stream.language)
            and (
                not stream.disposition
                or set(stream.disposition).issubset(set(candidate.disposition))
            )
        ), None)
        if matched_index is not None:
            remaining.pop(matched_index)
            continue
        if stream.language and not any(
            candidate.language == stream.language for candidate in actual
        ):
            raise TranscodeError(
                f"转换成品丢失了 {stream_type} 流的语言标记：{stream.language}"
            )
        if stream.disposition and not any(
            set(stream.disposition).issubset(set(candidate.disposition))
            for candidate in actual
        ):
            raise TranscodeError(
                f"转换成品丢失了 {stream_type} 流的默认/辅助标记"
            )
        raise TranscodeError(
            f"转换成品的 {stream_type} 流语言与默认/辅助标记对应关系不完整"
        )


def _probe_timeout(value: object) -> float:
    number = _finite_float(value)
    return max(1.0, number) if number is not None and number > 0 else 20.0


def _nonnegative_float(value: object) -> float:
    number = _finite_float(value)
    return number if number is not None and number >= 0 else 0.0


def _finite_float(value: object) -> float | None:
    try:
        number = float(value or 0.0)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _nonnegative_int(value: object) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, min(number, (1 << 31) - 1))


def _enabled(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return math.isfinite(float(value)) and value != 0
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _frame_rate(stream: Mapping[str, Any]) -> float:
    rate_text = str(
        stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "0"
    ).strip()
    try:
        if "/" in rate_text:
            numerator, denominator = rate_text.split("/", 1)
            denominator_value = float(denominator)
            value = float(numerator) / denominator_value if denominator_value else 0.0
        else:
            value = float(rate_text or 0.0)
    except (TypeError, ValueError, OverflowError, ZeroDivisionError):
        return 0.0
    return value if math.isfinite(value) and value > 0 else 0.0


def _disposition(stream: Mapping[str, Any]) -> tuple[str, ...]:
    value = stream.get("disposition")
    if not isinstance(value, Mapping):
        return ()
    return tuple(sorted(
        str(name)
        for name, enabled in value.items()
        if _enabled(enabled)
    ))


def _rotation(stream: Mapping[str, Any]) -> int:
    candidates: list[object] = []
    side_data = stream.get("side_data_list")
    if isinstance(side_data, (list, tuple)):
        candidates.extend(
            item.get("rotation")
            for item in side_data
            if isinstance(item, Mapping) and item.get("rotation") is not None
        )
    tags = stream.get("tags")
    if isinstance(tags, Mapping) and tags.get("rotate") is not None:
        candidates.append(tags.get("rotate"))
    for candidate in candidates:
        value = _finite_float(candidate)
        if value is None:
            continue
        quarter_turn = round(value / 90.0)
        if abs(value - quarter_turn * 90.0) <= 0.5:
            return int(quarter_turn * 90) % 360
    return 0


def _primary_video_stream(
    streams: list[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], int]:
    candidates: list[tuple[Mapping[str, Any], int]] = []
    video_ordinal = 0
    for stream in streams:
        if str(stream.get("codec_type") or "").casefold() != "video":
            continue
        if "attached_pic" not in _disposition(stream):
            candidates.append((stream, video_ordinal))
        video_ordinal += 1
    if not candidates:
        raise TranscodeError("FFprobe 未返回可用的视频流信息")
    return max(
        candidates,
        key=lambda value: (
            "default" in _disposition(value[0]),
            _nonnegative_int(value[0].get("width"))
            * _nonnegative_int(value[0].get("height")),
            -value[1],
        ),
    )


def _media_streams(streams: list[Mapping[str, Any]]) -> tuple[MediaStreamInfo, ...]:
    values: list[MediaStreamInfo] = []
    for stream in streams:
        tags = stream.get("tags") if isinstance(stream.get("tags"), Mapping) else {}
        values.append(MediaStreamInfo(
            codec_type=str(stream.get("codec_type") or "").casefold(),
            codec=str(stream.get("codec_name") or "").casefold(),
            language=str(tags.get("language") or "").casefold(),
            disposition=_disposition(stream),
        ))
    return tuple(values)


def _chapters(payload: Mapping[str, Any]) -> tuple[ChapterInfo, ...]:
    values: list[ChapterInfo] = []
    for chapter in payload.get("chapters") or ():
        if not isinstance(chapter, Mapping):
            continue
        start = _finite_float(chapter.get("start_time"))
        end = _finite_float(chapter.get("end_time"))
        if start is None or end is None or start < 0 or end < start:
            continue
        tags = chapter.get("tags") if isinstance(chapter.get("tags"), Mapping) else {}
        values.append(ChapterInfo(start, end, str(tags.get("title") or "")))
    return tuple(values)

from __future__ import annotations

import math
import os
import queue
import re
import shutil
import subprocess
import threading
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterable, Iterator

from app.core.media_probe import (
    ChapterInfo,
    TranscodeError,
    VideoStreamInfo,
    normalize_video_codec_name,
)


TRANSCODE_CODECS = frozenset({"original", "h264", "h265", "av1"})
TRANSCODE_DEVICES = frozenset({"auto", "gpu", "cpu"})
TRANSCODE_ENCODERS = {
    "libx264": ("h264", "cpu"),
    "libx265": ("h265", "cpu"),
    "libsvtav1": ("av1", "cpu"),
    "libaom-av1": ("av1", "cpu"),
    "librav1e": ("av1", "cpu"),
    "h264_nvenc": ("h264", "gpu"),
    "hevc_nvenc": ("h265", "gpu"),
    "av1_nvenc": ("av1", "gpu"),
    "h264_qsv": ("h264", "gpu"),
    "hevc_qsv": ("h265", "gpu"),
    "av1_qsv": ("av1", "gpu"),
    "h264_amf": ("h264", "gpu"),
    "hevc_amf": ("h265", "gpu"),
    "av1_amf": ("av1", "gpu"),
}
TRANSCODE_ENCODER_ORDER = (
    "libx264", "libx265", "libsvtav1", "libaom-av1", "librav1e",
    "h264_nvenc", "hevc_nvenc", "av1_nvenc",
    "h264_qsv", "hevc_qsv", "av1_qsv",
    "h264_amf", "hevc_amf", "av1_amf",
)
_MP4_COPY_AUDIO_CODECS = frozenset({"aac", "mp3", "ac3", "eac3", "alac"})
_MP4_COPY_SUBTITLE_CODECS = frozenset({"mov_text", "tx3g"})


@dataclass(slots=True)
class PublishedTranscode:
    """A validated transcode published while retaining a rollback path."""

    source_path: Path
    final_path: Path
    encoder: str
    preserve_source: bool
    backup_path: Path | None = None

    @staticmethod
    def _recovery_path(final_path: Path) -> Path:
        recovery = final_path.with_name(
            f"{final_path.stem}.uncommitted{final_path.suffix}"
        )
        counter = 1
        while recovery.exists():
            recovery = final_path.with_name(
                f"{final_path.stem}.uncommitted-{counter}{final_path.suffix}"
            )
            counter += 1
        return recovery

    def rollback(self) -> Path | None:
        """Restore the original and keep the converted file for diagnosis."""

        recovery: Path | None = None
        if self.backup_path is not None and self.backup_path.exists():
            if self.final_path.exists():
                recovery = self._recovery_path(self.final_path)
                self.final_path.replace(recovery)
            try:
                self.backup_path.replace(self.source_path)
            except BaseException:
                if (
                    recovery is not None
                    and recovery.exists()
                    and not self.final_path.exists()
                ):
                    try:
                        recovery.replace(self.final_path)
                        recovery = None
                    except OSError:
                        pass
                raise
            self.backup_path = None
        elif self.final_path != self.source_path and self.final_path.exists():
            recovery = self._recovery_path(self.final_path)
            self.final_path.replace(recovery)
        return recovery

    def finalize(self) -> None:
        """Finish a successful database commit and remove the obsolete source."""

        if self.backup_path is not None:
            self.backup_path.unlink(missing_ok=True)
            self.backup_path = None
        if (
            self.final_path != self.source_path
            and not self.preserve_source
            and self.source_path.exists()
        ):
            self.source_path.unlink()


@dataclass(slots=True)
class PreparedTranscode:
    """FFmpeg output that is not visible as the task's final file yet."""

    source_path: Path
    target_path: Path
    temporary_path: Path
    encoder: str
    preserve_source: bool

    @staticmethod
    def _same_volume(source: Path, target: Path) -> bool:
        if os.name == "nt":
            source_drive = os.path.splitdrive(str(source.resolve()))[0].casefold()
            target_drive = os.path.splitdrive(str(target.resolve()))[0].casefold()
            if source_drive or target_drive:
                return source_drive == target_drive
        try:
            return source.stat().st_dev == target.parent.stat().st_dev
        except OSError:
            return False

    @staticmethod
    def _copy_and_flush(source: Path, target: Path) -> None:
        shutil.copy2(source, target)
        with target.open("rb+") as stream:
            stream.flush()
            os.fsync(stream.fileno())
        if source.stat().st_size != target.stat().st_size:
            raise TranscodeError("跨磁盘提交后的文件大小不一致，已停止替换原文件")

    def commit(self) -> PublishedTranscode:
        if not self.temporary_path.is_file() or self.temporary_path.stat().st_size <= 0:
            raise TranscodeError("待提交的转换成品不存在或为空")
        backup: Path | None = None
        publish_source = self.temporary_path
        cross_volume_staging: Path | None = None
        if not self._same_volume(self.temporary_path, self.target_path):
            cross_volume_staging = self.target_path.with_name(
                f".{self.target_path.name}.commit-{os.getpid()}-{threading.get_ident()}.part"
            )
            cross_volume_staging.unlink(missing_ok=True)
            try:
                self._copy_and_flush(self.temporary_path, cross_volume_staging)
            except BaseException:
                cross_volume_staging.unlink(missing_ok=True)
                raise
            publish_source = cross_volume_staging
        if self.target_path == self.source_path:
            backup = self.source_path.with_name(
                f".{self.source_path.name}.pre-transcode-{os.getpid()}-{threading.get_ident()}.bak"
            )
            backup.unlink(missing_ok=True)
            self.source_path.replace(backup)
            try:
                publish_source.replace(self.target_path)
            except BaseException:
                backup.replace(self.source_path)
                if cross_volume_staging is not None:
                    cross_volume_staging.unlink(missing_ok=True)
                raise
        else:
            if self.target_path.exists():
                if cross_volume_staging is not None:
                    cross_volume_staging.unlink(missing_ok=True)
                raise TranscodeError(f"转换目标文件已存在，未覆盖：{self.target_path.name}")
            try:
                publish_source.replace(self.target_path)
            except BaseException:
                if cross_volume_staging is not None:
                    cross_volume_staging.unlink(missing_ok=True)
                raise
        if cross_volume_staging is not None:
            # Publication has already succeeded and the rollback handle below
            # is now authoritative. Failure to remove the old temporary copy
            # must not discard that handle or strand the original in backup.
            try:
                self.temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        return PublishedTranscode(
            source_path=self.source_path,
            final_path=self.target_path,
            encoder=self.encoder,
            preserve_source=self.preserve_source,
            backup_path=backup,
        )

    def discard(self) -> None:
        self.temporary_path.unlink(missing_ok=True)


def normalize_transcode_codec(value: str | None) -> str:
    codec = str(value or "").strip().casefold()
    return codec if codec in TRANSCODE_CODECS else "original"


def normalize_transcode_device(value: str | None) -> str:
    device = str(value or "").strip().casefold()
    return device if device in TRANSCODE_DEVICES else "auto"


def normalize_transcode_container(value: str | None) -> str:
    container = str(value or "auto").strip().casefold().lstrip(".")
    return container if container in {"auto", "mp4", "mkv"} else "auto"


def normalize_transcode_encoder(value: str | None) -> str:
    encoder = str(value or "").strip().casefold()
    if encoder in {"", "original", "copy", "none"}:
        return "original"
    return encoder if encoder in TRANSCODE_ENCODERS else "original"


def transcode_encoder_codec(encoder: str | None) -> str:
    normalized = normalize_transcode_encoder(encoder)
    return TRANSCODE_ENCODERS.get(normalized, ("original", "auto"))[0]


def transcode_encoder_device(encoder: str | None) -> str:
    normalized = normalize_transcode_encoder(encoder)
    return TRANSCODE_ENCODERS.get(normalized, ("original", "auto"))[1]


@lru_cache(maxsize=8)
def available_ffmpeg_encoders(ffmpeg_path: str) -> frozenset[str]:
    executable = str(ffmpeg_path or "").strip()
    if not executable:
        return frozenset()
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            [executable, "-hide_banner", "-encoders"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
            creationflags=creationflags,
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    found: set[str] = set()
    for line in result.stdout.splitlines():
        match = re.match(r"^\s*[A-Z.]{6}\s+([^\s]+)", line, re.IGNORECASE)
        if match:
            found.add(match.group(1).casefold())
    return frozenset(found)


def _resolved_ffmpeg_executable(ffmpeg_path: str) -> str:
    candidate = str(ffmpeg_path or "").strip()
    if not candidate:
        return ""
    path = Path(candidate)
    if path.is_file():
        return str(path)
    return str(shutil.which(candidate) or "")


@lru_cache(maxsize=32)
def ffmpeg_encoder_usable(ffmpeg_path: str, encoder: str) -> bool:
    """Verify that a compiled hardware encoder can open on this machine.

    ``ffmpeg -encoders`` only describes build-time support. A listed NVENC,
    QSV or AMF encoder can still be unusable because the matching GPU, driver
    or vendor runtime is absent. Encoding one tiny generated frame gives the
    real answer before a long media conversion starts.
    """
    executable = _resolved_ffmpeg_executable(ffmpeg_path)
    if not executable:
        return False
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    command = [
        executable, "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=black:s=128x72:r=1:d=0.04",
        "-frames:v", "1", "-an", "-pix_fmt", "yuv420p",
        "-c:v", str(encoder), *_encoder_options(str(encoder)),
        "-f", "null", "-",
    ]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
            creationflags=creationflags,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def clear_ffmpeg_encoder_cache() -> None:
    """Discard probe results after an app-local FFmpeg replacement."""
    available_ffmpeg_encoders.cache_clear()
    ffmpeg_encoder_usable.cache_clear()


def compiled_transcode_encoders(
    ffmpeg_path: str,
    cancel_event: threading.Event | None = None,
) -> tuple[str, ...]:
    """Return common encoders compiled into FFmpeg without opening a GPU."""

    available = available_ffmpeg_encoders(str(ffmpeg_path or ""))
    compiled: list[str] = []
    for encoder in TRANSCODE_ENCODER_ORDER:
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("编码器检测已取消")
        if encoder in available:
            compiled.append(encoder)
    return tuple(compiled)


def encoder_candidates(codec: str, device: str, available: Iterable[str]) -> tuple[str, ...]:
    codec = normalize_transcode_codec(codec)
    device = normalize_transcode_device(device)
    found = {str(item).casefold() for item in available}
    if codec == "original":
        return ()
    gpu = {
        "h264": ("h264_nvenc", "h264_qsv", "h264_amf"),
        "h265": ("hevc_nvenc", "hevc_qsv", "hevc_amf"),
        "av1": ("av1_nvenc", "av1_qsv", "av1_amf"),
    }[codec]
    cpu = {"h264": "libx264", "h265": "libx265", "av1": "libsvtav1"}[codec]
    gpu_found = tuple(item for item in gpu if item in found)
    if device == "cpu":
        return (cpu,) if cpu in found else ()
    if device == "gpu":
        return gpu_found
    return gpu_found + ((cpu,) if cpu in found else ())


def _responsive_transcode_thread_limit(cpu_count: int | None = None) -> int:
    """Leave enough CPU capacity for Qt, yt-dlp and an active download.

    FFmpeg's software encoders otherwise default to every logical processor.
    Lowering process priority helps scheduling, but encoders such as libx265
    can still keep the machine saturated and make the GUI appear frozen while
    another task is receiving data.  Use roughly 75% of the machine, with a
    practical upper bound that still gives large systems good throughput.
    """

    logical_cpus = max(1, int(cpu_count or os.cpu_count() or 1))
    if logical_cpus <= 2:
        return 1
    reserved = max(1, logical_cpus // 4)
    return max(1, min(16, logical_cpus - reserved))


def _encoder_options(encoder: str, thread_limit: int = 0) -> list[str]:
    if encoder in {"libx264", "libx265"}:
        options = ["-preset", "medium", "-crf", "23" if encoder == "libx264" else "27"]
        # libx265 owns its worker pools internally and may ignore FFmpeg's
        # generic -threads limit.  Cap those pools explicitly as well.
        if encoder == "libx265" and thread_limit > 0:
            options.extend(("-x265-params", f"pools={thread_limit}"))
        return options
    if encoder == "libsvtav1":
        return ["-preset", "8", "-crf", "30"]
    if encoder == "libaom-av1":
        return ["-cpu-used", "6", "-crf", "30", "-b:v", "0"]
    if encoder == "librav1e":
        return ["-speed", "6", "-qp", "100"]
    if encoder.endswith("_nvenc"):
        return ["-preset", "p5", "-rc", "vbr", "-cq", "23", "-b:v", "0"]
    if encoder.endswith("_qsv"):
        return ["-preset", "medium", "-global_quality", "23"]
    if encoder.endswith("_amf"):
        return ["-quality", "balanced", "-rc", "cqp", "-qp_i", "23", "-qp_p", "23"]
    return []


def _progress_seconds(key: str, value: str) -> float | None:
    try:
        if key in {"out_time_us", "out_time_ms"}:
            return max(0.0, float(value) / 1_000_000.0)
        if key == "out_time":
            hours, minutes, seconds = value.split(":", 2)
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except (TypeError, ValueError):
        return None
    return None


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=3)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        process.kill()
    except OSError:
        return
    try:
        process.wait(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _raise_if_transcode_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("用户取消格式转换")


def _wait_transcode_process(
    process: subprocess.Popen[str],
    cancel_event: threading.Event | None,
) -> int:
    """Wait for FFmpeg without losing cooperative cancellation."""

    while True:
        _raise_if_transcode_cancelled(cancel_event)
        return_code = process.poll()
        if return_code is not None:
            return int(return_code)
        try:
            return int(process.wait(timeout=0.2))
        except subprocess.TimeoutExpired:
            continue


def _ffmetadata_escape(value: str) -> str:
    return (
        str(value or "")
        .replace("\\", "\\\\")
        .replace("=", "\\=")
        .replace(";", "\\;")
        .replace("#", "\\#")
        .replace("\n", "\\n")
    )


def _write_shifted_chapters(
    path: Path,
    chapters: tuple[ChapterInfo, ...],
    offset_seconds: float,
) -> None:
    lines = [";FFMETADATA1"]
    offset_ms = max(0, int(round(offset_seconds * 1000.0)))
    # FFmpeg normalizes an ffmetadata input whose first chapter starts after
    # zero. Represent the inserted cover frames as a real leading chapter so
    # the original chapter timestamps retain their intended positive offset.
    if offset_ms:
        lines.extend((
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            "START=0",
            f"END={max(1, offset_ms)}",
            "title=Opening cover",
        ))
    for chapter in chapters:
        start_ms = max(0, int(round(chapter.start_seconds * 1000.0))) + offset_ms
        end_ms = max(start_ms + 1, int(round(chapter.end_seconds * 1000.0)) + offset_ms)
        lines.extend((
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={start_ms}",
            f"END={end_ms}",
            f"title={_ffmetadata_escape(chapter.title)}",
        ))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _stream_metadata_options(source_info: VideoStreamInfo | None) -> list[str]:
    if source_info is None:
        return []
    options: list[str] = []
    indexes = {"audio": 0, "subtitle": 0}
    specifiers = {"audio": "a", "subtitle": "s"}
    for stream in source_info.streams:
        if stream.codec_type not in indexes:
            continue
        index = indexes[stream.codec_type]
        indexes[stream.codec_type] += 1
        specifier = specifiers[stream.codec_type]
        if stream.language:
            options.extend((f"-metadata:s:{specifier}:{index}", f"language={stream.language}"))
        if stream.disposition:
            options.extend((f"-disposition:{specifier}:{index}", "+".join(stream.disposition)))
    return options


@dataclass(frozen=True, slots=True)
class _TranscodeCommandSpec:
    executable: str
    source: Path
    temporary: Path
    target_container: str
    prepend_cover: bool
    cover: Path | None
    cover_frames: int
    source_width: int
    source_height: int
    source_frame_rate: float
    source_has_audio: bool
    source_info: VideoStreamInfo | None


@dataclass(frozen=True, slots=True)
class _TranscodeRequest:
    source: Path
    executable: str
    codec: str
    device: str
    encoder: str
    preserve_source: bool
    cover: Path | None
    cover_frames: int
    source_codec: str
    source_width: int
    source_height: int
    source_frame_rate: float
    source_has_audio: bool
    source_info: VideoStreamInfo | None
    temporary_dir: Path | None
    output_container: str


@dataclass(frozen=True, slots=True)
class _ResolvedTranscodeSettings:
    codec: str
    device: str
    selected_encoder: str
    target_container: str
    prepend_cover: bool
    source_width: int
    source_height: int
    source_frame_rate: float
    source_has_audio: bool


@dataclass(frozen=True, slots=True)
class _TranscodeAttemptResult:
    return_code: int
    last_lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _TranscodePlan:
    command_spec: _TranscodeCommandSpec
    target: Path
    candidates: tuple[str, ...]
    codec: str
    device: str
    preserve_source: bool


def _cover_timing(spec: _TranscodeCommandSpec) -> tuple[float, float, int]:
    frame_rate = float(spec.source_frame_rate or 30.0)
    if not math.isfinite(frame_rate) or frame_rate <= 0:
        frame_rate = 30.0
    frame_rate = max(1.0, min(frame_rate, 240.0))
    cover_seconds = spec.cover_frames / frame_rate
    audio_count = (
        spec.source_info.audio_stream_count
        if spec.source_info is not None
        else (1 if spec.source_has_audio else 0)
    )
    return frame_rate, cover_seconds, max(0, int(audio_count or 0))


@contextmanager
def _shifted_chapter_metadata(
    spec: _TranscodeCommandSpec,
) -> Iterator[Path | None]:
    chapters = spec.source_info.chapters if spec.source_info is not None else ()
    if not spec.prepend_cover or not chapters:
        yield None
        return
    _, cover_seconds, _ = _cover_timing(spec)
    path = spec.temporary.with_suffix(".chapters.ffmeta")
    try:
        _write_shifted_chapters(path, chapters, cover_seconds)
        yield path
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _best_effort_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _stream_codecs_are_compatible(
    source_info: VideoStreamInfo | None,
    codec_type: str,
    declared_count: int,
    compatible: frozenset[str],
) -> bool:
    """Only copy into a strict container when every source codec is known."""

    if source_info is None:
        return False
    if declared_count <= 0:
        return True
    codecs = [
        stream.codec.casefold()
        for stream in source_info.streams
        if stream.codec_type == codec_type and stream.codec
    ]
    return len(codecs) == declared_count and all(codec in compatible for codec in codecs)


def _cover_input_and_mapping_options(
    spec: _TranscodeCommandSpec,
    chapter_metadata: Path | None,
) -> list[str]:
    if spec.cover is None:
        raise TranscodeError("用于插入视频开头的封面文件不存在")
    frame_rate, cover_seconds, audio_count = _cover_timing(spec)
    width = int(spec.source_width)
    height = int(spec.source_height)
    video_ordinal = (
        spec.source_info.primary_video_ordinal
        if spec.source_info is not None else 0
    )
    filter_parts = [
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"setsar=1,fps={frame_rate:g},trim=end_frame={spec.cover_frames},"
        "setpts=PTS-STARTPTS[coverv]",
        f"[1:v:{video_ordinal}]fps={frame_rate:g},setpts=PTS-STARTPTS[mainv];"
        "[coverv][mainv]concat=n=2:v=1:a=0[v]",
    ]
    delay_ms = max(1, int(round(cover_seconds * 1000)))
    filter_parts.extend(
        f"[1:a:{index}]adelay={delay_ms}:all=1[a{index}]"
        for index in range(audio_count)
    )

    options = [
        "-loop",
        "1",
        "-framerate",
        f"{frame_rate:g}",
        "-i",
        str(spec.cover),
        "-i",
        str(spec.source),
    ]
    chapter_input_index = 1
    if chapter_metadata is not None:
        options.extend(("-f", "ffmetadata", "-i", str(chapter_metadata)))
        chapter_input_index = 2
    options.extend(("-filter_complex", ";".join(filter_parts), "-map", "[v]"))
    for index in range(audio_count):
        options.extend(("-map", f"[a{index}]"))
    options.extend((
        "-map",
        "1:s?",
        "-map_metadata",
        "1",
        "-map_chapters",
        str(chapter_input_index),
    ))
    if spec.target_container == "mkv":
        options.extend(("-map", "1:t?", "-map", "1:d?"))
    return options


def _source_input_and_mapping_options(
    spec: _TranscodeCommandSpec,
    *,
    hardware_decode: bool = False,
) -> list[str]:
    video_ordinal = (
        spec.source_info.primary_video_ordinal
        if spec.source_info is not None else 0
    )
    options: list[str] = []
    if hardware_decode:
        options.extend(("-hwaccel", "cuda", "-hwaccel_output_format", "cuda"))
    options.extend((
        "-i",
        str(spec.source),
        "-map",
        f"0:v:{video_ordinal}",
        "-map",
        "0:a?",
        "-map",
        "0:s?",
        "-map_metadata",
        "0",
        "-map_chapters",
        "0",
    ))
    if spec.target_container == "mkv":
        options.extend(("-map", "0:t?", "-map", "0:d?"))
    return options


def _stream_codec_options(
    spec: _TranscodeCommandSpec,
    encoder: str,
    thread_limit: int,
    *,
    hardware_decode: bool = False,
) -> list[str]:
    source_info = spec.source_info
    copy_audio = not spec.prepend_cover and (
        spec.target_container == "mkv"
        or _stream_codecs_are_compatible(
            source_info,
            "audio",
            source_info.audio_stream_count if source_info is not None else 0,
            _MP4_COPY_AUDIO_CODECS,
        )
    )
    copy_subtitles = (
        spec.target_container == "mkv"
        or _stream_codecs_are_compatible(
            source_info,
            "subtitle",
            source_info.subtitle_stream_count if source_info is not None else 0,
            _MP4_COPY_SUBTITLE_CODECS,
        )
    )
    options: list[str] = []
    if not hardware_decode:
        options.extend(("-pix_fmt", "yuv420p"))
    options.extend((
        "-c:v",
        encoder,
        *_encoder_options(encoder, thread_limit),
        "-threads",
        str(thread_limit),
        *_stream_metadata_options(source_info),
        "-c:a",
        "copy" if copy_audio else "aac",
    ))
    if not copy_audio:
        options.extend(("-b:a", "192k"))
    options.extend(("-c:s", "copy" if copy_subtitles else "mov_text"))
    return options


def _container_output_options(target_container: str) -> list[str]:
    if target_container == "mkv":
        return ["-c:t", "copy", "-c:d", "copy"]
    return ["-movflags", "+faststart"]


def _build_transcode_command(
    spec: _TranscodeCommandSpec,
    encoder: str,
    thread_limit: int,
    chapter_metadata: Path | None,
    *,
    hardware_decode: bool = False,
) -> list[str]:
    command = [spec.executable, "-hide_banner", "-y"]
    command.extend(
        _cover_input_and_mapping_options(spec, chapter_metadata)
        if spec.prepend_cover
        else _source_input_and_mapping_options(
            spec,
            hardware_decode=hardware_decode,
        )
    )
    command.extend(
        _stream_codec_options(
            spec,
            encoder,
            thread_limit,
            hardware_decode=hardware_decode,
        )
    )
    command.extend(_container_output_options(spec.target_container))
    command.extend(("-progress", "pipe:1", "-nostats", str(spec.temporary)))
    return command


def _start_transcode_process(
    command: list[str],
    creationflags: int,
) -> subprocess.Popen[str]:
    try:
        return subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
    except OSError as exc:
        raise TranscodeError(f"无法启动 FFmpeg：{exc}") from exc


def _read_transcode_output(
    process: subprocess.Popen[str],
    output_queue: queue.Queue[str | None],
) -> None:
    try:
        if process.stdout is not None:
            for line in process.stdout:
                output_queue.put(line)
    except (OSError, ValueError):
        pass
    finally:
        output_queue.put(None)


def _consume_transcode_output(
    process: subprocess.Popen[str],
    output_queue: queue.Queue[str | None],
    *,
    encoder: str,
    duration_seconds: float,
    cancel_event: threading.Event | None,
    progress: Callable[[float, str], None] | None,
) -> tuple[str, ...]:
    last_lines: deque[str] = deque(maxlen=12)
    while True:
        _raise_if_transcode_cancelled(cancel_event)
        try:
            raw_line = output_queue.get(timeout=0.2)
        except queue.Empty:
            # A child process may inherit stdout and keep the reader alive
            # after FFmpeg itself has exited. Its return code is authoritative.
            if process.poll() is not None:
                break
            continue
        if raw_line is None:
            break
        line = raw_line.strip()
        if line:
            last_lines.append(line)
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        elapsed = _progress_seconds(key, value)
        if elapsed is None or progress is None:
            continue
        percent = (
            min(99.9, elapsed * 100.0 / duration_seconds)
            if duration_seconds > 0 else 0.0
        )
        progress(percent, encoder)
    return tuple(last_lines)


def _close_transcode_output(
    process: subprocess.Popen[str],
    reader: threading.Thread | None,
) -> None:
    close_stdout = getattr(process.stdout, "close", None)
    if callable(close_stdout):
        try:
            close_stdout()
        except OSError:
            pass
    if reader is not None and reader.ident is not None:
        reader.join(timeout=1)


def _run_transcode_attempt(
    command: list[str],
    *,
    encoder: str,
    duration_seconds: float,
    cancel_event: threading.Event | None,
    progress: Callable[[float, str], None] | None,
    creationflags: int,
) -> _TranscodeAttemptResult:
    process = _start_transcode_process(command, creationflags)
    output_queue: queue.Queue[str | None] = queue.Queue()
    reader: threading.Thread | None = None
    try:
        reader = threading.Thread(
            target=_read_transcode_output,
            args=(process, output_queue),
            name="ffmpeg-progress-reader",
            daemon=True,
        )
        reader.start()
        last_lines = _consume_transcode_output(
            process,
            output_queue,
            encoder=encoder,
            duration_seconds=duration_seconds,
            cancel_event=cancel_event,
            progress=progress,
        )
        return _TranscodeAttemptResult(
            return_code=_wait_transcode_process(process, cancel_event),
            last_lines=last_lines,
        )
    except BaseException:
        _terminate_process(process)
        raise
    finally:
        _close_transcode_output(process, reader)


def _automatic_transcode_container(source: Path, requested: str) -> str:
    normalized = normalize_transcode_container(requested)
    if normalized != "auto":
        return normalized
    # Automatic means preserving an already supported source container. This
    # is especially important for MKV attachments/data streams that MP4 cannot
    # represent without silent loss. Other source containers use MP4 as the
    # broadly compatible conversion target.
    return "mkv" if source.suffix.casefold() == ".mkv" else "mp4"


def _resolve_transcode_settings(
    request: _TranscodeRequest,
) -> _ResolvedTranscodeSettings:
    prepend_cover = bool(request.cover_frames and request.cover is not None)
    selected_encoder = (
        normalize_transcode_encoder(request.encoder)
        if request.encoder.strip()
        else ""
    )
    if selected_encoder == "original":
        selected_encoder = ""
    codec = (
        transcode_encoder_codec(selected_encoder)
        if selected_encoder
        else normalize_transcode_codec(request.codec)
    )
    device = (
        transcode_encoder_device(selected_encoder)
        if selected_encoder
        else normalize_transcode_device(request.device)
    )
    target_container = _automatic_transcode_container(
        request.source,
        request.output_container,
    )
    if codec == "original" and not prepend_cover:
        raise TranscodeError("当前设置不需要格式转换")
    if not request.source.is_file():
        raise TranscodeError(f"待转换媒体文件不存在：{request.source}")
    if not request.executable:
        raise TranscodeError("未找到 FFmpeg，无法执行格式转换")
    if prepend_cover and (request.cover is None or not request.cover.is_file()):
        raise TranscodeError("用于插入视频开头的封面文件不存在")

    source_codec = request.source_codec
    source_width = request.source_width
    source_height = request.source_height
    source_frame_rate = request.source_frame_rate
    source_has_audio = request.source_has_audio
    if prepend_cover:
        source_codec = normalize_video_codec_name(
            request.source_info.codec if request.source_info else source_codec
        )
        if codec == "original":
            codec = source_codec if source_codec in {"h264", "h265", "av1"} else "h264"
        source_width = request.source_info.width if request.source_info else source_width
        source_height = request.source_info.height if request.source_info else source_height
        source_frame_rate = (
            request.source_info.frame_rate if request.source_info else source_frame_rate
        )
        source_has_audio = (
            request.source_info.has_audio if request.source_info else source_has_audio
        )
        if source_width <= 0 or source_height <= 0:
            raise TranscodeError("无法识别视频尺寸，不能在开头插入封面")
    if (
        target_container == "mp4"
        and request.source_info
        and (
            request.source_info.attachment_stream_count
            or request.source_info.data_stream_count
        )
    ):
        raise TranscodeError("原文件包含 MP4 无法无损保留的附件或数据流，已停止转换以避免静默丢失")
    return _ResolvedTranscodeSettings(
        codec=codec,
        device=device,
        selected_encoder=selected_encoder,
        target_container=target_container,
        prepend_cover=prepend_cover,
        source_width=source_width,
        source_height=source_height,
        source_frame_rate=source_frame_rate,
        source_has_audio=source_has_audio,
    )


def _resolve_transcode_candidates(
    executable: str,
    settings: _ResolvedTranscodeSettings,
) -> tuple[str, ...]:
    available = available_ffmpeg_encoders(executable)
    candidates = (
        (settings.selected_encoder,)
        if settings.selected_encoder and settings.selected_encoder in available
        else encoder_candidates(settings.codec, settings.device, available)
        if not settings.selected_encoder
        else ()
    )
    if _resolved_ffmpeg_executable(executable):
        candidates = tuple(
            encoder
            for encoder in candidates
            if encoder.startswith("lib")
            or ffmpeg_encoder_usable(executable, encoder)
        )
    if not candidates:
        label = "GPU 硬件编码器" if settings.device == "gpu" else "所需编码器"
        target_label = {
            "h264": "H.264",
            "h265": "H.265",
            "av1": "AV1",
        }[settings.codec]
        raise TranscodeError(f"当前 FFmpeg 未提供可用的 {target_label} {label}")
    return candidates


def _resolve_transcode_paths(
    request: _TranscodeRequest,
    target_container: str,
) -> tuple[Path, Path]:
    target_suffix = f".{target_container}"
    target = (
        request.source
        if request.source.suffix.casefold() == target_suffix
        else request.source.with_suffix(target_suffix)
    )
    if target != request.source and target.exists():
        raise TranscodeError(f"转换目标文件已存在，未覆盖：{target.name}")
    temporary_root = request.temporary_dir or target.parent
    temporary_root.mkdir(parents=True, exist_ok=True)
    temporary = temporary_root / (
        f".{target.stem}.transcoding-{os.getpid()}-{threading.get_ident()}.part{target_suffix}"
    )
    temporary.unlink(missing_ok=True)
    return target, temporary


def _resolve_transcode_plan(request: _TranscodeRequest) -> _TranscodePlan:
    settings = _resolve_transcode_settings(request)
    candidates = _resolve_transcode_candidates(request.executable, settings)
    target, temporary = _resolve_transcode_paths(
        request,
        settings.target_container,
    )
    return _TranscodePlan(
        command_spec=_TranscodeCommandSpec(
            executable=request.executable,
            source=request.source,
            temporary=temporary,
            target_container=settings.target_container,
            prepend_cover=settings.prepend_cover,
            cover=request.cover,
            cover_frames=request.cover_frames,
            source_width=settings.source_width,
            source_height=settings.source_height,
            source_frame_rate=settings.source_frame_rate,
            source_has_audio=settings.source_has_audio,
            source_info=request.source_info,
        ),
        target=target,
        candidates=candidates,
        codec=settings.codec,
        device=settings.device,
        preserve_source=request.preserve_source,
    )


def _execute_transcode_plan(
    plan: _TranscodePlan,
    *,
    duration_seconds: float,
    cancel_event: threading.Event | None,
    progress: Callable[[float, str], None] | None,
) -> PreparedTranscode:
    errors: list[str] = []
    creationflags = (
        getattr(subprocess, "CREATE_NO_WINDOW", 0)
        | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
    )
    thread_limit = _responsive_transcode_thread_limit()
    spec = plan.command_spec
    for candidate_encoder in plan.candidates:
        decode_attempts = (
            (True, False)
            if candidate_encoder.casefold().endswith("_nvenc") and not spec.prepend_cover
            else (False,)
        )
        for hardware_decode in decode_attempts:
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("用户取消格式转换")
            spec.temporary.unlink(missing_ok=True)
            try:
                with _shifted_chapter_metadata(spec) as chapter_metadata:
                    command = _build_transcode_command(
                        spec,
                        candidate_encoder,
                        thread_limit,
                        chapter_metadata,
                        hardware_decode=hardware_decode,
                    )
                    attempt = _run_transcode_attempt(
                        command,
                        encoder=candidate_encoder,
                        duration_seconds=duration_seconds,
                        cancel_event=cancel_event,
                        progress=progress,
                        creationflags=creationflags,
                    )
            except BaseException:
                _best_effort_unlink(spec.temporary)
                raise

            last_lines = list(attempt.last_lines)
            try:
                output_ready = (
                    spec.temporary.is_file()
                    and spec.temporary.stat().st_size > 0
                )
            except OSError as exc:
                output_ready = False
                last_lines.append(f"无法检查转换成品：{exc}")
            if attempt.return_code == 0 and output_ready:
                if progress is not None:
                    progress(100.0, candidate_encoder)
                return PreparedTranscode(
                    spec.source,
                    plan.target,
                    spec.temporary,
                    candidate_encoder,
                    plan.preserve_source,
                )

            detail = (
                " | ".join(last_lines[-4:])
                or f"FFmpeg 退出码 {attempt.return_code}"
            )
            decode_label = "NVDEC" if hardware_decode else "软件解码"
            errors.append(f"{candidate_encoder}/{decode_label}: {detail}")
            _best_effort_unlink(spec.temporary)

    target_label = {"h264": "H.264", "h265": "H.265", "av1": "AV1"}[plan.codec]
    if plan.device == "gpu":
        target_label += " GPU 编码"
    hint = "；可切换为自动或 CPU 编码" if plan.device == "gpu" else ""
    raise TranscodeError(f"{target_label} 转换失败{hint}：{'；'.join(errors)}")


def prepare_transcode_media(
    input_path: str | Path,
    ffmpeg_path: str,
    codec: str,
    device: str = "auto",
    *,
    encoder: str = "",
    duration_seconds: float = 0.0,
    cancel_event: threading.Event | None = None,
    progress: Callable[[float, str], None] | None = None,
    preserve_source: bool = False,
    cover_path: str | Path = "",
    prepend_cover_frames: int = 0,
    source_codec: str = "",
    source_width: int = 0,
    source_height: int = 0,
    source_frame_rate: float = 0.0,
    source_has_audio: bool = False,
    source_info: VideoStreamInfo | None = None,
    temporary_dir: str | Path = "",
    output_container: str = "auto",
) -> PreparedTranscode:
    """Encode to a hidden temporary file without replacing the source.

    The caller validates ``temporary_path`` before publishing it. The original
    stays recoverable until the database update has succeeded.
    """

    request = _TranscodeRequest(
        source=Path(input_path),
        executable=str(ffmpeg_path or "").strip(),
        codec=str(codec or ""),
        device=str(device or ""),
        encoder=str(encoder or "").strip(),
        preserve_source=bool(preserve_source),
        cover=(Path(cover_path) if str(cover_path or "").strip() else None),
        cover_frames=max(0, int(prepend_cover_frames or 0)),
        source_codec=str(source_codec or ""),
        source_width=int(source_width or 0),
        source_height=int(source_height or 0),
        source_frame_rate=float(source_frame_rate or 0.0),
        source_has_audio=bool(source_has_audio),
        source_info=source_info,
        temporary_dir=(
            Path(temporary_dir) if str(temporary_dir or "").strip() else None
        ),
        output_container=str(output_container or "auto"),
    )
    plan = _resolve_transcode_plan(request)
    return _execute_transcode_plan(
        plan,
        duration_seconds=duration_seconds,
        cancel_event=cancel_event,
        progress=progress,
    )

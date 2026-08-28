from __future__ import annotations

import hashlib
import ntpath
import os
import re
import sys
import threading
import time
from copy import deepcopy
from contextlib import contextmanager
from functools import partial
from math import isfinite
from uuid import uuid4
from collections import defaultdict, deque
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Iterator, Mapping

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot

from app.core.network_service import detect_public_ip
from app.core.log_service import DownloadLogService
from app.core.disk_capacity import (
    CapacityEstimate,
    DiskCapacityError,
    DiskCapacityErrorCode,
    DiskCapacitySnapshot,
    DiskReservation,
    DiskReservationManager,
    estimate_download_capacity,
)
from app.core.disk_capacity_lease import DiskReservationLease
from app.core.media_validation import (
    MediaValidationError,
    MediaValidationErrorCode,
    MediaValidationResult,
    validate_media_file,
)
from app.core.media_identity import (
    normalize_media_title,
    normalize_source_key,
    normalize_source_url,
)
from app.core.media_probe import (
    TranscodeError,
    VideoStreamInfo,
    probe_video_stream,
    validate_transcode_topology,
    video_stream_info_from_probe_payload,
)
from app.core.download_thumbnails import DownloadThumbnailManager
from app.core.completed_conversion import (
    CompletedMediaTranscodeWorker,
    transcode_capacity_estimate,
)
from app.core.local_components import activate_local_ejs
from app.core.external_ytdlp import (
    ExternalYtdlpError,
    cached_external_ytdlp_version,
    run_external_ytdlp,
)
from app.core.paths import application_dir, resolve_portable_path, tool_runtime_roots
from app.core.tool_resolver import resolve_ffprobe_tool, resolve_runtime_tool
from app.core.ytdlp_metadata import (
    MediaCapabilityProfile,
    build_format_choices,
    is_ytdlp_collection_result,
    media_capability_profile,
    media_source_key,
    selected_video_quality,
)
from app.core.ytdlp_ejs import normalize_ytdlp_ejs_source
from app.core.transcode_service import (
    PreparedTranscode,
    PublishedTranscode,
    normalize_transcode_codec,
    normalize_transcode_device,
    normalize_transcode_encoder,
    prepare_transcode_media,
    transcode_encoder_codec,
    transcode_encoder_device,
)
from app.core.subtitles import normalize_subtitle_language
from app.core.download_options import DownloadOptions
from app.core.download_performance import normalize_download_performance_values
from app.core.download_progress import (
    StageProgressState,
    TransferCounterState,
    bounded_percent,
    format_eta,
    format_speed,
    merge_stage_progress,
    merge_stream_progress,
    merge_transfer_counters,
    non_negative_float,
    non_negative_int,
    optional_non_negative_float,
)
from app.core.collection_aggregation import (
    CollectionAggregate as _CollectionAggregate,
    CollectionChildContribution as _CollectionChildContribution,
    CollectionSummary as _CollectionSummary,
    collection_child_contribution,
    summarize_collection,
)
from app.core.progress_persistence import ProgressPersistenceBuffer
from app.core.download_runtime_state import (
    download_runtime_signal_is_current,
    finished_download_state,
)
from app.core.qt_lifecycle import delete_unstarted_worker
from app.core.download_task_restore import (
    RestoredTaskHierarchy as _RestoredTaskHierarchy,
    TaskRowReader as _TaskRowReader,
    build_task_restore_plan,
    restored_status,
)
from app.core.download_task_index import DownloadTaskIndex
from app.core.download_queue import DownloadTaskQueue, QueueStartOutcome
from app.core.processing_workspace import (
    cleanup_processing_workspace as _cleanup_processing_workspace,
    final_output_capacity_estimate,
    is_reparse_point as _is_reparse_point,
    processing_temp_workspace,
    processing_temp_workspace_path,
    same_storage_volume,
)
from app.core.cookie_sources import (
    COOKIE_SOURCE_BROWSER,
    COOKIE_SOURCE_EMBEDDED,
    CookieSource,
    MaterializedCookieSource,
    materialize_cookie_source,
)
from app.core.ytdlp_runtime_options import (
    YtdlpDownloadOptionRequest,
    build_ytdlp_download_options,
)
from app.core.ytdlp_logging import (
    SilentYtdlpProbeLogger as _SilentYtdlpProbeLogger,
    YtdlpLogger as _YtdlpLogger,
    normalize_ytdlp_log_message,
    ytdlp_log_level,
)
from app.core.ytdlp_core_selection import (
    YtdlpCoreSelection,
    YtdlpCoreSelectionError,
    normalize_ytdlp_core_mode,
    select_ytdlp_core,
)
from app.storage.database import Database
from app.storage.models import MediaItem

activate_local_ejs()

try:
    import yt_dlp
except ImportError:  # pragma: no cover
    yt_dlp = None


DISK_WATCHDOG_INTERVAL_SECONDS = 2.0
DISK_WATCHDOG_BYTE_STEP = 64 * 1024 * 1024
RESTORE_INITIAL_TERMINAL_CHILDREN = 200
DOWNLOAD_TERMINAL_STATUSES = frozenset({
    "completed",
    "failed",
    "canceled",
    "deleted",
    "partial_failed",
    "paused",
})
DOWNLOAD_RESTORABLE_STATUSES = frozenset({
    *DOWNLOAD_TERMINAL_STATUSES,
    "queued",
    "downloading",
    "canceling",
    "暂停中",
    "waiting_selection",
    "processing",
    "parsing_collection",
})


RESTORE_BATCH_SIZE = 200
ACTIVE_DUPLICATE_STATUSES = frozenset({
    "queued",
    "downloading",
    "processing",
    "waiting_selection",
    "parsing_collection",
    "canceling",
    "暂停中",
    "paused",
})
WORKER_PROGRESS_EMIT_INTERVAL_SECONDS = 0.10
TRANSCODE_PROGRESS_EMIT_INTERVAL_SECONDS = 0.15


class _DownloadSetupError(RuntimeError):
    """A user-facing failure that occurs before yt-dlp starts downloading."""

    def __init__(
        self,
        message: str,
        *,
        category: str,
        log_message: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.log_message = log_message or message
        self.details = dict(details or {})


def cleanup_processing_workspace(workspace: Path | None) -> bool:
    return _cleanup_processing_workspace(
        workspace,
        reparse_check=_is_reparse_point,
    )


def ffmpeg_runtime_path(configured: str = "") -> str:
    """Return the exact FFmpeg executable selected by the shared resolver."""
    root = application_dir()
    return resolve_runtime_tool(
        "ffmpeg",
        configured,
        application_root=root,
        runtime_roots=tool_runtime_roots(root),
    ).executable


def ffprobe_runtime_path(configured_ffmpeg: str = "", configured_ffprobe: str = "") -> str:
    """Return the FFprobe paired with the active FFmpeg when possible.

    A user-selected FFmpeg normally ships beside ``ffprobe.exe``.  Prefer that
    exact sibling so post-download validation uses the same toolchain as the
    merge step, then fall back to the shared portable/bundled/PATH resolver.
    """
    root = application_dir()
    roots = tool_runtime_roots(root)
    return resolve_ffprobe_tool(
        configured_ffmpeg,
        configured_ffprobe,
        application_root=root,
        runtime_roots=roots,
    ).executable


def deno_runtime_path(configured: str = "") -> str:
    """Return yt-dlp's preferred Deno runtime used for EJS challenges."""
    root = application_dir()
    return resolve_runtime_tool(
        "deno",
        configured,
        application_root=root,
        runtime_roots=tool_runtime_roots(root),
    ).executable


def ytdlp_runtime_path() -> str:
    """Return the standalone yt-dlp selected by the shared runtime policy."""
    root = application_dir()
    return resolve_runtime_tool(
        "yt-dlp",
        application_root=root,
        runtime_roots=tool_runtime_roots(root),
    ).executable


def ytdlp_ejs_runtime_options(
    configured_deno: str = "",
    source: str = "auto",
) -> tuple[dict[str, Any], str, str]:
    """Return yt-dlp options, resolved Deno path and normalized EJS source."""
    normalized_source = normalize_ytdlp_ejs_source(source)
    local_component = activate_local_ejs()
    deno_path = deno_runtime_path(configured_deno)
    if not deno_path:
        return {}, "", normalized_source
    options: dict[str, Any] = {"js_runtimes": {"deno": {"path": deno_path}}}
    if normalized_source == "github":
        options["remote_components"] = {"ejs:github"}
    elif normalized_source == "npm" or (normalized_source == "auto" and local_component is None):
        options["remote_components"] = {"ejs:npm"}
    return options, deno_path, normalized_source


_COOKIE_OPTION_KEYS = frozenset({"cookiefile", "cookiesfrombrowser"})


def format_duration(seconds: float) -> str:
    """Format a short human-readable duration for task/stage diagnostics."""
    total = max(0, int(seconds or 0))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


@dataclass
class DownloadTask:
    id: str
    url: str
    output_dir: str
    task_kind: str = "video"
    parent_task_id: str = ""
    root_task_id: str = ""
    source_key: str = ""
    collection_index: int = 0
    options_json: dict[str, Any] = field(default_factory=dict)
    quality: str = "best"
    download_album: bool = False
    playlist_mode: str = "auto"
    proxy: str = ""
    cookie_file: str = ""
    cookie_source: str = "none"
    cookie_browser: str = "chrome"
    cookie_profile: str = ""
    cookie_keyring: str = ""
    cookie_container: str = ""
    filename_template: str = "%(title)s [%(id)s].%(ext)s"
    ffmpeg_path: str = ""
    format_selector: str = ""
    transcode_codec: str = "original"
    transcode_device: str = "auto"
    transcode_encoder: str = ""
    subtitle_language: str = "none"
    title: str = "等待获取视频信息"
    status: str = "queued"
    progress: float = 0.0
    speed: str = ""
    speed_bps: float = 0.0
    speed_samples: deque[float] = field(default_factory=lambda: deque(maxlen=6), repr=False)
    downloaded_bytes: int = 0
    total_bytes: int = 0
    eta: str = ""
    size: str = ""
    error: str = ""
    media_path: str = ""
    thumbnail_path: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    cancel_requested: bool = False
    pause_requested: bool = False
    # ``stage`` is intentionally kept in memory instead of being added to the
    # SQLite schema.  The database remains compatible with existing installs;
    # a restored task derives its initial stage from the persisted status.
    stage: str = "queued"
    stage_text: str = "排队中"
    stage_progress: float = 0.0
    retry_count: int = 0
    retry_total: int = 2
    reconnect_message: str = ""
    elapsed_seconds: float = 0.0
    stage_elapsed_seconds: float = 0.0
    video_progress: float = 0.0
    audio_progress: float = 0.0
    current_filename: str = ""
    # Actual video stream selected by yt-dlp. Runtime-only task-card state.
    selected_quality: str = ""
    # Runtime encoder selected by FFmpeg. Keep it separate from the user's
    # configured encoder so an automatic fallback does not become a strict
    # setting on retry.
    current_transcode_encoder: str = ""
    # A failed optional post-download conversion must not turn a verified
    # download into a failed task. Keep the warning separate from ``error`` so
    # status aggregation and retry logic still treat the task as completed.
    completion_warning: str = ""
    # Monotonic in-memory presentation values. yt-dlp can briefly emit
    # transitional callbacks with zero/unknown byte counters when switching
    # streams or entering post-processing. Keep them on the task so a UI
    # card rebuild cannot make details jump back to 0 B / unknown.
    # These fields are not persisted and do not alter the SQLite schema.
    visible_progress: float = 0.0
    visible_downloaded_bytes: int = 0
    visible_total_bytes: int = 0

    visible_size: str = ""
    visible_speed: str = ""
    visible_eta: str = ""
    # Completion metadata is kept in memory for the task card; the canonical
    # copy remains in ``media_items`` and no task-table columns are added.
    uploader: str = ""
    downloaded_at: str = ""

    def __post_init__(self) -> None:
        self.task_kind = "collection" if self.task_kind == "collection" else "video"
        self.parent_task_id = str(self.parent_task_id or "")
        self.root_task_id = str(self.root_task_id or "")
        self.source_key = normalize_source_key(self.source_key)
        self.collection_index = max(0, int(self.collection_index or 0))
        raw_options = dict(self.options_json or {})
        self.options_json = DownloadOptions.from_mapping(raw_options).to_dict()
        for internal_key in ("_collection", "_storage_preview", "_collection_materialization"):
            if isinstance(raw_options.get(internal_key), Mapping):
                self.options_json[internal_key] = dict(raw_options[internal_key])
        self.transcode_encoder = (
            normalize_transcode_encoder(self.transcode_encoder)
            if str(self.transcode_encoder or "").strip() else ""
        )
        if self.transcode_encoder:
            self.transcode_codec = transcode_encoder_codec(self.transcode_encoder)
            self.transcode_device = transcode_encoder_device(self.transcode_encoder)
        else:
            self.transcode_codec = normalize_transcode_codec(self.transcode_codec)
            self.transcode_device = normalize_transcode_device(self.transcode_device)


@dataclass(slots=True)
class _MediaIdentityIndex:
    """Normalized identities used to reject duplicate active media work."""

    source_keys: set[str] = field(default_factory=set)
    urls: set[str] = field(default_factory=set)
    titles: set[str] = field(default_factory=set)

    def add(self, source_key: object, url: object, title: object) -> None:
        normalized_source_key = normalize_source_key(source_key)
        normalized_url = normalize_source_url(url)
        normalized_title = normalize_media_title(title)
        if normalized_source_key:
            self.source_keys.add(normalized_source_key)
        if normalized_url:
            self.urls.add(normalized_url)
        if normalized_title:
            self.titles.add(normalized_title)

    def contains(self, source_key: object, url: object, title: object) -> bool:
        normalized_source_key = normalize_source_key(source_key)
        normalized_url = normalize_source_url(url)
        normalized_title = normalize_media_title(title)
        return bool(
            (normalized_source_key and normalized_source_key in self.source_keys)
            or (normalized_url and normalized_url in self.urls)
            or (normalized_title and normalized_title in self.titles)
        )


@dataclass(frozen=True, slots=True)
class _CollectionEntrySpec:
    """Validated, normalized input used to create one collection child."""

    url: str
    source_key: str
    title: str
    collection_index: int
    thumbnail_path: str


@dataclass(frozen=True, slots=True)
class _CollectionMaterializationPlan:
    """One atomic parent-summary and child-creation transition."""

    parent: DownloadTask
    persisted_parent: DownloadTask
    collection_metadata: dict[str, Any]
    children: tuple[DownloadTask, ...]

    @property
    def child_ids(self) -> list[str]:
        return [child.id for child in self.children]


@dataclass(frozen=True, slots=True)
class _NewDownloadTaskPlan:
    """A durable task creation followed by its in-memory publication."""

    task: DownloadTask
    queue_for_download: bool
    log_message: str


@dataclass(frozen=True, slots=True)
class _CompletedEntryPaths:
    """Resolved files and stream requests for one finished yt-dlp entry."""

    requested: tuple[Mapping[str, Any], ...]
    base: Path
    video: Path
    info_json: Path | None


@dataclass(frozen=True, slots=True)
class _AdaptiveAccessProbe:
    """One anonymous-or-Cookie metadata probe and its comparable profile."""

    mode: str
    result: dict[str, Any] | None
    profile: MediaCapabilityProfile
    error_name: str = ""


@dataclass(frozen=True, slots=True)
class _AdaptiveCookieDecision:
    """Pure selection result applied to the real yt-dlp options afterward."""

    use_anonymous: bool
    reason: str


@dataclass(frozen=True, slots=True)
class _YtdlpStageEvent:
    """One user-visible pipeline transition inferred from yt-dlp text."""

    stage: str
    text: str
    progress: float | None = None
    starts_media_transfer: bool = False


@dataclass(frozen=True, slots=True)
class _CapacityReservationContext:
    """Normalized paths and estimate for one yt-dlp media entry."""

    estimate: CapacityEstimate
    group_id: str
    temporary_target: Path
    final_target: Path
    cross_volume: bool
    storage_preview: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _CapacityReservationResult:
    """Newly acquired capacity for one entry; existing keys stay idempotent."""

    primary: DiskReservation
    temporary_created: bool
    final_created: bool

    @property
    def created(self) -> bool:
        return self.temporary_created or self.final_created


@dataclass(frozen=True, slots=True)
class _OptionalTranscodePlan:
    """Presentation and fallback state for one optional final conversion."""

    video_path: str
    verification_path: Path
    thumbnail: Path | None
    prepend_cover: bool
    required: bool
    target_label: str = ""
    device_label: str = ""
    action_text: str = ""


def validate_filename_template(value: str) -> str:
    """Return a safe relative yt-dlp template or raise a user-facing error."""

    template = str(value or "").strip()
    if not template:
        raise ValueError("文件名模板不能为空")
    drive, _tail = ntpath.splitdrive(template)
    normalized = template.replace("\\", "/")
    if drive or normalized.startswith("/") or normalized.startswith("//"):
        raise ValueError("文件名模板必须是下载目录内的相对路径，不能使用盘符、UNC 或绝对路径")
    if any(part == ".." for part in normalized.split("/")):
        raise ValueError("文件名模板不能包含 ..，以免写出下载目录")
    if any(part in {"", "."} for part in normalized.split("/")[:-1]):
        raise ValueError("文件名模板包含无效目录段")
    return template


def _resolved_output_root(output_dir: str | Path) -> Path:
    try:
        return Path(output_dir).expanduser().resolve()
    except OSError:
        return Path(output_dir).expanduser().absolute()


def _bounded_output_path(root: Path, path_value: str | Path) -> Path | None:
    """Normalize an output path while preserving a safe final symlink."""

    if not str(path_value or "").strip():
        return None
    candidate = Path(path_value).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        parent = candidate.parent.resolve()
        parent.relative_to(root)
    except (OSError, ValueError):
        return None
    local_path = parent / candidate.name
    if local_path.is_symlink():
        return local_path
    try:
        resolved = local_path.resolve()
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    return resolved


def _is_ytdlp_artifact_remainder(remainder: str) -> bool:
    """Recognize bounded yt-dlp sidecars and split-stream filenames."""

    normalized = str(remainder or "").casefold()
    terminal_suffix = ""
    for candidate in (".part", ".ytdl"):
        if normalized.endswith(candidate):
            normalized = normalized[:-len(candidate)]
            terminal_suffix = candidate
            break

    media_suffixes = {
        "mp4", "mkv", "webm", "mov", "avi", "flv", "m4v", "ts",
        "mp3", "m4a", "aac", "opus", "ogg", "wav", "flac",
        "webp", "jpg", "jpeg", "png", "gif", "avif",
        "vtt", "srt", "ass", "lrc", "ttml",
    }
    if normalized in media_suffixes:
        return True
    if normalized in {"info.json", "description"}:
        return not terminal_suffix
    if re.fullmatch(
        r"temp\.(?:" + "|".join(sorted(media_suffixes)) + r")",
        normalized,
    ):
        return True
    if re.fullmatch(
        r"f(?:\d+|[a-z][a-z0-9_-]*-\d+)\.(?:"
        + "|".join(sorted(media_suffixes))
        + r")",
        normalized,
    ):
        return True
    if not terminal_suffix and re.fullmatch(
        r"[a-z0-9][a-z0-9_-]{0,79}\.(?:vtt|srt|ass|lrc|ttml)",
        normalized,
    ):
        return True
    return False


def task_download_artifact_paths(task: DownloadTask) -> set[Path]:
    """Return app-owned files belonging to one task's yt-dlp output set.

    A paused DASH download may have no final ``media_path`` yet. yt-dlp still
    leaves files such as ``.f702.mp4.part``, completed sibling streams,
    thumbnails and ``.info.json`` beside the intended output. Use the latest
    progress filename to identify that exact output family without scanning or
    deleting unrelated files elsewhere on the computer.
    """
    resolved_root = _resolved_output_root(task.output_dir)

    artifacts: set[Path] = set()
    families: dict[Path, set[str]] = defaultdict(set)
    for raw_path in (task.current_filename, task.media_path):
        path = _bounded_output_path(resolved_root, raw_path)
        if path is None:
            continue
        artifacts.add(path)
        artifacts.add(path.with_name(path.name + ".part"))
        artifacts.add(path.with_name(path.name + ".ytdl"))
        normalized_name = path.name.removesuffix(".part")
        format_match = re.match(r"^(?P<base>.+)\.f[^.]+\.[^.]+$", normalized_name)
        if format_match:
            families[path.parent].add(format_match.group("base"))
        else:
            families[path.parent].add(Path(normalized_name).stem)

    if not families:
        return artifacts

    for parent, base_names in families.items():
        try:
            children = list(parent.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_file() and not child.is_symlink():
                continue
            for base_name in base_names:
                prefix = base_name + "."
                if not child.name.casefold().startswith(prefix.casefold()):
                    continue
                remainder = child.name[len(prefix):]
                if _is_ytdlp_artifact_remainder(remainder):
                    bounded = _bounded_output_path(resolved_root, child)
                    if bounded is not None:
                        artifacts.add(bounded)
                break
    return artifacts


def _managed_output_file(root: Path, path_value: str | Path) -> Path | None:
    """Return a deletable local path without following its final symlink."""

    local_path = _bounded_output_path(root, path_value)
    if local_path is None:
        return None
    return local_path if local_path.is_symlink() or local_path.is_file() else None


def _completed_sidecar_kind(
    media_path: Path,
    sibling: Path,
    download_options: DownloadOptions,
    subtitle_language: str,
) -> str:
    stem = media_path.stem
    name = sibling.name
    folded_name = name.casefold()
    if download_options.write_info_json and folded_name == f"{stem}.info.json".casefold():
        return "info_json"
    if download_options.write_description and folded_name == f"{stem}.description".casefold():
        return "description"

    suffix = sibling.suffix.casefold()
    if download_options.write_thumbnail and suffix in {
        ".jpg", ".jpeg", ".webp", ".png", ".gif", ".avif",
    }:
        return "thumbnail" if sibling.stem.casefold() == stem.casefold() else ""

    requested_language = normalize_subtitle_language(subtitle_language)
    if requested_language == "none" or suffix not in {
        ".vtt", ".srt", ".ass", ".lrc", ".ttml",
    }:
        return ""
    prefix = stem + "."
    if not name.casefold().startswith(prefix.casefold()):
        return ""
    language = name[len(prefix):-len(suffix)]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", language):
        return ""
    if requested_language != "all" and re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}",
        requested_language,
    ):
        if language.casefold() != requested_language.casefold():
            return ""
    return "subtitle"


def completed_task_file_manifest(
    output_dir: str,
    media_items: list[MediaItem],
    download_options: DownloadOptions,
    *,
    subtitle_language: str = "none",
) -> list[tuple[str, str, bool]]:
    """Capture only files this completed task was configured to create."""

    root = _resolved_output_root(output_dir)
    discovered: dict[Path, str] = {}

    def add(path_value: str | Path, kind: str) -> Path | None:
        local_path = _managed_output_file(root, path_value)
        if local_path is not None:
            # Explicit media catalog fields are added first and keep their
            # stronger ownership kind if a later sidecar scan sees the same path.
            discovered.setdefault(local_path, kind)
        return local_path

    for item in media_items:
        media_path = add(item.video_path, "media")
        add(item.thumbnail_path, "thumbnail")
        add(item.metadata_json_path, "info_json")
        if media_path is None:
            continue
        parent = media_path.parent
        try:
            siblings = list(parent.iterdir())
        except OSError:
            siblings = []
        for sibling in siblings:
            kind = _completed_sidecar_kind(
                media_path,
                sibling,
                download_options,
                subtitle_language,
            )
            if kind:
                add(sibling, kind)
    return [(str(path), kind, True) for path, kind in sorted(discovered.items(), key=lambda item: str(item[0]))]


class DownloadWorker(QObject):
    # Include the task id in every cross-thread payload. Connecting a worker
    # signal through a Python lambda gives that lambda no QObject thread
    # affinity, so PySide may execute it inside the worker thread.  The
    # DownloadService slots below belong to the GUI thread and therefore give
    # Qt a real queued-connection boundary for timers, persistence and UI
    # signals.
    progress = Signal(str, object)
    completed = Signal(str, object)
    failed = Signal(str, str)
    finished = Signal()
    formats_ready = Signal(str, object)
    playlist_info = Signal(str, object)

    def __init__(self, task_id: str, url: str, output_dir: str, db: Database, proxy: str = "", cookie_file: str = "",
                 quality: str = "best", filename_template: str = "%(title)s [%(id)s].%(ext)s",
                 ffmpeg_path: str = "", format_selector: str = "", download_album: bool = False,
                 playlist_mode: str = "auto", request_delay: float = 0.0,
                 fragment_concurrent: int = 12,
                 cookie_source: str = "none", cookie_browser: str = "chrome",
                 cookie_profile: str = "", cookie_keyring: str = "", cookie_container: str = "",
                 disk_lease: DiskReservationLease | None = None,
                 ytdlp_core_mode: str = "auto", deno_path: str = "", ffprobe_path: str = "",
                 ytdlp_ejs_source: str = "auto", transcode_codec: str = "original",
                 transcode_device: str = "auto", subtitle_language: str = "none",
                 transcode_encoder: str = "", cover_convert_jpeg: bool = False,
                 cover_jpeg_quality: int = 90, options_json: Mapping[str, Any] | None = None,
                 log_service: DownloadLogService | None = None):
        super().__init__()
        self.task_id = task_id
        self.url = url
        self.output_dir = str(resolve_portable_path(output_dir))
        self.db = db
        self.proxy = proxy
        self.cookie_file = (
            str(resolve_portable_path(cookie_file)) if str(cookie_file or "").strip() else ""
        )
        self.cookie_source = str(cookie_source or "none").strip().casefold()
        if self.cookie_source == "none" and str(cookie_file or "").strip():
            self.cookie_source = "file"
        self.cookie_browser = str(cookie_browser or "chrome").strip().casefold()
        self.cookie_profile = str(cookie_profile or "").strip()
        self.cookie_keyring = str(cookie_keyring or "").strip()
        self.cookie_container = str(cookie_container or "").strip()
        self.logs = log_service or DownloadLogService()
        self.quality, self.filename_template, self.ffmpeg_path = quality, filename_template, ffmpeg_path
        self.deno_path = str(deno_path or "").strip()
        self.ffprobe_path = str(ffprobe_path or "").strip()
        self.ytdlp_ejs_source = normalize_ytdlp_ejs_source(ytdlp_ejs_source)
        self.transcode_codec = normalize_transcode_codec(transcode_codec)
        self.transcode_device = normalize_transcode_device(transcode_device)
        self.transcode_encoder = (
            normalize_transcode_encoder(transcode_encoder)
            if str(transcode_encoder or "").strip() else ""
        )
        if self.transcode_encoder:
            self.transcode_codec = transcode_encoder_codec(self.transcode_encoder)
            self.transcode_device = transcode_encoder_device(self.transcode_encoder)
        self.subtitle_language = normalize_subtitle_language(subtitle_language)
        self.cover_convert_jpeg = bool(cover_convert_jpeg)
        self.cover_jpeg_quality = max(50, min(int(cover_jpeg_quality or 90), 100))
        self.download_options = DownloadOptions.from_mapping(options_json)
        self.processing_temp_dir = (
            str(resolve_portable_path(self.download_options.processing_temp_dir))
            if self.download_options.processing_temp_dir else ""
        )
        self._processing_workspace: Path | None = None
        self._download_completed = False
        _, self.fragment_concurrent, self.request_delay = (
            normalize_download_performance_values(
                1,
                fragment_concurrent,
                request_delay,
            )
        )
        self.download_album = download_album
        self.playlist_mode = playlist_mode if playlist_mode in {"auto", "single", "playlist"} else ("playlist" if download_album else "single")
        self.format_selector = format_selector
        self._cancel = threading.Event()
        self._thumbnails = DownloadThumbnailManager(
            self.output_dir,
            proxy=self.proxy,
            convert_jpeg=self.cover_convert_jpeg,
            jpeg_quality=self.cover_jpeg_quality,
            cancel_event=self._cancel,
            log=self._log,
        )
        self._format_event = threading.Event()
        self._last_progress_log = -1
        self._stage = "queued"
        self._stage_text = "排队中"
        self._stage_progress = 0.0
        self._retry_count = 0
        self._retry_total = 2
        self._current_info: dict[str, Any] = {}
        self._media_download_started = False
        # All entries in one DownloadWorker use the same proxy/network route.
        # Cache both successful and failed lookups so a large playlist does
        # not contact api.ipify.org once per video (each failed lookup can
        # otherwise add up to the network timeout).
        self._source_ip_checked = False
        self._source_ip = ""
        self._started_at = 0.0
        self._stage_started_at = 0.0
        self._cancel_reason = "cancel"
        self._disk_lease = disk_lease
        self._last_disk_watchdog_at = 0.0
        self._last_disk_watchdog_bytes = 0
        self.ytdlp_core_mode = normalize_ytdlp_core_mode(ytdlp_core_mode)
        self._pending_transcodes: list[PreparedTranscode] = []
        self._last_worker_progress_emit_at = 0.0
        self._last_transcode_progress_emit_at = 0.0

    def _log(self, level: str, category: str, message: str, **details: Any) -> None:
        self.logs.write(self.task_id, level, category, message, **details)

    def _emit_worker_progress(self, payload: dict[str, Any], *, force: bool = False) -> bool:
        """Bound queued worker traffic before it reaches the GUI event loop."""
        now = time.monotonic()
        if (
            not force
            and self._last_worker_progress_emit_at
            and now - self._last_worker_progress_emit_at < WORKER_PROGRESS_EMIT_INTERVAL_SECONDS
        ):
            return False
        self._last_worker_progress_emit_at = now
        self.progress.emit(self.task_id, payload)
        return True

    def _set_stage(self, stage: str, text: str, progress: float | None = None,
                   force: bool = False, publish: bool = True, **details: Any) -> None:
        """Publish a user-visible stage without changing the DB schema."""
        now = time.monotonic()
        if not self._started_at:
            self._started_at = now
        stage_code_changed = stage != self._stage
        if stage_code_changed or not self._stage_started_at:
            self._stage_started_at = now
        # ``stage_progress`` belongs to the current stage. Carrying a previous
        # stage's 99% into parsing, disk checks or a new post-processor makes
        # the UI look frozen at an unrelated value. Preserve it only for a
        # textual refresh within the same stage.
        next_progress = (
            0.0 if stage_code_changed else self._stage_progress
        ) if progress is None else bounded_percent(progress)
        stage_changed = stage_code_changed or text != self._stage_text
        changed = stage_changed or next_progress != self._stage_progress or details
        self._stage, self._stage_text, self._stage_progress = stage, text, next_progress
        if not changed and not force:
            return
        if stage_code_changed:
            self._log("info", "阶段", text, stage=stage, retry_count=self._retry_count)
        payload = {
            "stage": stage,
            "stage_text": text,
            "stage_progress": next_progress,
            "retry_count": self._retry_count,
            "retry_total": self._retry_total,
            "elapsed_seconds": max(0.0, now - self._started_at),
            "stage_elapsed_seconds": max(0.0, now - self._stage_started_at),
        }
        payload.update(details)
        if publish:
            # Stage transitions and explicitly forced lifecycle messages are
            # sparse and must remain immediate. High-frequency transfer and
            # FFmpeg progress is throttled at its producer below.
            self._emit_worker_progress(payload, force=force or stage_code_changed)

    def _download_stage(self, info: dict[str, Any] | None = None) -> tuple[str, str]:
        info = info or self._current_info or {}
        vcodec = str(info.get("vcodec") or "")
        acodec = str(info.get("acodec") or "")
        if vcodec == "none" and acodec and acodec != "none":
            return "downloading_audio", "正在下载音频"
        if acodec == "none" and vcodec and vcodec != "none":
            return "downloading_video", "正在下载视频"
        return "downloading", "正在下载视频和音频"

    def _ytdlp_stage_event(self, message: str) -> _YtdlpStageEvent | None:
        lower = message.casefold()
        if (
            "merging formats into" in lower
            or ("merging" in lower and "format" in lower)
        ):
            return _YtdlpStageEvent("merging", "正在合并视频和音频", 99)
        if self._media_download_started and any(
            token in lower
            for token in (
                "writing video thumbnail",
                "downloading thumbnail",
                "fixing thumbnail",
            )
        ):
            return _YtdlpStageEvent("thumbnail", "正在下载封面", 99)
        if self._media_download_started and any(
            token in lower
            for token in (
                "writing video metadata",
                "writing playlist metadata",
                "writing metadata",
                "adding metadata",
            )
        ):
            return _YtdlpStageEvent("metadata", "正在写入元数据", 99)
        if self._media_download_started and any(
            token in lower
            for token in ("deleting original file", "fixing malformed")
        ):
            return _YtdlpStageEvent("verifying", "正在校验媒体文件", 99)
        if any(
            token in lower
            for token in (
                "extracting url",
                "downloading webpage",
                "player api",
                "m3u8 information",
                "downloading android",
                "downloading ios",
            )
        ):
            return _YtdlpStageEvent("parsing", "正在解析视频信息")
        if (
            "downloading 1 format" in lower
            or ("downloading " in lower and "format(s)" in lower)
        ):
            return _YtdlpStageEvent("formats", "正在获取可用格式")
        transfer_line = (
            "destination:" in lower
            or bool(re.search(r"^\[download\]\s+\d+(?:\.\d+)?%", lower))
            or lower.startswith("[download] resuming download")
            or "has already been downloaded" in lower
        )
        if transfer_line:
            stage, text = self._download_stage()
            return _YtdlpStageEvent(
                stage,
                text,
                starts_media_transfer=True,
            )
        return None

    def _apply_ytdlp_stage_event(self, event: _YtdlpStageEvent | None) -> None:
        if event is None:
            return
        if event.starts_media_transfer:
            self._media_download_started = True
        self._set_stage(event.stage, event.text, event.progress)

    def _handle_ytdlp_log(self, level: str, category: str, message: str) -> None:
        normalized = normalize_ytdlp_log_message(message)
        if not normalized:
            return
        effective_level = ytdlp_log_level(normalized, level)
        effective_category = str(category or "yt-dlp")
        if effective_level in {"warning", "error"} and effective_category in {
            "",
            "yt-dlp",
            "未知",
        }:
            effective_category = DownloadLogService.classify_error(normalized)
        self._log(effective_level, effective_category, normalized)
        self._apply_ytdlp_stage_event(self._ytdlp_stage_event(normalized))

    def _handle_postprocessor(self, data: dict[str, Any]) -> None:
        """Translate yt-dlp post-processing callbacks into stable UI stages.

        Logger wording varies between yt-dlp versions and extractors.  The
        postprocessor hook is the authoritative signal for merge, thumbnail,
        metadata and fixup work, so the task card remains accurate even when
        quiet logging hides FFmpeg messages.
        """
        key = str(data.get("postprocessor") or data.get("postprocessor_key") or "").lower()
        status = str(data.get("status") or "").lower()
        if "merger" in key or "merge" in key:
            stage, text = "merging", "正在合并视频和音频"
        elif "thumbnail" in key or "embedthumbnail" in key:
            stage, text = "thumbnail", "正在处理封面"
        elif "metadata" in key or "metadatacreator" in key:
            stage, text = "metadata", "正在写入元数据"
        elif any(token in key for token in ("fixup", "movefiles", "ffmpeg", "extractaudio")):
            stage, text = "verifying", "正在校验和整理媒体文件"
        else:
            return
        if not self._media_download_started:
            return
        if status == "started":
            # Progress callbacks stop while FFmpeg is merging or moving the
            # completed streams. Recheck the physical low-watermark here so a
            # full target disk cannot turn a valid download into a corrupt
            # merge output.
            self._check_disk_low_watermark(force=True)
        progress = 100 if status == "finished" else 99
        self._set_stage(stage, text, progress)
        if status == "started":
            self._log("info", "后处理", text, postprocessor=key)

    def _run_with_network_retry(self, action, stage: str):
        """Retry transient network failures without retrying rate limits or login challenges."""
        resume_stage = self._stage if self._stage not in {"queued", "reconnecting"} else "parsing"
        for attempt in range(3):
            try:
                result = action()
                if self._stage == "reconnecting":
                    self._set_stage(resume_stage, f"网络已恢复，继续{stage}", force=True)
                return result
            except Exception as exc:
                category = DownloadLogService.classify_error(str(exc))
                if category != "网络/代理" or attempt >= 2 or self._cancel.is_set():
                    raise
                # A whole-extraction retry may revisit yt-dlp's match_filter.
                # Release only this worker's incomplete entry before retrying
                # so an unknown-size exclusive reservation cannot wait on
                # itself and known-size tasks cannot be counted twice.
                self._release_capacity_before_network_retry()
                delay = 2 ** attempt
                self._retry_count = attempt + 1
                self._log("warning", "网络/代理", f"{stage}失败，将在 {delay} 秒后重试", attempt=self._retry_count, error=str(exc))
                for remaining in range(delay, 0, -1):
                    self._set_stage(
                        "reconnecting",
                        f"网络中断，正在重连（第 {self._retry_count}/{self._retry_total} 次，{remaining} 秒后重试）",
                        force=True,
                        reconnect_delay=remaining,
                    )
                    if self._cancel.wait(1):
                        if yt_dlp is not None:
                            raise yt_dlp.utils.DownloadError("用户取消下载")
                        raise InterruptedError("用户取消下载")

    def _release_capacity_before_network_retry(self) -> None:
        """Best-effort cleanup that never replaces the network failure."""

        lease = self._disk_lease
        if lease is None:
            return
        try:
            released = lease.release_all()
        except Exception as exc:
            self._cleanup_warning(
                "磁盘/存储",
                "网络重试前未能释放全部磁盘预留，将复用现有预留并在线程结束时重试",
                error=str(exc),
                remaining=lease.active_count,
            )
            return
        if not released:
            return
        try:
            self._log(
                "warning",
                "磁盘/存储",
                "网络重试前已释放未完成媒体的磁盘预留",
                released=released,
            )
        except Exception:
            pass

    def cancel(self, reason: str = "cancel") -> None:
        """Stop the worker while preserving whether this was pause/cancel/delete."""
        self._cancel_reason = reason if reason in {"pause", "cancel", "delete", "discard", "shutdown"} else "cancel"
        self._cancel.set()
        self._format_event.set()

    def set_format_selector(
        self,
        selector: str,
        *,
        content_mode: str = "",
        audio_format: str = "",
    ) -> None:
        self.format_selector = selector
        if content_mode or audio_format:
            selected = self.download_options.to_dict()
            if content_mode:
                selected["content_mode"] = content_mode
            if audio_format:
                selected["audio_format"] = audio_format
            self.download_options = DownloadOptions.from_mapping(selected)
        self._format_event.set()

    def _manual_selection_required(self) -> bool:
        """Return whether this single-video task needs a user confirmation."""

        return not self.format_selector and (
            self.quality == "custom"
            or self.download_options.content_mode == "manual"
        )

    def _apply_manual_format_selection(self, ydl_opts: dict[str, Any]) -> None:
        """Apply a format-picker choice after the worker has already parsed metadata."""

        ydl_opts["format"] = self.format_selector
        processors = [
            processor
            for processor in (ydl_opts.get("postprocessors") or ())
            if str(processor.get("key") or "")
            not in {"FFmpegExtractAudio", "FFmpegVideoRemuxer"}
        ]
        refreshed = self.download_options.ytdlp_options()
        ydl_opts.pop("merge_output_format", None)
        ydl_opts.pop("format_sort", None)
        custom_final_processing = (
            self.transcode_codec != "original"
            or self.download_options.prepend_cover_enabled
        )
        if self.download_options.content_mode == "video":
            ydl_opts["format_sort"] = self.download_options.format_sort(self.quality)
            if not custom_final_processing and refreshed.get("merge_output_format"):
                ydl_opts["merge_output_format"] = refreshed["merge_output_format"]
        for processor in refreshed.get("postprocessors") or ():
            key = str(processor.get("key") or "")
            if key == "FFmpegExtractAudio" or (
                key == "FFmpegVideoRemuxer" and not custom_final_processing
            ):
                processors.append(processor)
        if processors:
            ydl_opts["postprocessors"] = processors
        else:
            ydl_opts.pop("postprocessors", None)

    @staticmethod
    def _disk_entry_key(info: Mapping[str, Any], target_path: str | Path) -> str:
        """Build a retry-stable key without retaining a source URL."""

        url_digest = hashlib.sha256(
            str(info.get("webpage_url") or info.get("original_url") or "").encode(
                "utf-8", errors="replace"
            )
        ).hexdigest()[:16]
        fields = (
            info.get("extractor_key") or info.get("extractor") or "",
            info.get("id") or "",
            info.get("playlist_id") or "",
            info.get("playlist_index") or "",
            info.get("format_id") or "",
            info.get("section_start") or "",
            info.get("section_end") or "",
            url_digest,
            str(Path(target_path)),
        )
        return "\x1f".join(str(value) for value in fields)

    def _capacity_reservation_context(
        self,
        ydl: Any,
        info: Mapping[str, Any],
    ) -> _CapacityReservationContext:
        estimate = estimate_download_capacity(info)
        prepared = ydl.prepare_filename(dict(info))
        final_target = Path(prepared).parent if prepared else Path(self.output_dir)
        temporary_target = self._processing_workspace or final_target
        cross_volume = not same_storage_volume(temporary_target, final_target)
        return _CapacityReservationContext(
            estimate=estimate,
            group_id=self._disk_entry_key(info, final_target),
            temporary_target=temporary_target,
            final_target=final_target,
            cross_volume=cross_volume,
            storage_preview={
                "known": bool(estimate.known),
                "temporary_bytes": max(0, int(estimate.peak_bytes or 0)),
                "final_bytes": max(0, int(estimate.final_bytes or 0)),
                "entry_count": max(0, int(estimate.entry_count or 0)),
                "merge_entry_count": max(0, int(estimate.merge_entry_count or 0)),
                "temporary_dir": str(self.processing_temp_dir or temporary_target),
                "final_dir": str(final_target),
                "cross_volume": cross_volume,
            },
        )

    def _report_capacity_waiting(
        self,
        snapshot: DiskCapacitySnapshot,
        required_bytes: int,
    ) -> None:
        self._log(
            "info",
            "磁盘/存储",
            "同一磁盘上的其他下载任务正在占用预留空间，当前任务将等待后继续",
            reserved_by_other_tasks=snapshot.reserved_bytes,
            required_bytes=required_bytes,
            unknown_reservation_active=snapshot.unknown_reservation_active,
        )
        self._set_stage(
            "waiting_disk",
            "等待其他下载任务释放磁盘空间",
            force=True,
            reserved_by_other_tasks=snapshot.reserved_bytes,
            required_bytes=required_bytes,
        )

    def _reserve_entry_capacity(
        self,
        context: _CapacityReservationContext,
    ) -> _CapacityReservationResult:
        lease = self._disk_lease
        if lease is None:
            raise RuntimeError("磁盘容量预留服务不可用")
        temporary_key = f"{context.group_id}\x1ftemp"
        reservation, temporary_created = lease.acquire(
            temporary_key,
            context.temporary_target,
            context.estimate,
            cancel_event=self._cancel,
            on_wait=self._report_capacity_waiting,
        )
        reservation_keys = [temporary_key]
        final_created = False
        try:
            if context.cross_volume:
                final_key = f"{context.group_id}\x1ffinal"
                _final_reservation, final_created = lease.acquire(
                    final_key,
                    context.final_target,
                    final_output_capacity_estimate(context.estimate),
                    cancel_event=self._cancel,
                    on_wait=self._report_capacity_waiting,
                )
                reservation_keys.append(final_key)
        except BaseException:
            try:
                lease.release_keys(reservation_keys)
            except Exception as cleanup_error:
                self._log(
                    "warning",
                    "磁盘/存储",
                    "跨盘预留失败后清理临时预留失败，将在线程结束时重试",
                    error=str(cleanup_error),
                )
            raise
        lease.queue_release_group(context.group_id, reservation_keys)
        return _CapacityReservationResult(
            primary=reservation,
            temporary_created=temporary_created,
            final_created=final_created,
        )

    def _publish_capacity_reserved(
        self,
        context: _CapacityReservationContext,
        result: _CapacityReservationResult,
    ) -> None:
        if not result.created:
            return
        estimate = context.estimate
        if estimate.known:
            self._log(
                "info",
                "磁盘/存储",
                "已为当前媒体预留磁盘空间",
                reserved_bytes=result.primary.reserved_bytes,
                entry_count=estimate.entry_count,
                merge_entry_count=estimate.merge_entry_count,
                estimate_sources=estimate.sources,
            )
            self._set_stage("waiting_disk", "磁盘空间已预留，正在准备下载", force=True)
            return
        self._log(
            "warning",
            "磁盘/存储",
            "文件大小暂时无法准确预估，已独占目标磁盘并持续监控空间",
        )
        self._set_stage("waiting_disk", "文件大小未知，已独占目标磁盘并持续监控", force=True)

    def _capacity_match_filter(self, ydl, info: Mapping[str, Any], *, incomplete: bool = False):
        """Reserve the selected format's peak space immediately before I/O."""

        if incomplete or self._disk_lease is None:
            return None
        try:
            context = self._capacity_reservation_context(ydl, info)
            self._set_stage(
                "waiting_disk",
                "正在检查并预留磁盘空间",
                force=True,
                storage_preview=context.storage_preview,
            )
            result = self._reserve_entry_capacity(context)
        except DiskCapacityError:
            raise
        except TypeError:
            # yt-dlp treats TypeError as an old single-argument match-filter
            # signature and calls the callback again. Convert it so a genuine
            # capacity bug cannot silently bypass the safety gate.
            raise RuntimeError("磁盘容量检查失败：下载信息格式异常，请重新解析后重试。") from None
        self._publish_capacity_reserved(context, result)
        return None

    def _capacity_post_hook(self, _filename: str) -> None:
        """Release one entry only after all yt-dlp post-processing completed."""

        if self._disk_lease is None:
            return
        try:
            released = self._disk_lease.release_next_group()
            if released:
                self._log("info", "磁盘/存储", "当前媒体处理完成，已释放磁盘容量预留")
        except Exception as exc:
            # A post hook failure is treated as a download failure by yt-dlp.
            # Capacity cleanup is best-effort here and is repeated in finally.
            self._log("warning", "磁盘/存储", "释放磁盘容量预留时发生异常，将在线程结束时重试", error=str(exc))

    def _check_disk_low_watermark(
        self,
        *,
        force: bool = False,
        downloaded_bytes: int = 0,
    ) -> None:
        """Throttle physical free-space probes during download and merge."""

        if self._disk_lease is None:
            return
        now = time.monotonic()
        byte_delta = max(0, int(downloaded_bytes or 0) - self._last_disk_watchdog_bytes)
        if not force and (
            now - self._last_disk_watchdog_at < DISK_WATCHDOG_INTERVAL_SECONDS
            and byte_delta < DISK_WATCHDOG_BYTE_STEP
        ):
            return
        for target in self._disk_lease.current_target_paths(self.output_dir):
            self._disk_lease.manager.check_low_watermark(target)
        self._last_disk_watchdog_at = now
        self._last_disk_watchdog_bytes = max(self._last_disk_watchdog_bytes, int(downloaded_bytes or 0))

    def _detect_source_ip_once(self) -> str:
        """Detect and cache the worker's download egress IP, including failure."""
        if not self._source_ip_checked:
            if self._cancel.is_set():
                raise InterruptedError("用户取消出口 IP 检测")
            self._source_ip = detect_public_ip(self.proxy)
            self._source_ip_checked = True
            self._log(
                "info" if self._source_ip else "warning",
                "网络/代理",
                "已检测下载出口 IP" if self._source_ip else "未能检测下载出口 IP",
                source_ip=self._source_ip,
            )
        return self._source_ip

    def _source_ip_for_media(self) -> str:
        """Record an egress IP only when the user explicitly configured a proxy.

        The previous default contacted a third-party IP service after every
        otherwise-complete task.  A slow or blocked service could hold a
        download worker slot for another eight seconds and delayed the next
        queued task even when no proxy diagnostics were needed.
        """
        return self._detect_source_ip_once() if self.proxy else ""

    def _download_output_template(self, template: str) -> tuple[Path, int]:
        """Build a Windows-safe yt-dlp output template for this task snapshot."""
        template = validate_filename_template(template)
        output_root = Path(self.output_dir)
        try:
            resolved_root = output_root.resolve()
        except OSError:
            resolved_root = output_root.absolute()
        if not self.download_options.organize_task_folder:
            relative_result = Path(template)
            result = relative_result if self._processing_workspace is not None else output_root / relative_result
            try:
                (output_root / relative_result).parent.resolve(strict=False).relative_to(resolved_root)
            except (OSError, ValueError):
                raise ValueError("文件名模板解析后超出下载目录") from None
            return result, 0

        # Stay below the traditional Windows MAX_PATH boundary even when long
        # path support is disabled. yt-dlp applies the byte precision and
        # filename trim after resolving the real title and identifier.
        available = max(48, 235 - len(str(output_root.resolve())))
        folder_budget = max(24, min(96, available // 3))
        filename_budget = max(32, min(140, available - folder_budget - 18))
        if available < 80:
            folder_template = f"%(id)s - {self.task_id}"
        else:
            folder_template = f"%(title).{folder_budget}B [%(id)s] - {self.task_id}"
        relative_result = Path(folder_template) / template
        result = relative_result if self._processing_workspace is not None else output_root / relative_result
        try:
            (output_root / relative_result).parent.resolve(strict=False).relative_to(resolved_root)
        except (OSError, ValueError):
            raise ValueError("文件名模板解析后超出下载目录") from None
        return result, filename_budget

    def _external_log_line(self, message: str) -> None:
        normalized = normalize_ytdlp_log_message(message)
        if not normalized:
            return
        level = ytdlp_log_level(normalized)
        category = (
            DownloadLogService.classify_error(normalized)
            if level in {"warning", "error"}
            else "yt-dlp"
        )
        self._handle_ytdlp_log(level, category, normalized)

    @staticmethod
    def _external_progress_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
        progress = dict(payload.get("progress") or {})
        info = payload.get("info")
        if isinstance(info, Mapping):
            progress["info_dict"] = dict(info)
        return progress

    def _external_prepare_filename(self, info: Mapping[str, Any]) -> str:
        requested = [item for item in (info.get("requested_downloads") or []) if isinstance(item, Mapping)]
        filepath = info.get("filepath") or info.get("_filename")
        if not filepath and requested:
            filepath = requested[0].get("filepath") or requested[0].get("filename")
        return str(filepath or (Path(self.output_dir) / "external-download"))

    @staticmethod
    def _download_result_entries(info: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        raw_entries = info.get("entries") if is_ytdlp_collection_result(info) else [info]
        try:
            entries = iter(raw_entries or ())
        except TypeError:
            return []
        return [entry for entry in entries if isinstance(entry, Mapping)]

    def _completed_download_task(self, media_items: list[MediaItem]) -> DownloadTask:
        if not media_items:
            raise ValueError("完成下载任务时至少需要一个媒体条目")
        last_item = media_items[-1]
        return DownloadTask(
            id=self.task_id,
            url=self.url,
            output_dir=self.output_dir,
            status="completed",
            progress=100.0,
            media_path=last_item.video_path,
            thumbnail_path=last_item.thumbnail_path,
        )

    @staticmethod
    def _publish_prepared_transcodes(
        prepared_transcodes: list[PreparedTranscode],
        published_transcodes: list[PublishedTranscode],
    ) -> None:
        for prepared in prepared_transcodes:
            published_transcodes.append(prepared.commit())

    def _rollback_completed_media_publication(
        self,
        published_transcodes: list[PublishedTranscode],
        uncommitted_transcodes: list[PreparedTranscode],
    ) -> None:
        for published in reversed(published_transcodes):
            try:
                published.rollback()
            except BaseException as cleanup_error:
                self._log(
                    "error",
                    "文件/回滚",
                    "完成事务失败后恢复原始媒体文件失败",
                    output_path=str(published.final_path),
                    error=str(cleanup_error),
                    error_type=type(cleanup_error).__name__,
                )
        for prepared in uncommitted_transcodes:
            try:
                prepared.discard()
            except BaseException as cleanup_error:
                self._log(
                    "warning",
                    "文件/清理",
                    "完成事务失败后清理未提交转码文件失败",
                    temporary_path=str(prepared.temporary_path),
                    error=str(cleanup_error),
                    error_type=type(cleanup_error).__name__,
                )

    def _finalize_committed_transcodes(
        self,
        published_transcodes: list[PublishedTranscode],
        completion_warnings: list[str],
    ) -> None:
        for published in published_transcodes:
            try:
                published.finalize()
            except Exception as exc:
                # The file and database transaction are already complete.
                # Cleanup failure must not invite a duplicate download.
                warning = "转换已完成，但旧文件清理失败"
                completion_warnings.append(warning)
                self._log(
                    "warning",
                    "文件/清理",
                    warning,
                    output_path=str(published.final_path),
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

    def _remove_pending_transcodes(
        self,
        prepared_transcodes: list[PreparedTranscode],
    ) -> None:
        committed_ids = {id(item) for item in prepared_transcodes}
        self._pending_transcodes = [
            item for item in self._pending_transcodes
            if id(item) not in committed_ids
        ]

    def _announce_completed_media(
        self,
        media_items: list[MediaItem],
        completion_warnings: list[str],
    ) -> None:
        last_item = media_items[-1]
        for item in media_items:
            self.completed.emit(self.task_id, item)
        self._log(
            "info",
            "完成",
            "全部媒体文件已校验并原子写入完成列表",
            media_count=len(media_items),
            video_path=last_item.video_path,
        )
        completion_warning = "；".join(dict.fromkeys(completion_warnings))
        completion_text = (
            f"下载完成；{completion_warning}"
            if completion_warning else "下载完成"
        )
        self._download_completed = True
        self._set_stage(
            "completed",
            completion_text,
            100,
            force=True,
            completion_warning=completion_warning,
        )

    def _commit_completed_media(
        self,
        media_items: list[MediaItem],
        prepared_transcodes: list[PreparedTranscode],
        completion_warnings: list[str],
    ) -> None:
        """Atomically publish prepared files, task state and media catalog rows."""

        completion_task = self._completed_download_task(media_items)
        published_transcodes: list[PublishedTranscode] = []
        try:
            self._publish_prepared_transcodes(
                prepared_transcodes,
                published_transcodes,
            )
            task_files = completed_task_file_manifest(
                self.output_dir,
                media_items,
                self.download_options,
                subtitle_language=self.subtitle_language,
            )
            self.db.complete_download_task_batch(completion_task, media_items, task_files)
        except BaseException:
            self._rollback_completed_media_publication(
                published_transcodes,
                prepared_transcodes[len(published_transcodes):],
            )
            raise
        self._finalize_committed_transcodes(
            published_transcodes,
            completion_warnings,
        )
        self._remove_pending_transcodes(prepared_transcodes)
        self._announce_completed_media(media_items, completion_warnings)

    @staticmethod
    def _transcode_presentation(
        codec: str,
        device: str,
        prepend_cover: bool,
    ) -> tuple[str, str, str]:
        target_label = {
            "h264": "H.264",
            "h265": "H.265",
            "av1": "AV1",
            "original": "原编码",
        }[codec]
        device_label = {
            "auto": "自动选择",
            "gpu": "GPU",
            "cpu": "CPU",
        }[device]
        action_text = (
            f"正在转换为 {target_label} 并插入开头封面"
            if prepend_cover and codec != "original"
            else "正在视频开头插入封面"
            if prepend_cover
            else f"正在转换为 {target_label}（{device_label}）"
        )
        return target_label, device_label, action_text

    @contextmanager
    def _transcode_workspace(
        self,
        video_path: str,
    ) -> Iterator[Path | None]:
        """Create the temp directory and own all transcode reservations."""

        transcode_temp_dir = (
            self._processing_workspace / "transcode"
            if self._processing_workspace is not None else None
        )
        if transcode_temp_dir is not None:
            transcode_temp_dir.mkdir(parents=True, exist_ok=True)
        lease = self._disk_lease
        reservation_keys: list[str] = []
        normalized_video_path = str(Path(video_path))
        try:
            if lease is not None:
                estimate = transcode_capacity_estimate(video_path)
                temp_capacity_target = transcode_temp_dir or Path(video_path)
                temporary_key = f"transcode\x1f{normalized_video_path}\x1ftemp"
                lease.acquire(
                    temporary_key,
                    temp_capacity_target,
                    estimate,
                    cancel_event=self._cancel,
                )
                reservation_keys.append(temporary_key)
                if not same_storage_volume(temp_capacity_target, video_path):
                    final_key = f"transcode\x1f{normalized_video_path}\x1ffinal"
                    lease.acquire(
                        final_key,
                        video_path,
                        estimate,
                        cancel_event=self._cancel,
                    )
                    reservation_keys.append(final_key)
            yield transcode_temp_dir
        finally:
            if lease is not None and reservation_keys:
                try:
                    lease.release_keys(reservation_keys)
                except Exception as exc:
                    # Failed keys remain owned by the lease and are retried by
                    # both DownloadWorker.finally and DownloadService after
                    # the QObject has been deleted.
                    self._cleanup_warning(
                        "磁盘/存储",
                        "释放格式转换磁盘预留失败，将在线程结束时重试",
                        error=str(exc),
                        remaining=lease.active_count,
                    )

    def _build_validated_transcode(
        self,
        entry: Mapping[str, Any],
        video_path: str,
        thumbnail: Path | None,
        ffprobe_path: str,
        configured_ffmpeg: str,
        prepend_cover: bool,
        progress: Callable[[float, str], None],
    ) -> tuple[PreparedTranscode, MediaValidationResult]:
        """Prepare one transcode and discard it if validation cannot finish."""

        prepared: PreparedTranscode | None = None
        try:
            stream_info = probe_video_stream(video_path, ffprobe_path)
            with self._transcode_workspace(video_path) as transcode_temp_dir:
                prepared = self._prepare_transcode_output(
                    entry,
                    video_path,
                    thumbnail,
                    configured_ffmpeg,
                    stream_info,
                    transcode_temp_dir,
                    prepend_cover,
                    progress,
                )
                validation = self._validate_prepared_transcode(
                    prepared,
                    stream_info,
                    ffprobe_path,
                )
            return prepared, validation
        except BaseException:
            if prepared is not None:
                self._discard_failed_transcode(prepared)
            raise

    @staticmethod
    def _entry_duration(entry: Mapping[str, Any]) -> float:
        try:
            return max(0.0, float(entry.get("duration") or 0.0))
        except (TypeError, ValueError, OverflowError):
            return 0.0

    def _prepare_transcode_output(
        self,
        entry: Mapping[str, Any],
        video_path: str,
        thumbnail: Path | None,
        configured_ffmpeg: str,
        stream_info: VideoStreamInfo,
        transcode_temp_dir: Path | None,
        prepend_cover: bool,
        progress: Callable[[float, str], None],
    ) -> PreparedTranscode:
        self._set_stage(
            "waiting_disk",
            "正在为格式转换预留磁盘空间",
            0,
            force=True,
        )
        return prepare_transcode_media(
            video_path,
            configured_ffmpeg,
            self.transcode_codec,
            self.transcode_device,
            encoder=self.transcode_encoder,
            duration_seconds=(
                stream_info.duration_seconds or self._entry_duration(entry)
            ),
            cancel_event=self._cancel,
            progress=progress,
            cover_path=str(thumbnail or ""),
            prepend_cover_frames=(
                self.download_options.prepend_cover_frames if prepend_cover else 0
            ),
            source_codec=stream_info.codec,
            source_width=stream_info.width,
            source_height=stream_info.height,
            source_frame_rate=stream_info.frame_rate,
            source_has_audio=stream_info.has_audio,
            source_info=stream_info,
            temporary_dir=str(transcode_temp_dir or ""),
            output_container=self.download_options.container,
        )

    def _validate_prepared_transcode(
        self,
        prepared: PreparedTranscode,
        source_info: VideoStreamInfo,
        ffprobe_path: str,
    ) -> MediaValidationResult:
        self._set_stage(
            "verifying",
            "正在校验转换临时成品",
            0,
            force=True,
        )
        validation = validate_media_file(
            prepared.temporary_path,
            ffprobe_path,
            require_video=True,
            require_audio=source_info.has_audio,
            cancel_event=self._cancel,
        )
        converted_info = (
            video_stream_info_from_probe_payload(validation.probe_payload)
            if validation.probe_payload
            else probe_video_stream(prepared.temporary_path, ffprobe_path)
        )
        validate_transcode_topology(source_info, converted_info)
        return validation

    def _discard_failed_transcode(self, prepared: PreparedTranscode) -> None:
        try:
            prepared.discard()
        except BaseException as cleanup_error:
            self._cleanup_warning(
                "文件/清理",
                "转码校验失败后清理临时文件失败，已保留供检查",
                temporary_path=str(prepared.temporary_path),
                error=str(cleanup_error),
                error_type=type(cleanup_error).__name__,
            )

    def _optional_transcode_plan(
        self,
        video_path: str,
        thumbnail: Path | None,
        completion_warnings: list[str],
    ) -> _OptionalTranscodePlan:
        verification_path = Path(video_path)
        prepend_cover = (
            self.download_options.prepend_cover_enabled
            and self.download_options.content_mode != "audio"
        )
        if prepend_cover and thumbnail is None:
            warning = "未获取到可用封面，已跳过视频开头封面插帧"
            completion_warnings.append(warning)
            self._log("warning", "封面", warning, input_path=video_path)
            prepend_cover = False
        requires_transcode = (
            self.download_options.content_mode != "audio"
            and (self.transcode_codec != "original" or prepend_cover)
        )
        if not requires_transcode:
            return _OptionalTranscodePlan(
                video_path=video_path,
                verification_path=verification_path,
                thumbnail=thumbnail,
                prepend_cover=prepend_cover,
                required=False,
            )
        target_label, device_label, action_text = self._transcode_presentation(
            self.transcode_codec,
            self.transcode_device,
            prepend_cover,
        )
        return _OptionalTranscodePlan(
            video_path=video_path,
            verification_path=verification_path,
            thumbnail=thumbnail,
            prepend_cover=prepend_cover,
            required=True,
            target_label=target_label,
            device_label=device_label,
            action_text=action_text,
        )

    def _begin_optional_transcode(self, plan: _OptionalTranscodePlan) -> None:
        self._set_stage("transcoding", plan.action_text, 0, force=True)
        self._log(
            "info",
            "格式/转换",
            "开始转换下载成品",
            codec=self.transcode_codec,
            device=self.transcode_device,
            input_path=plan.video_path,
            prepend_cover=plan.prepend_cover,
            prepend_cover_frames=(
                self.download_options.prepend_cover_frames if plan.prepend_cover else 0
            ),
        )

    def _optional_transcode_progress_reporter(
        self,
        action_text: str,
    ) -> Callable[[float, str], None]:
        def report_transcode_progress(percent: float, encoder: str) -> None:
            now = time.monotonic()
            if (
                percent < 100.0
                and self._last_transcode_progress_emit_at
                and now - self._last_transcode_progress_emit_at
                < TRANSCODE_PROGRESS_EMIT_INTERVAL_SECONDS
            ):
                return
            self._last_transcode_progress_emit_at = now
            self._set_stage(
                "transcoding",
                f"{action_text} · {encoder}",
                percent,
                force=percent >= 100.0,
                transcode_encoder=encoder,
            )
        return report_transcode_progress

    def _optional_transcode_was_cancelled(self, error: Exception) -> bool:
        if self._cancel.is_set() or isinstance(error, InterruptedError):
            return True
        if isinstance(error, MediaValidationError):
            return error.code == MediaValidationErrorCode.CANCELLED
        if isinstance(error, DiskCapacityError):
            return error.code == DiskCapacityErrorCode.CANCELLED
        return False

    def _optional_transcode_failure_warning(
        self,
        plan: _OptionalTranscodePlan,
    ) -> str:
        if plan.prepend_cover and self.transcode_codec == "original":
            return "视频开头封面插入失败，已保留原始下载文件"
        return (
            f"{plan.target_label} {plan.device_label} "
            "转换失败，已保留原始下载文件"
        )

    def _handle_optional_transcode_failure(
        self,
        plan: _OptionalTranscodePlan,
        error: Exception,
        completion_warnings: list[str],
    ) -> tuple[str, Path, None, None]:
        warning = self._optional_transcode_failure_warning(plan)
        completion_warnings.append(warning)
        self._log(
            "warning",
            "格式/转换",
            warning,
            codec=self.transcode_codec,
            device=self.transcode_device,
            encoder=self.transcode_encoder,
            prepend_cover=plan.prepend_cover,
            input_path=plan.video_path,
            error=str(error),
            error_type=type(error).__name__,
        )
        self._set_stage(
            "transcoding",
            warning,
            100,
            force=True,
            completion_warning=warning,
        )
        return plan.video_path, plan.verification_path, None, None

    def _prepare_optional_transcode(
        self,
        entry: Mapping[str, Any],
        video_path: str,
        thumbnail: Path | None,
        ffprobe_path: str,
        completion_warnings: list[str],
    ) -> tuple[str, Path, MediaValidationResult | None, PreparedTranscode | None]:
        """Prepare and validate an optional transcode without publishing it."""

        plan = self._optional_transcode_plan(
            video_path,
            thumbnail,
            completion_warnings,
        )
        if not plan.required:
            return video_path, plan.verification_path, None, None
        self._begin_optional_transcode(plan)

        try:
            configured_ffmpeg = ffmpeg_runtime_path(self.ffmpeg_path)
            prepared, validation = self._build_validated_transcode(
                entry,
                plan.video_path,
                plan.thumbnail,
                ffprobe_path,
                configured_ffmpeg,
                plan.prepend_cover,
                self._optional_transcode_progress_reporter(plan.action_text),
            )
        except Exception as exc:
            if self._optional_transcode_was_cancelled(exc):
                raise InterruptedError("用户取消格式转换") from exc
            return self._handle_optional_transcode_failure(
                plan,
                exc,
                completion_warnings,
            )

        self._log(
            "info",
            "格式/转换",
            "格式转换完成",
            codec=self.transcode_codec,
            encoder=prepared.encoder,
            output_path=str(prepared.target_path),
            prepend_cover=plan.prepend_cover,
        )
        return (
            str(prepared.target_path),
            prepared.temporary_path,
            validation,
            prepared,
        )

    def _completed_entry_paths(
        self,
        entry: Mapping[str, Any],
        prepare_filename: Callable[[Mapping[str, Any]], str],
    ) -> _CompletedEntryPaths:
        requested = tuple(
            item for item in (entry.get("requested_downloads") or ())
            if isinstance(item, Mapping)
        )
        filepath = entry.get("filepath") or entry.get("_filename") or (
            requested[0].get("filepath") if requested else ""
        ) or prepare_filename(entry)
        base = Path(filepath)

        # Prefer the exact path returned by yt-dlp when it exists. The former
        # unconditional .mp4 preference could catalogue a stale file from an
        # older run while the current download had successfully produced WebM.
        video = base
        if not base.is_file():
            effective_container = self.download_options.effective_container()
            fallback_suffixes = []
            if effective_container != "auto":
                fallback_suffixes.append(f".{effective_container}")
            if ".mp4" not in fallback_suffixes:
                fallback_suffixes.append(".mp4")
            video = next(
                (
                    candidate
                    for suffix in fallback_suffixes
                    if (candidate := base.with_suffix(suffix)).is_file()
                ),
                base,
            )

        info_json = base.with_suffix(".info.json")
        if not info_json.is_file():
            info_json = None
        return _CompletedEntryPaths(requested, base, video, info_json)

    def _prepare_entry_thumbnail(
        self,
        base: Path,
        video_path: Path,
    ) -> Path | None:
        self._set_stage("thumbnail", "正在整理封面", 99)
        thumbnail = self._thumbnails.finalize(base, video_path)
        if thumbnail is not None:
            # The early id-based preview may have been renamed to the final
            # media title. Publish the new path before any card is rebuilt.
            self._thumbnails.path = str(thumbnail)
            self._set_stage(
                "thumbnail",
                "封面整理完成",
                99,
                force=True,
                thumbnail_path=str(thumbnail),
            )
        return thumbnail

    def _discard_unwanted_thumbnail(self, thumbnail: Path | None) -> Path | None:
        if thumbnail is None or self.download_options.write_thumbnail:
            return thumbnail
        try:
            thumbnail.unlink(missing_ok=True)
        except OSError as exc:
            self._log(
                "warning",
                "封面",
                "临时封面清理失败",
                source_path=str(thumbnail),
                error=str(exc),
            )
            return thumbnail
        return None

    @staticmethod
    def _completed_entry_requested_audio(
        entry: Mapping[str, Any],
        requested: tuple[Mapping[str, Any], ...],
    ) -> bool:
        if requested:
            return any(
                str(download.get("acodec") or "").strip().casefold()
                not in {"", "none"}
                for download in requested
            )
        return str(entry.get("acodec") or "").strip().casefold() not in {"", "none"}

    def _validate_completed_entry(
        self,
        entry: Mapping[str, Any],
        requested: tuple[Mapping[str, Any], ...],
        video_path: str,
        verification_path: Path,
        transcode_validation: MediaValidationResult | None,
        ffprobe_path: str,
    ) -> MediaValidationResult:
        self._set_stage("verifying", "正在校验媒体文件", 99)
        validation = transcode_validation or validate_media_file(
            verification_path,
            ffprobe_path,
            require_video=self.download_options.content_mode != "audio",
            require_audio=(
                self._completed_entry_requested_audio(entry, requested)
                or self.download_options.content_mode == "audio"
            ),
            cancel_event=self._cancel,
        )
        self._log(
            "info",
            "文件/校验",
            "媒体成品校验通过",
            video_path=video_path,
            size_bytes=validation.size_bytes,
            duration_seconds=round(validation.duration_seconds, 3),
            container=validation.container,
            video_streams=validation.video_stream_count,
            audio_streams=validation.audio_stream_count,
        )
        return validation

    def _completed_media_item(
        self,
        entry: Mapping[str, Any],
        video_path: str,
        thumbnail: Path | None,
        info_json: Path | None,
    ) -> MediaItem:
        self._set_stage("metadata", "正在写入元数据", 99)
        return MediaItem(
            source_url=entry.get("webpage_url") or self.url,
            title=entry.get("title") or "",
            description=entry.get("description") or "",
            tags=entry.get("tags") or [],
            uploader=entry.get("uploader") or "",
            thumbnail_path=str(thumbnail or ""),
            video_path=video_path,
            metadata_json_path=str(info_json or ""),
            source_ip=self._source_ip_for_media(),
            proxy_profile=self.proxy,
        )

    def _prepare_completed_entry(
        self,
        entry: Mapping[str, Any],
        prepare_filename: Callable[[Mapping[str, Any]], str],
        ffprobe_path: str,
        completion_warnings: list[str],
        *,
        release_capacity: bool,
        allow_optional_transcode: bool = True,
    ) -> tuple[MediaItem, PreparedTranscode | None]:
        """Validate one downloaded entry and build its uncommitted catalog row."""

        paths = self._completed_entry_paths(entry, prepare_filename)
        thumbnail = self._prepare_entry_thumbnail(paths.base, paths.video)

        if allow_optional_transcode:
            (
                video_path,
                verification_path,
                transcode_validation,
                prepared_transcode,
            ) = self._prepare_optional_transcode(
                entry,
                str(paths.video),
                thumbnail,
                ffprobe_path,
                completion_warnings,
            )
        else:
            plan = self._optional_transcode_plan(
                str(paths.video),
                thumbnail,
                completion_warnings,
            )
            if plan.required:
                warning = "磁盘预留清理异常，已跳过可选格式转换并保留原始下载文件"
                completion_warnings.append(warning)
                self._cleanup_warning(
                    "磁盘/存储",
                    warning,
                    input_path=str(paths.video),
                    remaining=(
                        self._disk_lease.active_count
                        if self._disk_lease is not None else 0
                    ),
                )
            video_path = str(paths.video)
            verification_path = paths.video
            transcode_validation = None
            prepared_transcode = None
        if prepared_transcode is not None:
            # Register immediately so a later metadata/validation exception
            # is still cleaned by the worker's finalizer.
            self._pending_transcodes.append(prepared_transcode)

        thumbnail = self._discard_unwanted_thumbnail(thumbnail)
        self._validate_completed_entry(
            entry,
            paths.requested,
            video_path,
            verification_path,
            transcode_validation,
            ffprobe_path,
        )
        item = self._completed_media_item(
            entry,
            video_path,
            thumbnail,
            paths.info_json,
        )
        if release_capacity:
            self._capacity_post_hook(video_path)
        return item, prepared_transcode

    def _complete_download_info(
        self,
        info: Mapping[str, Any],
        prepare_filename: Callable[[Mapping[str, Any]], str],
        *,
        release_capacity: bool = False,
    ) -> None:
        entries = self._download_result_entries(info)
        if not entries:
            raise RuntimeError("下载没有生成任何可用媒体条目，请重新解析链接后重试。")

        ffprobe_path = ffprobe_runtime_path(self.ffmpeg_path, self.ffprobe_path)
        media_items: list[MediaItem] = []
        completion_warnings: list[str] = []
        prepared_transcodes: list[PreparedTranscode] = []
        # yt-dlp has finished all network/merge work at this point. Release its
        # reservations before taking a dedicated temporary-output reservation.
        allow_optional_transcode = True
        if self._disk_lease is not None:
            try:
                self._disk_lease.release_all()
            except Exception as exc:
                # The media download itself is already complete.  Preserve it
                # and avoid waiting on this worker's retained reservation in a
                # new optional transcode; finalizers will retry the cleanup.
                allow_optional_transcode = False
                self._cleanup_warning(
                    "磁盘/存储",
                    "下载完成后未能释放全部磁盘预留，将保留原始媒体并在线程结束时重试",
                    error=str(exc),
                    remaining=self._disk_lease.active_count,
                )
        for entry in entries:
            item, prepared_transcode = self._prepare_completed_entry(
                entry,
                prepare_filename,
                ffprobe_path,
                completion_warnings,
                release_capacity=release_capacity,
                allow_optional_transcode=allow_optional_transcode,
            )
            media_items.append(item)
            if prepared_transcode is not None:
                prepared_transcodes.append(prepared_transcode)

        self._commit_completed_media(
            media_items,
            prepared_transcodes,
            completion_warnings,
        )

    @staticmethod
    def _adaptive_probe_options(
        ydl_opts: Mapping[str, Any],
        *,
        include_cookies: bool,
    ) -> dict[str, Any]:
        """Build a bounded metadata-only probe without output side effects."""
        excluded = {
            "format",
            "outtmpl",
            "progress_hooks",
            "postprocessor_hooks",
            "postprocessors",
            "match_filter",
            "writethumbnail",
            "writeinfojson",
            "writesubtitles",
            "writeautomaticsub",
            "writedescription",
            "getcomments",
            "download_sections",
            "live_from_start",
            "wait_for_video",
        }
        options = {
            key: value
            for key, value in ydl_opts.items()
            if key not in excluded and (include_cookies or key not in _COOKIE_OPTION_KEYS)
        }
        options.update({
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "logger": _SilentYtdlpProbeLogger(),
        })
        # Never expand an entire large collection twice merely to decide
        # whether cookies help. Single-video extraction is unaffected by flat
        # playlist mode; aggregate links are capped to a representative batch.
        if not options.get("noplaylist"):
            options["extract_flat"] = "in_playlist"
            existing_end = options.get("playlistend")
            try:
                options["playlistend"] = min(50, max(1, int(existing_end))) if existing_end else 50
            except (TypeError, ValueError):
                options["playlistend"] = 50
        return options

    def _probe_media_access(
        self,
        external_executable: str,
        ydl_opts: Mapping[str, Any],
        *,
        include_cookies: bool,
    ) -> dict[str, Any]:
        options = self._adaptive_probe_options(ydl_opts, include_cookies=include_cookies)
        if external_executable:
            return run_external_ytdlp(
                external_executable,
                self.url,
                options,
                download=False,
                cancel_event=self._cancel,
            )
        if yt_dlp is None:
            raise RuntimeError("yt-dlp 解析核心不可用")
        with yt_dlp.YoutubeDL(options) as probe:
            result = probe.extract_info(self.url, download=False)
        return result if isinstance(result, dict) else {}

    def _probe_adaptive_access_candidate(
        self,
        mode: str,
        external_executable: str,
        ydl_opts: Mapping[str, Any],
        *,
        include_cookies: bool,
    ) -> _AdaptiveAccessProbe:
        if self._cancel.is_set():
            raise InterruptedError("用户取消下载")
        try:
            result = self._probe_media_access(
                external_executable,
                ydl_opts,
                include_cookies=include_cookies,
            )
        except InterruptedError:
            # Cancellation is control flow, not evidence that one access mode
            # is worse than the other. Never mutate the real download options
            # after the user has requested an immediate stop.
            raise
        except Exception as exc:
            return _AdaptiveAccessProbe(
                mode=mode,
                result=None,
                profile=media_capability_profile(None),
                error_name=type(exc).__name__,
            )
        return _AdaptiveAccessProbe(
            mode=mode,
            result=result,
            profile=media_capability_profile(result),
        )

    @staticmethod
    def _choose_adaptive_cookie_access(
        anonymous: _AdaptiveAccessProbe,
        cookie: _AdaptiveAccessProbe,
    ) -> _AdaptiveCookieDecision:
        anonymous_profile = anonymous.profile
        cookie_profile = cookie.profile
        if anonymous_profile.usable and not cookie_profile.usable:
            return _AdaptiveCookieDecision(True, "cookie_result_unusable")
        if cookie_profile.usable and not anonymous_profile.usable:
            return _AdaptiveCookieDecision(False, "cookie_required")
        if anonymous_profile.usable and cookie_profile.usable:
            if anonymous_profile.score > cookie_profile.score:
                return _AdaptiveCookieDecision(True, "anonymous_quality_higher")
            if cookie_profile.score > anonymous_profile.score:
                return _AdaptiveCookieDecision(False, "cookie_quality_higher")
            return _AdaptiveCookieDecision(False, "quality_equal_keep_cookie")
        if anonymous.result is not None and cookie.result is None:
            # Metadata-only extractors may not expose a conventional formats
            # list. If only anonymous access completed, let the real download
            # use that viable route instead of a known-failed Cookie route.
            return _AdaptiveCookieDecision(True, "cookie_probe_failed")
        if anonymous.result is None and cookie.result is not None:
            return _AdaptiveCookieDecision(False, "anonymous_probe_failed")
        return _AdaptiveCookieDecision(False, "both_probes_failed_keep_cookie")

    def _apply_adaptive_cookie_decision(
        self,
        ydl_opts: dict[str, Any],
        anonymous: _AdaptiveAccessProbe,
        cookie: _AdaptiveAccessProbe,
        decision: _AdaptiveCookieDecision,
    ) -> dict[str, Any] | None:
        chosen_probe = anonymous if decision.use_anonymous else cookie
        if decision.use_anonymous:
            for key in _COOKIE_OPTION_KEYS:
                ydl_opts.pop(key, None)
            level = "warning" if cookie.profile.usable else "info"
            message = "智能 Cookie 策略已改用匿名访问"
        else:
            level = "info"
            message = "智能 Cookie 策略已保留 Cookie 访问"

        self._log(
            level,
            "风控/登录",
            message,
            reason=decision.reason,
            anonymous=anonymous.profile.as_log_details(),
            cookie=cookie.profile.as_log_details(),
            anonymous_error=anonymous.error_name,
            cookie_error=cookie.error_name,
        )
        chosen = chosen_probe.result
        if isinstance(chosen, Mapping) and is_ytdlp_collection_result(chosen):
            # The adaptive probe intentionally caps/flattens collections. The
            # regular collection parser must still obtain the complete result.
            return None
        return chosen

    def _select_adaptive_cookie_access(
        self,
        external_executable: str,
        ydl_opts: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Choose anonymous or Cookie access by comparing real media output."""
        if not any(key in ydl_opts for key in _COOKIE_OPTION_KEYS):
            return None

        self._set_stage("parsing", "正在智能比较匿名与 Cookie 解析结果")
        self._log(
            "info",
            "风控/登录",
            "开始智能比较匿名访问与 Cookie 访问",
            policy="compare_media_capabilities",
        )
        anonymous = self._probe_adaptive_access_candidate(
            "anonymous",
            external_executable,
            ydl_opts,
            include_cookies=False,
        )
        cookie = self._probe_adaptive_access_candidate(
            "cookie",
            external_executable,
            ydl_opts,
            include_cookies=True,
        )
        decision = self._choose_adaptive_cookie_access(anonymous, cookie)
        return self._apply_adaptive_cookie_decision(
            ydl_opts,
            anonymous,
            cookie,
            decision,
        )

    def _run_external_flow(
        self,
        executable: str,
        ydl_opts: dict[str, Any],
        progress_hook: Callable[[dict[str, Any]], None],
        initial_preview: dict[str, Any] | None = None,
    ) -> None:
        def probe(operation: str) -> dict[str, Any]:
            return self._run_with_network_retry(
                lambda: run_external_ytdlp(
                    executable,
                    self.url,
                    ydl_opts,
                    download=False,
                    cancel_event=self._cancel,
                    log_line=self._external_log_line,
                ),
                operation,
            )

        initial_format = ydl_opts.get("format")
        preview = self._prepare_preview_and_selection(
            ydl_opts,
            initial_preview=initial_preview,
            probe=probe,
            parse_log_message="开始使用外置 yt-dlp 解析视频信息",
            missing_selection_error=ExternalYtdlpError,
        )

        if self._disk_lease is not None:
            capacity_preview = (
                preview
                if preview is not None and ydl_opts.get("format") == initial_format
                else probe("下载容量解析")
            )
            self._reserve_external_preview_capacity(capacity_preview)

        self._log("info", "下载", "开始调用外置 yt-dlp", executable=executable)
        info = self._run_with_network_retry(
            lambda: run_external_ytdlp(
                executable,
                self.url,
                ydl_opts,
                download=True,
                cancel_event=self._cancel,
                log_line=self._external_log_line,
                progress_hook=lambda payload: progress_hook(self._external_progress_payload(payload)),
                postprocess_hook=lambda payload: self._handle_postprocessor(
                    self._external_progress_payload(payload)
                ),
            ),
            "视频下载",
        )
        self._complete_download_info(info, self._external_prepare_filename, release_capacity=True)

    def _reserve_external_preview_capacity(
        self,
        preview: Mapping[str, Any],
    ) -> None:
        preview_entries = (
            preview.get("entries")
            if is_ytdlp_collection_result(preview)
            else [preview]
        )

        resolver = SimpleNamespace(prepare_filename=self._external_prepare_filename)
        for entry in preview_entries or []:
            if isinstance(entry, Mapping):
                self._capacity_match_filter(resolver, entry)

    @staticmethod
    def _external_core_candidate(core_mode: str) -> tuple[str, str | None]:
        if core_mode == "builtin":
            return "", None
        executable = ytdlp_runtime_path()
        version = cached_external_ytdlp_version(executable) if executable else None
        return executable, version

    def _log_core_selection(
        self,
        selection: YtdlpCoreSelection,
        candidate_executable: str,
    ) -> None:
        if selection.external_rejected:
            self._log(
                "warning",
                "格式/工具",
                "检测到外置 yt-dlp，但版本探测失败，已回退内置模块",
                executable=candidate_executable,
            )
        if selection.uses_external:
            self._log(
                "info",
                "格式/工具",
                "已选择可独立更新的外置 yt-dlp 下载核心",
                version=selection.external_version or "",
                executable=selection.executable,
                core_mode=selection.mode,
            )
            return
        message = (
            "已按设置使用内置 yt-dlp 下载核心（随主程序更新）"
            if selection.mode == "builtin"
            else "未找到可用外置 yt-dlp，使用内置 Python 模块回退"
        )
        self._log("info", "格式/工具", message, core_mode=selection.mode)

    def _resolve_download_core(self) -> str:
        core_mode = normalize_ytdlp_core_mode(self.ytdlp_core_mode)
        executable, version = self._external_core_candidate(core_mode)
        try:
            selection = select_ytdlp_core(
                core_mode,
                external_executable=executable,
                external_version=version,
                builtin_available=yt_dlp is not None,
                packaged=bool(getattr(sys, "frozen", False)),
            )
        except YtdlpCoreSelectionError as exc:
            if executable and version == "":
                self._log(
                    "warning",
                    "格式/工具",
                    "检测到外置 yt-dlp，但版本探测失败",
                    executable=executable,
                )
            raise _DownloadSetupError(
                str(exc),
                category="格式/工具",
                details={"core_mode": exc.mode, "reason": exc.reason},
            ) from exc
        self._log_core_selection(selection, executable)
        return selection.executable

    def _prepare_run_environment(self) -> None:
        try:
            Path(self.output_dir).mkdir(parents=True, exist_ok=True)
            self._processing_workspace = processing_temp_workspace(
                self.processing_temp_dir,
                self.task_id,
                "download",
            )
        except Exception as exc:
            raise _DownloadSetupError(
                f"下载目录或临时处理目录不可用：\n{exc}",
                category="用户操作",
                log_message="下载目录或临时处理目录不可用",
                details={
                    "error": str(exc),
                    "output_dir": self.output_dir,
                    "processing_temp_dir": self.processing_temp_dir,
                },
            ) from exc
        self._log("info", "任务", "输出目录已准备")
        if self._processing_workspace is not None:
            self._log(
                "info",
                "磁盘/存储",
                "已启用独立临时处理目录，分片、合并和转码将在临时磁盘完成后提交到保存目录",
                processing_workspace=str(self._processing_workspace),
                output_dir=self.output_dir,
            )
        if self.cookie_source == "file" and self.cookie_file:
            cookie_path = Path(self.cookie_file).expanduser()
            if not cookie_path.is_file():
                raise _DownloadSetupError(
                    "下载 Cookie 文件不存在或不是文件，请在设置中重新选择 Netscape Cookie 文件。",
                    category="风控/登录",
                    log_message="下载 Cookie 文件不可用",
                )

    @staticmethod
    def _download_interruption(message: str) -> Exception:
        download_error = getattr(getattr(yt_dlp, "utils", None), "DownloadError", None)
        return download_error(message) if download_error is not None else InterruptedError(message)

    @staticmethod
    def _progress_bytes(*values: Any) -> int:
        """Return the first finite positive byte counter from a callback."""

        for value in values:
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError):
                continue
            if number > 0.0 and isfinite(number):
                return int(number)
        return 0

    def _raise_if_progress_canceled(self) -> None:
        if not self._cancel.is_set():
            return
        self._log("info", "用户操作", "收到取消或暂停请求")
        raise self._download_interruption("用户取消下载")

    def _progress_info(self, data: Mapping[str, Any]) -> dict[str, Any]:
        candidate = data.get("info_dict")
        if isinstance(candidate, Mapping):
            info = dict(candidate)
            self._current_info = info
            return info
        return self._current_info if isinstance(self._current_info, dict) else {}

    def _enrich_progress_metadata(
        self,
        payload: dict[str, Any],
        info: Mapping[str, Any],
    ) -> None:
        quality_text = selected_video_quality(info)
        if quality_text:
            payload["selected_quality"] = quality_text
        if self._thumbnails.attempted:
            return
        thumbnail_url = info.get("thumbnail")
        if not thumbnail_url:
            return
        thumbnail_path = self._thumbnails.save_preview(
            str(thumbnail_url),
            str(info.get("id") or "video"),
        )
        # Preview cancellation is quiet, but the active media hook must stop.
        self._raise_if_progress_canceled()
        if thumbnail_path:
            payload["thumbnail_path"] = thumbnail_path

    @staticmethod
    def _progress_stream_kind(stage: str) -> str:
        if stage == "downloading_video":
            return "video"
        if stage == "downloading_audio":
            return "audio"
        return ""

    def _apply_worker_transfer_progress(
        self,
        payload: dict[str, Any],
        info: Mapping[str, Any],
        total: int,
        done: int,
    ) -> None:
        status = str(payload.get("status") or "").casefold()
        if status not in {"downloading", "finished"}:
            return
        if status == "downloading":
            self._check_disk_low_watermark(downloaded_bytes=done)
        self._media_download_started = True
        stage, stage_text = self._download_stage(dict(info))
        stream_progress = (
            100.0
            if status == "finished"
            else min(100.0, done * 100.0 / total) if total else 0.0
        )
        self._set_stage(
            stage,
            stage_text,
            stream_progress if stage != "downloading" or status == "finished" else None,
            publish=False,
        )
        stream_kind = self._progress_stream_kind(stage)
        if stream_kind:
            payload["stream_kind"] = stream_kind
            payload["stream_progress"] = stream_progress

    def _log_transfer_milestone(self, total: int, done: int) -> None:
        if total <= 0:
            return
        bucket = min(100, int(done * 100 / total) // 10 * 10)
        if bucket <= self._last_progress_log:
            return
        self._last_progress_log = bucket
        self._log(
            "info",
            "进度",
            f"下载进度约 {bucket}%",
            downloaded_bytes=done,
            total_bytes=total,
        )

    def _publish_worker_progress(self, payload: dict[str, Any]) -> None:
        now = time.monotonic()
        payload.update({
            "stage": self._stage,
            "stage_text": self._stage_text,
            "stage_progress": self._stage_progress,
            "retry_count": self._retry_count,
            "retry_total": self._retry_total,
            "elapsed_seconds": max(0.0, now - self._started_at),
            "stage_elapsed_seconds": max(0.0, now - self._stage_started_at),
        })
        self._emit_worker_progress(
            payload,
            force=str(payload.get("status") or "").casefold() != "downloading",
        )

    def _progress_hook(self, data: dict[str, Any]) -> None:
        self._raise_if_progress_canceled()
        payload = dict(data)
        info = self._progress_info(payload)
        self._enrich_progress_metadata(payload, info)
        total = self._progress_bytes(
            payload.get("total_bytes"),
            payload.get("total_bytes_estimate"),
        )
        done = self._progress_bytes(payload.get("downloaded_bytes"))
        self._apply_worker_transfer_progress(payload, info, total, done)
        self._log_transfer_milestone(total, done)
        self._publish_worker_progress(payload)

    def _build_ytdlp_options(self) -> dict[str, Any]:
        template = self.filename_template.strip() or "%(title)s [%(id)s].%(ext)s"
        output_template, filename_limit = self._download_output_template(template)
        self._log_ytdlp_option_summary()
        ejs_options = self._resolve_ytdlp_ejs_options()
        configured_ffmpeg = ffmpeg_runtime_path(self.ffmpeg_path)
        ydl_opts = build_ytdlp_download_options(YtdlpDownloadOptionRequest(
            output_template=str(output_template),
            output_dir=self.output_dir,
            quality=self.quality,
            download_options=self.download_options,
            subtitle_language=self.subtitle_language,
            playlist_mode=self.playlist_mode,
            fragment_concurrent=self.fragment_concurrent,
            processing_workspace=(
                str(self._processing_workspace)
                if self._processing_workspace is not None else ""
            ),
            filename_limit=filename_limit,
            request_delay=self.request_delay,
            ffmpeg_location=configured_ffmpeg,
            proxy=self.proxy,
            ejs_options=ejs_options,
            remove_remux_postprocessor=(
                self.transcode_codec != "original"
                or self.download_options.prepend_cover_enabled
            ),
            windows_filenames=os.name == "nt",
        ))
        ydl_opts.update({
            "progress_hooks": [self._progress_hook],
            "postprocessor_hooks": [self._handle_postprocessor],
            "logger": _YtdlpLogger(self._handle_ytdlp_log),
        })
        return ydl_opts

    def _log_ytdlp_option_summary(self) -> None:
        self._log(
            "info",
            "下载",
            "已启用分片并发下载",
            fragment_concurrent=self.fragment_concurrent,
            note="仅对 DASH/HLS 等分片流生效；渐进式单文件流仍受服务器带宽限制",
        )
        if self.subtitle_language != "none":
            self._log(
                "info",
                "字幕",
                "已启用字幕下载",
                language=self.subtitle_language,
                priority="上传者字幕优先；无对应语言时使用自动字幕；均无则跳过",
            )

    def _resolve_ytdlp_ejs_options(self) -> dict[str, Any]:
        ejs_options, deno_path, ejs_source = ytdlp_ejs_runtime_options(
            self.deno_path,
            self.ytdlp_ejs_source,
        )
        if deno_path:
            source_label = {
                "auto": (
                    "自动（软件本地 yt-dlp-ejs）"
                    if "remote_components" not in ejs_options
                    else "自动（本地缺失，临时使用 Deno/npm）"
                ),
                "npm": "npm 远程组件",
                "github": "GitHub 远程组件",
                "local": "仅本地/随 yt-dlp 提供",
            }[ejs_source]
            self._log(
                "info",
                "格式/工具",
                "已启用 yt-dlp Deno/yt-dlp-ejs 支持",
                deno=deno_path,
                ejs_source=source_label,
            )
        else:
            self._log(
                "warning",
                "格式/工具",
                "未找到 Deno；部分 YouTube 格式可能缺少可下载格式",
            )
        return dict(ejs_options)

    def _configure_cookie_options(
        self,
        ydl_opts: dict[str, Any],
    ) -> MaterializedCookieSource:
        materialized: MaterializedCookieSource | None = None
        try:
            materialized = materialize_cookie_source(CookieSource(
                source=self.cookie_source,
                file=self.cookie_file,
                browser=self.cookie_browser,
                profile=self.cookie_profile,
                keyring=self.cookie_keyring,
                container=self.cookie_container,
            ))
            ydl_opts.update(materialized.options)
            if materialized.options:
                self._log(
                    "info",
                    "风控/登录",
                    "已配置 Cookie 来源",
                    source=materialized.normalized_source,
                    browser=(
                        self.cookie_browser
                        if materialized.normalized_source == COOKIE_SOURCE_BROWSER else ""
                    ),
                )
            return materialized
        except Exception as exc:
            if materialized is not None:
                self._cleanup_cookie_source(materialized)
            message = (
                "内置浏览器 Cookie 不可用"
                if self.cookie_source == COOKIE_SOURCE_EMBEDDED
                else "Cookie 配置不可用"
            )
            raise _DownloadSetupError(
                f"{message}：{exc}",
                category="风控/登录",
                log_message=message,
                details={"error": str(exc)},
            ) from exc

    @staticmethod
    def _probe_options(ydl_opts: Mapping[str, Any]) -> dict[str, Any]:
        excluded = {
            "format",
            "progress_hooks",
            "postprocessor_hooks",
            "writethumbnail",
            "writeinfojson",
        }
        return {key: value for key, value in ydl_opts.items() if key not in excluded}

    def _probe_download_info(
        self,
        ydl_opts: Mapping[str, Any],
        operation: str,
    ) -> dict[str, Any]:
        if yt_dlp is None:
            raise _DownloadSetupError(
                "内置 yt-dlp 下载核心不可用",
                category="格式/工具",
            )
        with yt_dlp.YoutubeDL(self._probe_options(ydl_opts)) as probe:
            preview = self._run_with_network_retry(
                lambda: probe.extract_info(self.url, download=False),
                operation,
            )
        if not isinstance(preview, dict):
            raise RuntimeError("yt-dlp 未返回可解析的视频信息")
        return preview

    def _publish_playlist_preview(self, preview: Mapping[str, Any]) -> None:
        entries = preview.get("entries") or []
        count = preview.get("playlist_count") or preview.get("n_entries")
        if not count:
            try:
                count = len(entries)
            except TypeError:
                count = 0
        is_playlist = is_ytdlp_collection_result(preview) or bool(count and count > 1)
        self.playlist_info.emit(self.task_id, {
            "is_playlist": is_playlist,
            "count": int(count or 0),
            "title": preview.get("title") or preview.get("playlist_title") or "",
        })
        self._log(
            "info",
            "解析",
            "视频信息解析完成",
            is_playlist=is_playlist,
            count=int(count or 0),
        )

    def _await_manual_format_selection(
        self,
        preview: Mapping[str, Any],
        ydl_opts: dict[str, Any],
        *,
        missing_selection_error: Callable[[str], Exception] | None = None,
    ) -> None:
        format_info: Mapping[str, Any] = preview
        if not preview.get("formats") and preview.get("entries"):
            first_entry = next(
                (entry for entry in preview["entries"] if isinstance(entry, Mapping)),
                None,
            )
            if first_entry is not None:
                format_info = first_entry
        thumbnail_path = ""
        thumbnail_url = preview.get("thumbnail") or format_info.get("thumbnail")
        if thumbnail_url:
            thumbnail_path = self._thumbnails.save_preview(
                thumbnail_url,
                preview.get("id") or format_info.get("id") or "video",
            )
        choices = build_format_choices(format_info)
        self._set_stage("waiting_selection", "等待选择下载内容或格式")
        self._log("info", "格式", "已生成可选下载内容和格式", choices=len(choices))
        self.formats_ready.emit(self.task_id, {
            "title": preview.get("title", ""),
            "thumbnail_path": thumbnail_path,
            "choices": choices,
            "content_mode": (
                "video"
                if self.download_options.content_mode == "manual"
                else self.download_options.content_mode
            ),
            "audio_format": self.download_options.audio_format,
        })
        self._format_event.wait(timeout=900)
        if self._cancel.is_set():
            raise self._download_interruption("用户取消下载")
        if not self.format_selector:
            self._log("warning", "格式", "用户未选择视频分辨率")
            error_factory = missing_selection_error or self._download_interruption
            raise error_factory("未选择视频分辨率")
        self._apply_manual_format_selection(ydl_opts)
        self._log("info", "格式", "已选择视频格式", selector=self.format_selector)

    def _prepare_preview_and_selection(
        self,
        ydl_opts: dict[str, Any],
        *,
        initial_preview: Mapping[str, Any] | None,
        probe: Callable[[str], Mapping[str, Any]],
        parse_log_message: str,
        missing_selection_error: Callable[[str], Exception] | None = None,
    ) -> Mapping[str, Any] | None:
        """Run the shared metadata preview and manual-format selection flow."""

        needs_preview = self.playlist_mode == "auto" or self._manual_selection_required()
        if not needs_preview:
            return initial_preview

        self._set_stage("parsing", "正在解析视频信息")
        preview = initial_preview
        if preview is None:
            self._log("info", "解析", parse_log_message)
            preview = probe("视频信息解析")
        if not isinstance(preview, Mapping):
            raise RuntimeError("yt-dlp 未返回可解析的视频信息")

        if self.playlist_mode == "auto":
            self._publish_playlist_preview(preview)
        if self._manual_selection_required():
            self._await_manual_format_selection(
                preview,
                ydl_opts,
                missing_selection_error=missing_selection_error,
            )
        return preview

    def _run_download_flow(
        self,
        external_executable: str,
        ydl_opts: dict[str, Any],
    ) -> None:
        self._check_disk_low_watermark(force=True)
        preview = self._select_adaptive_cookie_access(external_executable, ydl_opts)
        if external_executable:
            self._run_external_flow(
                external_executable,
                ydl_opts,
                self._progress_hook,
                initial_preview=preview,
            )
            return
        preview = self._prepare_preview_and_selection(
            ydl_opts,
            initial_preview=preview,
            probe=lambda operation: self._probe_download_info(ydl_opts, operation),
            parse_log_message="开始解析视频信息",
        )
        if self._stage not in {"downloading", "waiting_selection"}:
            self._set_stage("parsing", "正在解析视频信息")
        if yt_dlp is None:
            raise _DownloadSetupError(
                "内置 yt-dlp 下载核心不可用",
                category="格式/工具",
            )
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            if self._disk_lease is not None:
                ydl.params["match_filter"] = (
                    lambda info, *, incomplete=False: self._capacity_match_filter(
                        ydl,
                        info,
                        incomplete=incomplete,
                    )
                )
                ydl.add_post_hook(self._capacity_post_hook)
            self._log("info", "下载", "开始调用 yt-dlp")
            info = self._run_with_network_retry(
                lambda: ydl.extract_info(self.url, download=True),
                "视频下载",
            )
            if not isinstance(info, Mapping):
                raise RuntimeError("yt-dlp 下载完成后未返回媒体信息")
            self._complete_download_info(info, ydl.prepare_filename)

    def _cooperative_cancel_state(self) -> tuple[str, str] | None:
        if not self._cancel.is_set() or self._cancel_reason not in {
            "pause", "cancel", "delete", "discard", "shutdown",
        }:
            return None
        target_stage = (
            "paused" if self._cancel_reason in {"pause", "shutdown"} else "canceled"
        )
        target_text = "正在暂停任务" if target_stage == "paused" else "正在取消任务"
        return target_stage, target_text

    @staticmethod
    def _run_exception_message(exc: Exception) -> str:
        return str(exc).strip() or type(exc).__name__

    @staticmethod
    def _run_failure_details(
        exc: Exception,
    ) -> tuple[str, str, dict[str, Any]]:
        if isinstance(exc, _DownloadSetupError):
            return exc.category, exc.log_message, dict(exc.details)
        if isinstance(exc, MediaValidationError):
            return "文件/校验", "下载失败", {"validation": exc.as_dict()}
        if isinstance(exc, DiskCapacityError):
            return "磁盘/存储", "下载失败", {"capacity": exc.as_dict()}
        return (
            DownloadLogService.classify_error(str(exc)),
            "下载失败",
            {"error": DownloadWorker._run_exception_message(exc)},
        )

    def _handle_run_exception(self, exc: Exception) -> None:
        cancel_state = self._cooperative_cancel_state()
        if cancel_state is not None:
            target_stage, target_text = cancel_state
            self._set_stage(
                target_stage,
                target_text,
                force=True,
                cancel_reason=self._cancel_reason,
            )
            self._log("info", "用户操作", "下载工作线程已停止", reason=self._cancel_reason)
            return
        category, log_message, details = self._run_failure_details(exc)
        self._log("error", category, log_message, **details)
        message = self._run_exception_message(exc)
        self._set_stage(
            "failed",
            f"下载失败：{message[:120]}",
            self._stage_progress,
            force=True,
            error=message,
        )
        self.failed.emit(self.task_id, message)

    def _cleanup_warning(self, category: str, message: str, **details: Any) -> None:
        """Best-effort cleanup logging that can never break worker shutdown."""

        try:
            self._log("warning", category, message, **details)
        except Exception:
            pass

    def _discard_pending_transcodes_after_run(self) -> None:
        pending = tuple(self._pending_transcodes)
        self._pending_transcodes.clear()
        for prepared in pending:
            try:
                prepared.discard()
            except Exception as exc:
                self._cleanup_warning(
                    "文件/校验",
                    "清理未提交的转码文件失败",
                    error=str(exc),
                )

    def _cleanup_cookie_source(
        self,
        materialized_cookies: MaterializedCookieSource,
    ) -> None:
        temporary_file = materialized_cookies.temporary_file
        try:
            cleaned = materialized_cookies.cleanup()
        except Exception as exc:
            self._cleanup_warning(
                "风控/登录",
                "清理临时 Cookie 文件失败",
                path=str(temporary_file or ""),
                error=str(exc),
            )
            return
        if not cleaned:
            self._cleanup_warning(
                "风控/登录",
                "清理临时 Cookie 文件失败",
                path=str(temporary_file or ""),
            )

    def _release_disk_lease_after_run(self) -> None:
        if self._disk_lease is None:
            return
        try:
            released = self._disk_lease.release_all()
        except Exception as exc:
            self._cleanup_warning(
                "磁盘/存储",
                "释放磁盘容量预留失败",
                error=str(exc),
            )
            return
        if released:
            try:
                self._log(
                    "info",
                    "磁盘/存储",
                    "下载线程结束，已释放剩余磁盘容量预留",
                    released=released,
                )
            except Exception:
                pass

    def _should_cleanup_processing_workspace(self) -> bool:
        return (
            self._download_completed
            or not self._cancel.is_set()
            or self._cancel_reason not in {"pause", "shutdown"}
        )

    def _cleanup_processing_workspace_after_run(self) -> None:
        if not self._should_cleanup_processing_workspace():
            return
        try:
            cleaned = cleanup_processing_workspace(self._processing_workspace)
        except Exception as exc:
            self._cleanup_warning(
                "磁盘/存储",
                "清理临时处理目录失败",
                error=str(exc),
            )
            return
        if self._processing_workspace is not None and not cleaned:
            self._cleanup_warning(
                "磁盘/存储",
                "临时处理目录未能安全清理，已保留供检查",
                path=str(self._processing_workspace),
            )

    def _flush_task_logs_after_run(self) -> None:
        try:
            self.logs.flush(self.task_id)
        except Exception:
            # The log sink itself is unavailable, so another log record cannot
            # report this failure. Worker completion must still be delivered.
            pass

    def _cleanup_run(
        self,
        materialized_cookies: MaterializedCookieSource | None,
    ) -> None:
        self._discard_pending_transcodes_after_run()
        if materialized_cookies is not None:
            self._cleanup_cookie_source(materialized_cookies)
        self._release_disk_lease_after_run()
        self._cleanup_processing_workspace_after_run()
        self._flush_task_logs_after_run()

    def _start_run(self) -> None:
        self._started_at = time.monotonic()
        self._stage_started_at = self._started_at
        self._set_stage("parsing", "正在解析视频信息")
        self._log(
            "info",
            "任务",
            "开始处理下载任务",
            url=self.logs.redact_url(self.url),
            output_dir=self.output_dir,
            quality=self.quality,
            playlist_mode=self.playlist_mode,
            proxy=bool(self.proxy),
        )

    @Slot()
    def run(self) -> None:
        cookies: MaterializedCookieSource | None = None
        try:
            self._start_run()
            external_executable = self._resolve_download_core()
            self._prepare_run_environment()
            ydl_options = self._build_ytdlp_options()
            cookies = self._configure_cookie_options(ydl_options)
            self._run_download_flow(external_executable, ydl_options)
        except Exception as exc:
            self._handle_run_exception(exc)
        finally:
            try:
                self._cleanup_run(cookies)
            finally:
                self.finished.emit()

@dataclass(frozen=True, slots=True)
class _DownloadRuntime:
    """Fully wired but not-yet-started runtime for one download task."""

    thread: QThread
    worker: DownloadWorker
    disk_lease: DiskReservationLease


@dataclass(frozen=True, slots=True)
class _CompletedConversionRuntime:
    """Fully wired but not-yet-started runtime for a manual conversion."""

    thread: QThread
    worker: CompletedMediaTranscodeWorker
    disk_lease: DiskReservationLease


@dataclass(frozen=True, slots=True)
class _CompletedConversionRequest:
    """Validated inputs for a completed-task manual conversion."""

    task: DownloadTask
    encoder: str
    ffmpeg_path: str = ""
    ffprobe_path: str = ""


@dataclass(frozen=True, slots=True)
class _CompletedConversionCommit:
    """Normalized publication data ready for one atomic catalog commit."""

    task: DownloadTask
    publication: PublishedTranscode | None
    old_path: str
    new_path: str
    encoder: str
    codec: str
    device: str


@dataclass(slots=True)
class _PendingCollectionDelete:
    """Runtime-only intent and bounded file plan for one collection tree."""

    delete_files: bool = False
    cleanup_plans: dict[str, tuple[set[Path], Path]] = field(default_factory=dict)


@dataclass(slots=True)
class _CollectionCascadePlan:
    """One validated collection action before any durable mutation occurs."""

    action: str
    tree_tasks: tuple[DownloadTask, ...]
    transitions: list[tuple[str, DownloadTask]] = field(default_factory=list)
    worker_cancels: list[tuple[DownloadTask, Any, str]] = field(default_factory=list)
    conversion_cancels: list[tuple[DownloadTask, Any]] = field(default_factory=list)
    pending_retries: list[DownloadTask] = field(default_factory=list)
    pending_retry_discards: set[str] = field(default_factory=set)

    @property
    def mutated_tasks(self) -> tuple[DownloadTask, ...]:
        return tuple(task for _transition, task in self.transitions)

    def transition_ids(self, *kinds: str) -> list[str]:
        selected = set(kinds)
        return [
            task.id
            for transition, task in self.transitions
            if transition in selected
        ]


class DownloadService(QObject):
    task_added = Signal(object)
    tasks_added = Signal(object)
    task_updated = Signal(object)
    task_progress = Signal(str, dict)
    task_media_completed = Signal(str, object)
    task_finished = Signal(str, str, str)
    task_deleted = Signal(str)
    formats_ready = Signal(str, object)
    playlist_info = Signal(str, object)
    failed = Signal(str)
    conversion_finished = Signal(str, str, bool)
    conversion_failed = Signal(str, str)

    def __init__(self, db: Database, max_concurrent: int = 3, request_delay: float = 0.0,
                 fragment_concurrent: int = 12,
                 disk_capacity_manager: DiskReservationManager | None = None,
                 ytdlp_core_mode: str = "auto", deno_path: str = "", ffprobe_path: str = "",
                 ytdlp_ejs_source: str = "auto", cover_convert_jpeg: bool = False,
                 cover_jpeg_quality: int = 90):
        super().__init__()
        self.db = db
        self.tasks: dict[str, DownloadTask] = {}
        self._task_index = DownloadTaskIndex()
        self._collection_child_state: dict[str, _CollectionChildContribution] = {}
        self._collection_aggregates: dict[str, _CollectionAggregate] = {}
        self.queue = DownloadTaskQueue()
        (
            self.max_concurrent,
            self.fragment_concurrent,
            self.request_delay,
        ) = normalize_download_performance_values(
            max_concurrent,
            fragment_concurrent,
            request_delay,
        )
        self.ytdlp_core_mode = normalize_ytdlp_core_mode(ytdlp_core_mode)
        self.deno_path = str(deno_path or "").strip()
        self.ffprobe_path = str(ffprobe_path or "").strip()
        self.ytdlp_ejs_source = normalize_ytdlp_ejs_source(ytdlp_ejs_source)
        self.cover_convert_jpeg = bool(cover_convert_jpeg)
        self.cover_jpeg_quality = max(50, min(int(cover_jpeg_quality or 90), 100))
        self.active_task_id: str | None = None
        self.thread: QThread | None = None
        self.worker: DownloadWorker | None = None
        self.threads: dict[str, QThread] = {}
        self.workers: dict[str, DownloadWorker] = {}
        self._deferred_thread_finishes: set[QThread] = set()
        self.conversion_threads: dict[str, QThread] = {}
        self.conversion_workers: dict[str, CompletedMediaTranscodeWorker] = {}
        self._conversion_disk_leases: dict[str, DiskReservationLease] = {}
        self._deferred_conversion_finishes: set[QThread] = set()
        self._discard_tasks: set[str] = set()
        self._pending_deletes: dict[str, bool] = {}
        self._pending_runtime_retries: set[str] = set()
        self._pending_collection_deletes: dict[str, _PendingCollectionDelete] = {}
        self._collection_delete_root_by_child: dict[str, str] = {}
        self._progress_persistence = ProgressPersistenceBuffer()
        self._last_progress_emit: dict[str, float] = {}
        self._resolved_identity_state: dict[str, tuple[str, str, str]] = {}
        # One manager is shared by every worker so parallel downloads on the
        # same physical volume cannot each assume they own the full free space.
        self.disk_capacity_manager = disk_capacity_manager or DiskReservationManager()
        self._disk_leases: dict[str, DiskReservationLease] = {}
        self._progress_flush_timer = QTimer(self)
        self._progress_flush_timer.setSingleShot(True)
        self._progress_flush_timer.setInterval(900)
        self._progress_flush_timer.timeout.connect(self._flush_progress_persists)
        self._pending_collection_refreshes: set[str] = set()
        self._collection_refresh_timer = QTimer(self)
        self._collection_refresh_timer.setSingleShot(True)
        self._collection_refresh_timer.setInterval(250)
        self._collection_refresh_timer.timeout.connect(self._flush_collection_refreshes)
        self._materialization_parents: deque[str] = deque()
        self._materialization_timer = QTimer(self)
        self._materialization_timer.setSingleShot(True)
        self._materialization_timer.timeout.connect(self._process_collection_materialization_batch)
        self._deferred_restore_rows: deque[Any] = deque()
        self._restore_latest_media: dict[str, MediaItem] = {}
        self._restore_refresh_parents: set[str] = set()
        self._restore_hierarchy: dict[str, _RestoredTaskHierarchy] = {}
        self._restore_timer = QTimer(self)
        self._restore_timer.setSingleShot(True)
        self._restore_timer.timeout.connect(self._process_restore_batch)
        self._shutting_down = False
        self.logs = DownloadLogService()

    def configure_performance(
        self,
        *,
        max_concurrent: object,
        fragment_concurrent: object,
        request_delay: object,
    ) -> tuple[int, int, float]:
        """Apply validated runtime limits and fill newly available slots."""
        values = normalize_download_performance_values(
            max_concurrent,
            fragment_concurrent,
            request_delay,
        )
        self.max_concurrent, self.fragment_concurrent, self.request_delay = values
        self._start_next()
        return values

    @property
    def active_thread_count(self) -> int:
        threads = set(self.threads.values()) | set(self.conversion_threads.values())
        return sum(1 for thread in threads if thread.isRunning())

    def total_speed_bps(self) -> float:
        """Return the incrementally maintained aggregate download speed."""
        return self._task_index.total_speed_bps

    @staticmethod
    def _task_speed_contribution(task: DownloadTask) -> float:
        # Collection parents already aggregate child speed for their own card.
        # Counting them here would double the status-bar total.
        if task.task_kind == "collection" or task.status not in {"downloading", "canceling"}:
            return 0.0
        return non_negative_float(task.speed_bps)

    @staticmethod
    def _collection_child_contribution(
        task: DownloadTask,
    ) -> _CollectionChildContribution | None:
        return collection_child_contribution(
            parent_id=task.parent_task_id,
            status=task.status,
            speed_bps=task.speed_bps,
            total_bytes=task.total_bytes,
            downloaded_bytes=task.downloaded_bytes,
            progress=task.progress,
        )

    def _apply_collection_contribution(
        self,
        contribution: _CollectionChildContribution,
        direction: int,
    ) -> None:
        parent_id = contribution.parent_id
        aggregate = self._collection_aggregates.get(parent_id)
        if aggregate is None:
            if direction < 0:
                return
            aggregate = _CollectionAggregate()
            self._collection_aggregates[parent_id] = aggregate
        aggregate.apply(contribution, direction)
        if aggregate.child_count <= 0:
            self._collection_aggregates.pop(parent_id, None)

    def _sync_collection_child_index(self, task: DownloadTask) -> None:
        task_id = str(task.id)
        previous = self._collection_child_state.get(task_id)
        current = self._collection_child_contribution(task)
        if previous == current:
            return
        if previous is not None:
            self._apply_collection_contribution(previous, -1)
        if current is None:
            self._collection_child_state.pop(task_id, None)
            return
        self._apply_collection_contribution(current, 1)
        self._collection_child_state[task_id] = current

    def _remove_collection_child_index(self, task_id: str) -> None:
        previous = self._collection_child_state.pop(str(task_id), None)
        if previous is not None:
            self._apply_collection_contribution(previous, -1)

    def _sync_task_indexes(self, task: DownloadTask) -> None:
        """Update status, speed and parent-child indexes in constant time."""
        self._sync_collection_child_index(task)
        self._task_index.sync(
            task.id,
            parent_id=task.parent_task_id,
            status=task.status,
            speed_bps=self._task_speed_contribution(task),
        )

    def _register_task(self, task: DownloadTask) -> None:
        self.tasks[task.id] = task
        self._sync_task_indexes(task)

    def _remove_task_indexes(self, task_id: str) -> None:
        self._remove_collection_child_index(task_id)
        self._task_index.remove(task_id)

    def _unregister_task(self, task_id: str) -> DownloadTask | None:
        task = self.tasks.pop(task_id, None)
        self._remove_task_indexes(task_id)
        self._resolved_identity_state.pop(str(task_id), None)
        return task

    def reset_task_cache(self) -> None:
        """Clear only in-memory task state after an external database reset."""
        # Stop every timer whose callback consumes task-derived queues before
        # clearing them. A timeout event may already be queued, so the queues
        # are also emptied to make such a callback harmless.
        self._progress_flush_timer.stop()
        self._collection_refresh_timer.stop()
        self._materialization_timer.stop()
        self._restore_timer.stop()
        self._progress_persistence.clear()
        self._pending_collection_refreshes.clear()
        self._materialization_parents.clear()
        self._deferred_restore_rows.clear()
        self._restore_refresh_parents.clear()
        self._restore_latest_media.clear()
        self._restore_hierarchy.clear()
        self._discard_tasks.clear()
        self._pending_deletes.clear()
        self._pending_runtime_retries.clear()
        self._pending_collection_deletes.clear()
        self._collection_delete_root_by_child.clear()
        self._deferred_thread_finishes.clear()
        self.tasks.clear()
        self.queue.clear()
        self._task_index.clear()
        self._collection_child_state.clear()
        self._collection_aggregates.clear()
        self._last_progress_emit.clear()
        self._resolved_identity_state.clear()

    def task_statistics(self, *, top_level_only: bool = False) -> dict[str, int]:
        """Return cached task totals without walking every task object."""
        return self._task_index.statistics(top_level_only=top_level_only)

    def request_shutdown(self) -> None:
        """Cooperatively stop active downloads without blocking the GUI thread."""
        if self._shutting_down:
            self._flush_progress_persists()
            return
        self._shutting_down = True
        self._progress_flush_timer.stop()
        self._flush_progress_persists()
        self._collection_refresh_timer.stop()
        self._flush_collection_refreshes()
        self._materialization_timer.stop()
        for task_id, worker in list(self.workers.items()):
            task = self.tasks.get(task_id)
            if task:
                task.pause_requested = True
                task.cancel_requested = False
                task.status = "暂停中"
                try:
                    self._persist(task)
                except Exception as exc:
                    self._sync_task_indexes(task)
                    self._best_effort_service_log(
                        task_id,
                        "warning",
                        "数据库/持久化",
                        "退出时保存暂停状态失败，仍将继续停止后台任务",
                        error=str(exc),
                    )
                self.task_updated.emit(task)
            try:
                worker.cancel("shutdown")
            except RuntimeError:
                pass
        for thread in set(self.threads.values()):
            if thread.isRunning():
                thread.requestInterruption()
                thread.quit()
        for task_id, worker in list(self.conversion_workers.items()):
            try:
                worker.cancel()
            except RuntimeError:
                pass
            task = self.tasks.get(task_id)
            if task and task.status == "processing":
                task.stage_text = "正在取消格式转换"
                self.task_updated.emit(task)
        for thread in set(self.conversion_threads.values()):
            if thread.isRunning():
                thread.requestInterruption()
                thread.quit()
        try:
            self.logs.flush()
        except Exception:
            pass

    def shutdown(self, timeout_ms: int = 4000) -> bool:
        """Request shutdown, optionally wait, and report whether all workers stopped."""
        self.request_shutdown()
        threads = list(set(self.threads.values()) | set(self.conversion_threads.values()))
        if timeout_ms > 0 and threads:
            deadline = time.monotonic() + timeout_ms / 1000.0
            for thread in threads:
                remaining = max(0, int((deadline - time.monotonic()) * 1000))
                if thread.isRunning() and remaining > 0:
                    thread.wait(remaining)
        # Retry a transient failure from the first shutdown flush after worker
        # state writes and thread cleanup had an opportunity to release SQLite.
        self._flush_progress_persists()
        # A QThread may already be stopped while its queued ``finished`` slot
        # has not yet saved the final task state and removed its references.
        # MainWindow must keep SQLite open until that cleanup has run.
        return (
            not self.threads
            and not self.workers
            and not self.conversion_threads
            and not self.conversion_workers
            and not self._conversion_disk_leases
        )

    @staticmethod
    def _apply_restored_presentation(task: DownloadTask) -> None:
        if task.status != "downloading":
            task.speed_samples.clear()
            task.speed_bps = 0.0
            task.speed = ""
            task.eta = ""
        if task.status == "completed":
            task.stage, task.stage_text, task.stage_progress = (
                "completed",
                "下载完成",
                100.0,
            )
        elif task.status == "failed":
            task.stage, task.stage_text = "failed", "下载失败"
        elif task.status == "paused":
            task.stage, task.stage_text = "paused", "已暂停"
        elif task.status == "canceled":
            task.stage, task.stage_text = "canceled", "已取消"

    @staticmethod
    def _media_file_exists(path: str) -> bool:
        if not path:
            return False
        try:
            return Path(path).is_file()
        except OSError:
            return False

    @staticmethod
    def _restored_task_from_reader(
        reader: _TaskRowReader,
        status: str,
    ) -> DownloadTask:
        download_album = reader.boolean("download_album")
        return DownloadTask(
            id=reader.text("id"),
            url=reader.text("url"),
            output_dir=reader.text("output_dir"),
            quality=reader.text("quality", "best") or "best",
            task_kind=reader.text("task_kind", "video") or "video",
            parent_task_id=reader.text("parent_task_id"),
            root_task_id=reader.text("root_task_id"),
            source_key=reader.text("source_key"),
            collection_index=reader.integer("collection_index"),
            options_json=reader.options(),
            download_album=download_album,
            playlist_mode=(
                reader.text("playlist_mode")
                or ("playlist" if download_album else "single")
            ),
            proxy=reader.text("proxy"),
            cookie_file=reader.text("cookie_file"),
            cookie_source=reader.text("cookie_source", "none") or "none",
            cookie_browser=reader.text("cookie_browser", "chrome") or "chrome",
            cookie_profile=reader.text("cookie_profile"),
            cookie_keyring=reader.text("cookie_keyring"),
            cookie_container=reader.text("cookie_container"),
            filename_template=(
                reader.text("filename_template")
                or "%(title)s [%(id)s].%(ext)s"
            ),
            ffmpeg_path=reader.text("ffmpeg_path"),
            format_selector=reader.text("format_selector"),
            transcode_codec=reader.text("transcode_codec", "original") or "original",
            transcode_device=reader.text("transcode_device", "auto") or "auto",
            transcode_encoder=reader.text("transcode_encoder"),
            subtitle_language=reader.text("subtitle_language", "none") or "none",
            title=reader.text("title", "等待获取视频信息") or "等待获取视频信息",
            status=status,
            progress=reader.floating("progress", maximum=100.0),
            speed=reader.text("speed"),
            speed_bps=reader.floating("speed_bps"),
            downloaded_bytes=reader.integer("downloaded_bytes"),
            total_bytes=reader.integer("total_bytes"),
            eta=reader.text("eta"),
            size=reader.text("size"),
            error=reader.text("error"),
            media_path=reader.text("media_path"),
            thumbnail_path=reader.text("thumbnail_path"),
            created_at=(
                reader.text("created_at")
                or datetime.now().isoformat(timespec="seconds")
            ),
        )

    def _restore_media_reference(self, task: DownloadTask) -> bool:
        if task.task_kind != "video" or task.status not in {"completed", "deleted"}:
            return False
        previous = (task.status, task.media_path, task.thumbnail_path)
        if not task.media_path:
            media = self._restore_latest_media.get(task.url)
            if media:
                task.media_path = media.video_path
                task.thumbnail_path = media.thumbnail_path
        task.status = "completed" if self._media_file_exists(task.media_path) else "deleted"
        return previous != (task.status, task.media_path, task.thumbnail_path)

    @staticmethod
    def _append_restore_message(existing: str, message: str) -> str:
        values = [value for value in (str(existing or "").strip(), message.strip()) if value]
        return "\n".join(dict.fromkeys(values))

    def _repair_restored_hierarchy(self, task: DownloadTask) -> bool:
        desired = self._restore_hierarchy.get(task.id)
        if desired is None:
            return False
        changed = (
            task.parent_task_id != desired.parent_task_id
            or task.root_task_id != desired.root_task_id
        )
        if not changed:
            return False

        task.parent_task_id = desired.parent_task_id
        task.root_task_id = desired.root_task_id
        if desired.invalid_reason:
            message = f"任务层级记录无效，已提升为顶层任务：{desired.invalid_reason}"
            if task.status in {"completed", "deleted"}:
                task.completion_warning = self._append_restore_message(
                    task.completion_warning,
                    message,
                )
            else:
                task.status = "failed"
                task.error = self._append_restore_message(task.error, message)
            self._best_effort_service_log(
                task.id,
                "warning",
                "数据库/恢复",
                "任务层级记录无效，已阻止隐藏子任务继续运行",
                reason=desired.invalid_reason,
            )
        else:
            self._best_effort_service_log(
                task.id,
                "warning",
                "数据库/恢复",
                "任务根层级引用不一致，已按父合集关系修复",
            )
        return True

    def _repair_restored_fields(
        self,
        task: DownloadTask,
        issues: list[str],
    ) -> bool:
        if not issues:
            return False
        repair_message = (
            "任务记录部分字段损坏，已使用安全默认值："
            + "、".join(issues)
        )
        if task.status in {"completed", "deleted"}:
            task.completion_warning = self._append_restore_message(
                task.completion_warning,
                repair_message,
            )
        else:
            task.status = "failed"
            task.error = self._append_restore_message(task.error, repair_message)
        self._best_effort_service_log(
            task.id,
            "warning",
            "数据库/恢复",
            "任务记录包含无效字段，已使用安全默认值恢复",
            fields=issues,
        )
        return True

    def _publish_restored_task(self, task: DownloadTask, changed: bool) -> None:
        self._apply_restored_presentation(task)
        self._register_task(task)
        if changed:
            try:
                self._persist(task)
            except Exception as exc:
                # A damaged row must not prevent every healthy task from
                # appearing. Keep the repaired in-memory state conservative
                # and retry persistence through a later user/task transition.
                self._best_effort_service_log(
                    task.id,
                    "warning",
                    "数据库/恢复",
                    "保存任务自愈结果失败，已继续恢复其他任务",
                    error=str(exc),
                )
        if task.status == "queued" and task.task_kind == "video":
            self.queue.append_unique(task.id)

    def _restore_task_row(self, row: Any) -> DownloadTask:
        reader = _TaskRowReader(row)
        original_status, status = restored_status(
            reader,
            DOWNLOAD_RESTORABLE_STATUSES,
        )
        task = self._restored_task_from_reader(reader, status)
        hierarchy_changed = self._repair_restored_hierarchy(task)
        media_changed = self._restore_media_reference(task)
        repair_changed = self._repair_restored_fields(task, reader.issues)
        self._publish_restored_task(
            task,
            status != original_status
            or hierarchy_changed
            or media_changed
            or repair_changed,
        )
        return task

    def _resume_collection_materialization_after_restore(self) -> None:
        if self._materialization_parents and not self._materialization_timer.isActive():
            self._materialization_timer.start(0)

    def _process_restore_batch(self) -> None:
        batch: list[DownloadTask] = []
        for _ in range(RESTORE_BATCH_SIZE):
            if not self._deferred_restore_rows:
                break
            task = self._restore_task_row(self._deferred_restore_rows.popleft())
            batch.append(task)
            if task.parent_task_id:
                self._restore_refresh_parents.add(task.parent_task_id)
        if batch:
            self.tasks_added.emit(batch)
        if self._deferred_restore_rows:
            self._restore_timer.start(0)
            return
        for parent_id in tuple(self._restore_refresh_parents):
            self._refresh_collection(parent_id)
        self._restore_refresh_parents.clear()
        self._restore_latest_media.clear()
        self._restore_hierarchy.clear()
        self._resume_collection_materialization_after_restore()

    def restore_tasks(self) -> list[DownloadTask]:
        rows = self.db.list_download_tasks()
        plan = build_task_restore_plan(
            rows,
            initial_terminal_children=RESTORE_INITIAL_TERMINAL_CHILDREN,
        )
        self._restore_latest_media = self.db.latest_media_by_source_urls(
            plan.missing_media_urls,
        )
        self._deferred_restore_rows.clear()
        self._deferred_restore_rows.extend(plan.deferred_rows)
        self._restore_refresh_parents.clear()
        self._materialization_parents.clear()
        self._restore_hierarchy = plan.hierarchy

        restored = [self._restore_task_row(row) for row in plan.immediate_rows]
        for task in restored:
            if task.task_kind == "collection":
                if task.id not in plan.deferred_parent_ids and self.collection_children(task.id):
                    self._refresh_collection(task.id)
                materialization = task.options_json.get("_collection_materialization", {})
                if isinstance(materialization, Mapping) and materialization.get("active"):
                    self._materialization_parents.append(task.id)

        # Queued and interrupted work is restored synchronously so downloads
        # can resume immediately. Old terminal collection children continue in
        # small event-loop batches after the main window has painted.
        self._start_next()
        if self._deferred_restore_rows:
            self._restore_timer.start(0)
        else:
            self._restore_latest_media.clear()
            self._restore_hierarchy.clear()
            self._resume_collection_materialization_after_restore()
        return restored

    def _persist(self, task: DownloadTask) -> None:
        # The database is authoritative. Keep cached counters on their last
        # durable state if SQLite rejects this transition, and never recreate
        # a row that was already deleted by another task lifecycle path.
        self.db.update_download_task(task)
        self._sync_task_indexes(task)
        self._progress_persistence.mark_persisted((task,), time.monotonic())

    @staticmethod
    def _snapshot_task_state(task: DownloadTask) -> dict[str, Any]:
        return deepcopy(task.__dict__)

    def _restore_task_state(
        self,
        task: DownloadTask,
        snapshot: Mapping[str, Any],
    ) -> None:
        task.__dict__.clear()
        task.__dict__.update(snapshot)
        self._sync_task_indexes(task)

    @contextmanager
    def _durable_task_mutation(self, task: DownloadTask) -> Iterator[None]:
        """Persist one task mutation atomically from the service's perspective.

        SQLite remains authoritative for user-triggered state changes. If the
        write fails, restore every in-memory field (including mutable option
        dictionaries and speed samples) before propagating the error. Queue and
        worker side effects must be performed only after this context succeeds.
        """

        snapshot = self._snapshot_task_state(task)
        try:
            yield
            self._persist(task)
        except BaseException:
            self._restore_task_state(task, snapshot)
            raise

    @contextmanager
    def _durable_task_mutations(
        self,
        tasks: Iterable[DownloadTask],
    ) -> Iterator[None]:
        """Persist a group of task transitions as one SQLite transaction."""

        unique_tasks = tuple({task.id: task for task in tasks}.values())
        if not unique_tasks:
            yield
            return
        snapshots = {
            task.id: self._snapshot_task_state(task)
            for task in unique_tasks
        }
        try:
            yield
            self.db.update_download_tasks(unique_tasks)
        except BaseException:
            for task in unique_tasks:
                self._restore_task_state(task, snapshots[task.id])
            raise

        persisted_at = time.monotonic()
        for task in unique_tasks:
            self._sync_task_indexes(task)
        self._progress_persistence.mark_persisted(unique_tasks, persisted_at)

    def _persist_progress(self, task: DownloadTask, force: bool = False) -> bool:
        self._sync_task_indexes(task)
        if self._shutting_down and not force:
            return False
        now = time.monotonic()
        if self._progress_persistence.should_write_immediately(
            task.id,
            force=force,
        ):
            try:
                self.db.update_download_task(task)
            except LookupError as exc:
                # A late queued progress callback must never recreate a task
                # that has already been deleted from the authoritative DB.
                self._progress_persistence.forget(task.id)
                self._best_effort_service_log(
                    task.id,
                    "warning",
                    "数据库/持久化",
                    "任务进度记录已不存在，已丢弃迟到更新",
                    error=str(exc),
                )
                return False
            except Exception as exc:
                self._progress_persistence.enqueue(task)
                self._report_progress_persist_failure((task,), exc)
                self._schedule_progress_persist_retry()
            else:
                self._progress_persistence.mark_persisted((task,), now)
            return True
        self._progress_persistence.enqueue(task)
        self._schedule_progress_persist_retry()
        return True

    def _schedule_progress_persist_retry(self) -> None:
        if self._shutting_down or self._progress_flush_timer.isActive():
            return
        self._progress_flush_timer.start()

    def _report_progress_persist_failure(
        self,
        tasks: Iterable[DownloadTask],
        error: Exception,
    ) -> None:
        batch = tuple(tasks)
        if not batch:
            return
        now = time.monotonic()
        if not self._progress_persistence.should_report_error(now):
            return
        self._best_effort_service_log(
            batch[0].id,
            "warning",
            "数据库/持久化",
            "批量保存任务进度失败，已保留数据等待重试",
            error=str(error),
            task_count=len(batch),
        )

    def _retry_progress_batch_without_missing_rows(
        self,
        tasks: tuple[DownloadTask, ...],
    ) -> tuple[tuple[DownloadTask, ...], tuple[str, ...]]:
        existing_ids = self.db.existing_download_task_ids(
            task.id for task in tasks
        )
        retained: list[DownloadTask] = []
        missing: list[str] = []
        for task in tasks:
            if task.id in existing_ids:
                retained.append(task)
            else:
                missing.append(task.id)
                self._progress_persistence.forget(task.id)
        return tuple(retained), tuple(missing)

    def _commit_progress_batch(
        self,
        tasks: tuple[DownloadTask, ...],
    ) -> bool:
        if not tasks:
            return True
        try:
            self.db.update_download_tasks(tasks)
        except LookupError as exc:
            try:
                retained, missing = self._retry_progress_batch_without_missing_rows(
                    tasks,
                )
                if retained == tasks:
                    raise exc
                if retained:
                    self.db.update_download_tasks(retained)
            except Exception as retry_error:
                self._report_progress_persist_failure(tasks, retry_error)
                return False
            if missing:
                self._best_effort_service_log(
                    missing[0],
                    "warning",
                    "数据库/持久化",
                    "批量进度中包含已删除任务，已隔离迟到更新",
                    missing_task_count=len(missing),
                )
            tasks = retained
        except Exception as exc:
            self._report_progress_persist_failure(tasks, exc)
            return False
        self._progress_persistence.mark_persisted(tasks, time.monotonic())
        return True

    def _flush_progress_persists(self) -> None:
        tasks = self._progress_persistence.batch()
        if not tasks:
            return
        if getattr(self.db, "conn", None) is None:
            self._progress_persistence.pending.clear()
            return
        if not self._commit_progress_batch(tasks):
            self._schedule_progress_persist_retry()
            return

    @staticmethod
    def _snapshot_new_task_options(
        options_json: Mapping[str, Any] | None,
        *,
        organize_task_folder: bool | None,
        prepend_cover_enabled: bool | None,
        prepend_cover_frames: int | None,
    ) -> dict[str, Any]:
        options = dict(options_json or {})
        if organize_task_folder is not None:
            options["organize_task_folder"] = bool(organize_task_folder)
        if prepend_cover_enabled is not None:
            options["prepend_cover_enabled"] = bool(prepend_cover_enabled)
        if prepend_cover_frames is not None:
            options["prepend_cover_frames"] = int(prepend_cover_frames)
        return options

    def _build_new_download_task(
        self,
        url: str,
        output_dir: str,
        *,
        proxy: str = "",
        cookie_file: str = "",
        quality: str = "best",
        filename_template: str = "%(title)s [%(id)s].%(ext)s",
        ffmpeg_path: str = "",
        format_selector: str = "",
        download_album: bool = False,
        playlist_mode: str = "auto",
        cookie_source: str = "none",
        cookie_browser: str = "chrome",
        cookie_profile: str = "",
        cookie_keyring: str = "",
        cookie_container: str = "",
        transcode_codec: str = "original",
        transcode_device: str = "auto",
        subtitle_language: str = "none",
        transcode_encoder: str = "",
        task_kind: str = "video",
        parent_task_id: str = "",
        root_task_id: str = "",
        source_key: str = "",
        collection_index: int = 0,
        options_json: Mapping[str, Any] | None = None,
        organize_task_folder: bool | None = None,
        prepend_cover_enabled: bool | None = None,
        prepend_cover_frames: int | None = None,
    ) -> DownloadTask:
        return DownloadTask(
            id=self._new_task_id(),
            url=str(url or "").strip(),
            output_dir=str(output_dir or ""),
            proxy=str(proxy or ""),
            task_kind=task_kind,
            parent_task_id=parent_task_id,
            root_task_id=root_task_id,
            source_key=source_key,
            collection_index=collection_index,
            options_json=self._snapshot_new_task_options(
                options_json,
                organize_task_folder=organize_task_folder,
                prepend_cover_enabled=prepend_cover_enabled,
                prepend_cover_frames=prepend_cover_frames,
            ),
            cookie_file=str(cookie_file or "").strip(),
            cookie_source=str(cookie_source or "none").strip(),
            cookie_browser=str(cookie_browser or "chrome").strip(),
            cookie_profile=str(cookie_profile or "").strip(),
            cookie_keyring=str(cookie_keyring or "").strip(),
            cookie_container=str(cookie_container or "").strip(),
            quality=quality,
            download_album=download_album,
            playlist_mode=playlist_mode,
            filename_template=validate_filename_template(filename_template),
            ffmpeg_path=ffmpeg_path,
            format_selector=format_selector,
            transcode_codec=normalize_transcode_codec(transcode_codec),
            transcode_device=normalize_transcode_device(transcode_device),
            transcode_encoder=(
                normalize_transcode_encoder(transcode_encoder)
                if str(transcode_encoder or "").strip() else ""
            ),
            subtitle_language=normalize_subtitle_language(subtitle_language),
        )

    def _commit_new_task(self, plan: _NewDownloadTaskPlan) -> None:
        """Persist a new task before exposing it to any in-memory consumer."""

        task = plan.task
        self.db.insert_download_task(task)
        try:
            self._register_task(task)
        except Exception as register_error:
            # A registration error must not leave a durable row that the UI
            # never saw. Best-effort index cleanup also covers a partially
            # completed _register_task implementation.
            try:
                self._unregister_task(task.id)
            except Exception:
                pass
            rollback_error: Exception | None = None
            try:
                self.db.delete_download_task(task.id)
            except Exception as exc:
                rollback_error = exc
            if rollback_error is not None:
                raise RuntimeError(
                    "下载任务写入成功，但内存注册和数据库回滚均失败："
                    f"{rollback_error}"
                ) from register_error
            raise
        self._progress_persistence.mark_persisted((task,), time.monotonic())

    def _try_write_new_task_log(self, task: DownloadTask, message: str) -> None:
        try:
            self.logs.clear(task.id)
            self.logs.write(
                task.id,
                "info",
                "任务",
                message,
                url=self.logs.redact_url(task.url),
                output_dir=task.output_dir,
            )
        except Exception:
            # Logging is diagnostic. A filesystem or encoding failure must
            # not make the caller retry a task that is already durable.
            pass

    def _try_log_task_publication_error(
        self,
        task: DownloadTask,
        operation: str,
        error: Exception,
    ) -> None:
        self._best_effort_service_log(
            task.id,
            "warning",
            "任务/发布",
            operation,
            error=str(error),
        )

    def _publish_new_task(self, plan: _NewDownloadTaskPlan) -> None:
        """Publish a committed task without turning side-effect failures into duplicates."""

        task = plan.task
        self._try_write_new_task_log(task, plan.log_message)
        if plan.queue_for_download:
            self.queue.append_unique(task.id)
        try:
            self.task_added.emit(task)
        except Exception as exc:
            self._try_log_task_publication_error(
                task,
                "通知界面新增任务失败；任务记录仍已保存",
                exc,
            )
        if not plan.queue_for_download:
            return
        try:
            self._start_next()
        except Exception as exc:
            if (
                task.status == "queued"
                and task.id not in self.workers
            ):
                self.queue.requeue_front(task.id)
            self._try_log_task_publication_error(
                task,
                "启动下载调度失败；任务已保留在队列中",
                exc,
            )

    def _create_new_task(self, plan: _NewDownloadTaskPlan) -> str:
        self._commit_new_task(plan)
        self._publish_new_task(plan)
        return plan.task.id

    def enqueue(self, url: str, output_dir: str, proxy: str = "", cookie_file: str = "",
               quality: str = "best", filename_template: str = "%(title)s [%(id)s].%(ext)s",
               ffmpeg_path: str = "", format_selector: str = "", download_album: bool = False,
                playlist_mode: str = "auto", cookie_source: str = "none", cookie_browser: str = "chrome",
                cookie_profile: str = "", cookie_keyring: str = "", cookie_container: str = "",
                transcode_codec: str = "original", transcode_device: str = "auto",
                subtitle_language: str = "none", transcode_encoder: str = "",
                task_kind: str = "video", parent_task_id: str = "", root_task_id: str = "",
                source_key: str = "", collection_index: int = 0,
                options_json: Mapping[str, Any] | None = None,
                organize_task_folder: bool | None = None,
                prepend_cover_enabled: bool | None = None,
                prepend_cover_frames: int | None = None,
                start_immediately: bool = True) -> str:
        if self._shutting_down:
            raise RuntimeError("程序正在退出，不能再添加下载任务")
        task = self._build_new_download_task(
            url,
            output_dir,
            proxy=proxy,
            cookie_file=cookie_file,
            quality=quality,
            filename_template=filename_template,
            ffmpeg_path=ffmpeg_path,
            format_selector=format_selector,
            download_album=download_album,
            playlist_mode=playlist_mode,
            cookie_source=cookie_source,
            cookie_browser=cookie_browser,
            cookie_profile=cookie_profile,
            cookie_keyring=cookie_keyring,
            cookie_container=cookie_container,
            transcode_codec=transcode_codec,
            transcode_device=transcode_device,
            subtitle_language=subtitle_language,
            transcode_encoder=transcode_encoder,
            task_kind=task_kind,
            parent_task_id=parent_task_id,
            root_task_id=root_task_id,
            source_key=source_key,
            collection_index=collection_index,
            options_json=options_json,
            organize_task_folder=organize_task_folder,
            prepend_cover_enabled=prepend_cover_enabled,
            prepend_cover_frames=prepend_cover_frames,
        )
        queue_for_download = task.task_kind == "video" and bool(start_immediately)
        return self._create_new_task(_NewDownloadTaskPlan(
            task=task,
            queue_for_download=queue_for_download,
            log_message=(
                "任务已加入下载队列" if queue_for_download else "任务已创建"
            ),
        ))

    def create_collection(
        self,
        url: str,
        output_dir: str,
        *,
        title: str = "",
        source_key: str = "",
        options_json: Mapping[str, Any] | None = None,
        parent_task_id: str = "",
        root_task_id: str = "",
        collection_index: int = 0,
        **task_options: Any,
    ) -> str:
        """Create a durable collection parent without consuming a download slot."""

        if self._shutting_down:
            raise RuntimeError("程序正在退出，不能再添加下载任务")
        parent = self._build_new_download_task(
            url,
            output_dir,
            task_kind="collection",
            parent_task_id=parent_task_id,
            root_task_id=root_task_id,
            source_key=source_key,
            collection_index=collection_index,
            options_json=options_json,
            **task_options,
        )
        parent.root_task_id = root_task_id or parent.id
        parent.title = title or "正在解析合集"
        parent.status = "parsing_collection"
        parent.stage = "parsing_collection"
        parent.stage_text = "正在解析合集"
        parent.options_json.setdefault("_collection", {})
        parent_id = self._create_new_task(_NewDownloadTaskPlan(
            task=parent,
            queue_for_download=False,
            log_message="合集任务已创建，正在解析",
        ))
        if parent.parent_task_id:
            self._refresh_collection(parent.parent_task_id)
        return parent_id

    def update_collection_probe(
        self,
        parent_id: str,
        *,
        title: str = "",
        source_key: str = "",
        parsed_count: int | None = None,
        finished: bool = False,
    ) -> None:
        parent = self.tasks.get(parent_id)
        if not parent or parent.task_kind != "collection":
            return
        if title:
            parent.title = title
        if source_key:
            parent.source_key = normalize_source_key(source_key)
        metadata = parent.options_json.setdefault("_collection", {})
        if parsed_count is not None:
            metadata["parsed"] = max(0, int(parsed_count))
        metadata["probe_finished"] = bool(finished)
        parent.status = "waiting_selection" if finished else "parsing_collection"
        parent.stage = parent.status
        parent.stage_text = "等待选择下载项目" if finished else "正在解析合集"
        self._persist(parent)
        self.task_updated.emit(parent)

    def fail_collection_probe(self, parent_id: str, error: str) -> bool:
        """Persist a collection-probe failure through the service boundary."""

        parent = self.tasks.get(parent_id)
        if not parent or parent.task_kind != "collection":
            return False
        message = str(error or "合集解析失败")
        parent.status = "failed"
        parent.error = message
        parent.stage = "failed"
        parent.stage_text = message
        parent.stage_progress = 0.0
        self._persist(parent)
        self.task_updated.emit(parent)
        if parent.parent_task_id:
            self._refresh_collection(parent.parent_task_id)
        return True

    def resolve_collection_as_video(
        self,
        task_id: str,
        *,
        title: str = "",
        source_key: str = "",
    ) -> bool:
        """Reuse an immediately visible collection placeholder as a video task.

        Auto-detection starts with a collection-shaped parsing task so the UI
        can acknowledge the click before network probing finishes. If yt-dlp
        reports a single video, keep the same durable task/card and enqueue it
        as a normal video instead of deleting and recreating the row.
        """

        task = self.tasks.get(task_id)
        if not task or task.task_kind != "collection" or self.collection_children(task_id):
            return False
        resolved_title = str(title or task.title or "等待获取视频信息")
        duplicate_id = self.find_active_duplicate(
            task.url,
            task.output_dir,
            task.quality,
            "single",
            exclude_task_id=task.id,
            source_key=source_key,
            title=resolved_title,
        )
        if duplicate_id:
            # Smart parsing may begin from different aliases that resolve to
            # one platform video. Remove only this temporary placeholder; the
            # already queued/running canonical task remains untouched.
            self.discard_task(task.id)
            return True
        task.task_kind = "video"
        task.playlist_mode = "single"
        task.download_album = False
        task.title = resolved_title
        if source_key:
            task.source_key = normalize_source_key(source_key)
        task.status = "queued"
        task.stage = "queued"
        task.stage_text = "解析完成，等待下载"
        task.stage_progress = 0.0
        task.progress = 0.0
        task.error = ""
        self._persist(task)
        self.queue.append_unique(task.id)
        self._best_effort_service_log(
            task.id,
            "info",
            "解析",
            "链接已识别为单个视频，继续下载",
        )
        self.task_updated.emit(task)
        self._start_next()
        return True

    def _new_task_id(self, reserved_ids: set[str] | None = None) -> str:
        """Return a compact UUID that cannot replace a known in-memory task."""

        reserved = reserved_ids if reserved_ids is not None else set()
        for _attempt in range(16):
            # Sixty-four random bits keep collision risk negligible without
            # needlessly lengthening Windows processing and log paths.
            task_id = uuid4().hex[:16]
            if task_id in self.tasks or task_id in reserved:
                continue
            reserved.add(task_id)
            return task_id
        raise RuntimeError("无法生成唯一的下载任务编号")

    def _collection_materialization_identity_index(
        self,
        existing_children: list[DownloadTask],
    ) -> _MediaIdentityIndex:
        identities = _MediaIdentityIndex()
        for child in existing_children:
            identities.add(child.source_key, child.url, child.title)
        # Include unfinished video tasks from every parent. The same video can
        # appear in multiple playlists or channels; materializing a second
        # child must not start another simultaneous download.
        for candidate in self.tasks.values():
            if candidate.task_kind != "video" or candidate.status not in ACTIVE_DUPLICATE_STATUSES:
                continue
            identities.add(candidate.source_key, candidate.url, candidate.title)
        return identities

    @staticmethod
    def _collection_child_options(parent: DownloadTask) -> dict[str, Any]:
        options = DownloadOptions.from_mapping(parent.options_json).to_dict()
        if options.get("content_mode") == "manual":
            # Content confirmation is a single-video interaction. A collection
            # must not open one blocking dialog for every selected child.
            options["content_mode"] = "video"
        return options

    @staticmethod
    def _collection_entry_spec(
        entry: Mapping[str, Any],
        fallback_index: int,
    ) -> _CollectionEntrySpec | None:
        if str(entry.get("entry_kind") or "video").strip().casefold() != "video":
            return None
        url = str(entry.get("url") or "").strip()
        if not url:
            return None
        try:
            collection_index = max(0, int(entry.get("index") or fallback_index))
        except (TypeError, ValueError, OverflowError):
            collection_index = fallback_index
        return _CollectionEntrySpec(
            url=url,
            source_key=normalize_source_key(entry.get("source_key")),
            title=str(entry.get("title") or ""),
            collection_index=collection_index,
            thumbnail_path=str(entry.get("thumbnail_path") or ""),
        )

    @staticmethod
    def _build_collection_child(
        parent: DownloadTask,
        spec: _CollectionEntrySpec,
        options: dict[str, Any],
        task_id: str,
    ) -> DownloadTask:
        child = DownloadTask(
            id=task_id,
            url=spec.url,
            output_dir=parent.output_dir,
            proxy=parent.proxy,
            cookie_file=parent.cookie_file,
            # Manual stream selection is intentionally a single-video
            # workflow. A collection must not open one blocking picker per
            # child, so its selected entries use smart best quality.
            quality="best" if parent.quality == "custom" else parent.quality,
            filename_template=parent.filename_template,
            ffmpeg_path=parent.ffmpeg_path,
            format_selector="" if parent.quality == "custom" else parent.format_selector,
            download_album=False,
            playlist_mode="single",
            cookie_source=parent.cookie_source,
            cookie_browser=parent.cookie_browser,
            cookie_profile=parent.cookie_profile,
            cookie_keyring=parent.cookie_keyring,
            cookie_container=parent.cookie_container,
            transcode_codec=parent.transcode_codec,
            transcode_device=parent.transcode_device,
            transcode_encoder=parent.transcode_encoder,
            subtitle_language=parent.subtitle_language,
            task_kind="video",
            parent_task_id=parent.id,
            root_task_id=parent.root_task_id or parent.id,
            source_key=spec.source_key,
            collection_index=spec.collection_index,
            options_json=dict(options),
        )
        child.title = spec.title or child.title
        child.thumbnail_path = spec.thumbnail_path
        return child

    def _plan_collection_materialization(
        self,
        parent: DownloadTask,
        entries: list[Mapping[str, Any]],
    ) -> _CollectionMaterializationPlan:
        existing_children = self.collection_children(parent.id)
        identities = self._collection_materialization_identity_index(existing_children)
        child_options = self._collection_child_options(parent)
        reserved_ids = set(self.tasks)
        new_children: list[DownloadTask] = []

        for fallback_index, entry in enumerate(entries, start=1):
            if not isinstance(entry, Mapping):
                continue
            spec = self._collection_entry_spec(entry, fallback_index)
            if spec is None or identities.contains(spec.source_key, spec.url, spec.title):
                continue
            child = self._build_collection_child(
                parent,
                spec,
                child_options,
                self._new_task_id(reserved_ids),
            )
            new_children.append(child)
            identities.add(spec.source_key, spec.url, spec.title)

        selected_count = len(existing_children) + len(new_children)
        parent_options = dict(parent.options_json)
        collection_metadata = dict(parent_options.get("_collection", {}))
        collection_metadata["selected"] = selected_count
        parent_options["_collection"] = collection_metadata
        parent_status = "queued" if selected_count else "canceled"
        persisted_parent = replace(
            parent,
            options_json=parent_options,
            status=parent_status,
            stage=parent_status,
            stage_text="等待下载子任务" if selected_count else "未选择下载项目",
        )
        return _CollectionMaterializationPlan(
            parent=parent,
            persisted_parent=persisted_parent,
            collection_metadata=collection_metadata,
            children=tuple(new_children),
        )

    def _persist_collection_materialization(
        self,
        plan: _CollectionMaterializationPlan,
    ) -> None:
        self.db.materialize_download_tasks(plan.persisted_parent, plan.children)

    def _publish_collection_materialization(
        self,
        plan: _CollectionMaterializationPlan,
    ) -> None:
        parent = plan.parent
        # Preserve the live options mapping and its materialization-state
        # object. The batch scheduler keeps a reference to that nested dict
        # while this method runs; replacing the whole mapping would detach the
        # reference and make its next offset update disappear on persistence.
        live_collection_metadata = parent.options_json.get("_collection")
        if isinstance(live_collection_metadata, dict):
            live_collection_metadata.clear()
            live_collection_metadata.update(plan.collection_metadata)
        else:
            parent.options_json["_collection"] = dict(plan.collection_metadata)
        parent.status = plan.persisted_parent.status
        parent.stage = plan.persisted_parent.stage
        parent.stage_text = plan.persisted_parent.stage_text
        self._sync_task_indexes(parent)

        persisted_at = time.monotonic()
        for child in plan.children:
            self._register_task(child)
        self._progress_persistence.mark_persisted(
            (parent, *plan.children),
            persisted_at,
        )

        if plan.children:
            self._best_effort_service_log(
                parent.id,
                "info",
                "合集",
                f"已分批创建 {len(plan.children)} 个下载子任务",
            )
            self.tasks_added.emit(list(plan.children))
        self.task_updated.emit(parent)
        self.queue.extend_unique(plan.child_ids)
        self._start_next()
        self._refresh_collection(parent.id)

    def enqueue_collection_entries(
        self,
        parent_id: str,
        entries: list[Mapping[str, Any]],
    ) -> list[str]:
        """Materialize selected flat entries as independent child tasks."""

        parent = self.tasks.get(parent_id)
        if not parent or parent.task_kind != "collection":
            return []
        plan = self._plan_collection_materialization(parent, entries)
        # The parent summary and every newly materialized child form one
        # durable state transition. Do not expose children to indexes, signals
        # or the scheduler until the whole SQLite transaction has committed.
        self._persist_collection_materialization(plan)
        self._publish_collection_materialization(plan)
        return plan.child_ids

    def start_collection_materialization(self, parent_id: str, order: str = "original") -> bool:
        parent = self.tasks.get(parent_id)
        if not parent or parent.task_kind != "collection":
            return False
        normalized_order = str(order or "original")
        if normalized_order not in {"original", "reverse", "random"}:
            normalized_order = "original"
        previous = parent.options_json.get("_collection_materialization", {})
        seed = int(previous.get("seed") or 0) if isinstance(previous, Mapping) else 0
        if seed <= 0:
            seed = int(uuid4().hex[:8], 16) | 1
        total = self.db.count_selected_collection_probe_entries(parent_id, "video")
        parent.options_json["_collection_materialization"] = {
            "active": True,
            "offset": 0,
            "total": total,
            "order": normalized_order,
            "seed": seed,
        }
        parent.status = "queued" if total else "canceled"
        parent.stage = parent.status
        parent.stage_text = "正在创建合集子任务" if total else "未选择下载项目"
        self._persist(parent)
        self.task_updated.emit(parent)
        if total and parent_id not in self._materialization_parents:
            self._materialization_parents.append(parent_id)
            if not self._materialization_timer.isActive():
                self._materialization_timer.start(0)
        return bool(total)

    def _process_collection_materialization_batch(self) -> None:
        if self._shutting_down or not self._materialization_parents:
            return
        parent_id = self._materialization_parents.popleft()
        parent = self.tasks.get(parent_id)
        if parent is None:
            self._schedule_next_materialization()
            return
        state = parent.options_json.get("_collection_materialization", {})
        if not isinstance(state, dict) or not state.get("active"):
            self._schedule_next_materialization()
            return
        offset = max(0, int(state.get("offset") or 0))
        order = str(state.get("order") or "original")
        sort_column = "random" if order == "random" else "collection_index"
        entries = self.db.list_collection_probe_entries(
            parent_id,
            offset=offset,
            limit=200,
            selected_only=True,
            entry_kind="video",
            sort_column=sort_column,
            sort_descending=order == "reverse",
            random_seed=int(state.get("seed") or 1),
        )
        if entries:
            self.enqueue_collection_entries(parent_id, entries)
            state["offset"] = offset + len(entries)
        total = max(0, int(state.get("total") or 0))
        finished = not entries or int(state.get("offset") or 0) >= total
        if finished:
            state["active"] = False
            self._persist(parent)
            self._refresh_collection(parent_id)
        else:
            self._persist(parent)
            self._materialization_parents.append(parent_id)
        self._schedule_next_materialization()

    def _schedule_next_materialization(self) -> None:
        if self._materialization_parents and not self._shutting_down:
            self._materialization_timer.start(0)

    def collection_children(self, parent_id: str) -> list[DownloadTask]:
        child_ids = self._task_index.child_ids(parent_id)
        return sorted(
            (self.tasks[task_id] for task_id in child_ids if task_id in self.tasks),
            key=lambda task: (task.collection_index, task.created_at, task.id),
        )

    @staticmethod
    def _collection_summary(
        aggregate: _CollectionAggregate,
        parsed_count: int,
    ) -> _CollectionSummary:
        return summarize_collection(aggregate, parsed_count)

    @staticmethod
    def _apply_collection_summary(
        parent: DownloadTask,
        summary: _CollectionSummary,
    ) -> None:
        parent.status = summary.status
        parent.stage = summary.status
        parent.stage_text = summary.stage_text
        parent.progress = summary.progress
        parent.speed_bps = summary.speed_bps
        parent.speed = format_speed(summary.speed_bps)
        parent.downloaded_bytes = summary.downloaded_bytes
        parent.total_bytes = summary.total_bytes
        parent.eta = summary.eta
        metadata = parent.options_json.setdefault("_collection", {})
        metadata.update(summary.metadata)

    def _refresh_collection(self, parent_id: str) -> None:
        parent = self.tasks.get(parent_id)
        if not parent or parent.task_kind != "collection":
            return
        aggregate = self._collection_aggregates.get(str(parent_id))
        if aggregate is None or aggregate.child_count <= 0:
            return
        metadata = parent.options_json.setdefault("_collection", {})
        parsed_count = self._progress_int(metadata.get("parsed"))
        summary = self._collection_summary(aggregate, parsed_count)
        snapshot = self._snapshot_task_state(parent)
        self._apply_collection_summary(parent, summary)
        try:
            self._persist_progress(parent, force=summary.terminal)
        except Exception as exc:
            # Collection state is derived from its children and can be retried.
            # Do not leave the card ahead of SQLite or let a timer callback
            # escape into Qt's event loop when the database is temporarily busy.
            self._restore_task_state(parent, snapshot)
            self._schedule_collection_refresh(parent_id)
            self._best_effort_service_log(
                parent_id,
                "warning",
                "数据库/持久化",
                "保存合集汇总状态失败，将自动重试",
                error=str(exc),
            )
            return
        self.task_updated.emit(parent)
        if parent.parent_task_id:
            self._schedule_collection_refresh(parent.parent_task_id)

    def _schedule_collection_refresh(self, parent_id: str) -> None:
        if not parent_id:
            return
        self._pending_collection_refreshes.add(str(parent_id))
        if not self._shutting_down and not self._collection_refresh_timer.isActive():
            self._collection_refresh_timer.start()

    def _flush_collection_refreshes(self) -> None:
        pending = tuple(self._pending_collection_refreshes)
        self._pending_collection_refreshes.clear()
        for parent_id in pending:
            self._refresh_collection(parent_id)

    @staticmethod
    def _mark_task_pausing(task: DownloadTask) -> None:
        task.pause_requested = True
        task.status = "暂停中"
        task.stage = "paused"
        task.stage_text = "正在暂停任务"

    @staticmethod
    def _mark_task_paused(task: DownloadTask) -> None:
        task.status = "paused"
        task.stage = "paused"
        task.stage_text = "已暂停"

    @staticmethod
    def _mark_task_canceled(task: DownloadTask) -> None:
        task.pause_requested = False
        task.cancel_requested = False
        task.status = "canceled"
        task.error = ""
        task.stage = "canceled"
        task.stage_text = "已取消"

    @staticmethod
    def _mark_task_canceling(task: DownloadTask) -> None:
        task.pause_requested = False
        task.cancel_requested = True
        task.status = "canceling"
        task.stage = "canceled"
        task.stage_text = "正在取消任务"

    @staticmethod
    def _mark_task_resumed(task: DownloadTask) -> None:
        task.status = "queued"
        task.stage = "queued"
        task.stage_text = "等待重新开始"
        task.error = ""
        task.pause_requested = False
        task.cancel_requested = False
        task.speed_samples.clear()
        task.speed_bps = 0.0
        task.speed = ""

    @staticmethod
    def _reset_task_for_retry(task: DownloadTask) -> None:
        task.status = "queued"
        task.error = ""
        task.progress = 0.0
        task.cancel_requested = False
        task.pause_requested = False
        task.stage = "queued"
        task.stage_text = "等待重新开始"
        task.stage_progress = 0.0
        task.retry_count = 0
        task.reconnect_message = ""
        task.visible_progress = 0.0
        task.visible_downloaded_bytes = 0
        task.visible_total_bytes = 0
        task.visible_size = ""
        task.visible_speed = ""
        task.visible_eta = ""
        task.speed_samples.clear()
        task.speed_bps = 0.0
        task.speed = ""

    def _refresh_collection_tree(self, tree_tasks: Iterable[DownloadTask]) -> None:
        """Refresh nested collection summaries from deepest to root."""

        for collection in tree_tasks:
            if collection.task_kind == "collection":
                self._refresh_collection(collection.id)

    def _refresh_parent_collection(self, task: DownloadTask) -> None:
        if task.parent_task_id:
            self._refresh_collection(task.parent_task_id)

    def _build_collection_cascade_plan(
        self,
        task_id: str,
        action: str,
    ) -> _CollectionCascadePlan | None:
        task = self.tasks.get(task_id)
        if (
            task is None
            or task.task_kind != "collection"
            or action not in {"pause", "resume", "cancel", "retry"}
        ):
            return None

        plan = _CollectionCascadePlan(
            action=action,
            tree_tasks=tuple(self._collection_tree_tasks(task_id)),
        )
        for child in plan.tree_tasks:
            if child.id == task_id or child.task_kind == "collection":
                continue
            self._plan_collection_child_action(plan, child)
        return plan

    def _plan_collection_child_action(
        self,
        plan: _CollectionCascadePlan,
        child: DownloadTask,
    ) -> None:
        action = plan.action
        if action in {"pause", "cancel"}:
            # A retry requested while the previous worker is tearing down must
            # not revive a task after its collection has just been stopped.
            plan.pending_retry_discards.add(child.id)

        if action == "pause":
            worker = self.workers.get(child.id)
            if worker is not None and child.status in {"downloading", "waiting_selection"}:
                plan.transitions.append(("pause_active", child))
                plan.worker_cancels.append((child, worker, "pause"))
            elif child.status == "queued":
                plan.transitions.append(("pause_queued", child))
            return

        if action == "resume":
            if child.status == "paused":
                plan.transitions.append(("resume", child))
            return

        if action == "cancel":
            if child.status in {
                "completed", "failed", "partial_failed", "canceled", "deleted",
            }:
                return
            if child.status == "paused":
                plan.transitions.append(("cancel_paused", child))
                return
            if child.status == "queued":
                plan.transitions.append(("cancel_queued", child))
                return
            conversion_worker = self.conversion_workers.get(child.id)
            if conversion_worker is not None and child.status == "processing":
                plan.conversion_cancels.append((child, conversion_worker))
                return
            worker = self.workers.get(child.id)
            if worker is not None:
                plan.transitions.append(("cancel_active", child))
                plan.worker_cancels.append((child, worker, "cancel"))
                return
            # A restored or partially torn-down active task may no longer own
            # a worker. It still needs a durable terminal state on cancel.
            if child.status in {
                "downloading", "processing", "waiting_selection",
                "parsing_collection", "canceling", "暂停中",
            }:
                plan.transitions.append(("cancel_orphan", child))
            return

        if child.status not in {"failed", "canceled"}:
            return
        if (
            child.id in self.workers
            or child.id in self.threads
            or child.id in self._disk_leases
        ):
            plan.pending_retries.append(child)
        else:
            plan.transitions.append(("retry", child))

    def _persist_collection_cascade(self, plan: _CollectionCascadePlan) -> None:
        mutators = {
            "pause_active": self._mark_task_pausing,
            "pause_queued": self._mark_task_paused,
            "cancel_paused": self._mark_task_canceled,
            "cancel_queued": self._mark_task_canceled,
            "cancel_orphan": self._mark_task_canceled,
            "cancel_active": self._mark_task_canceling,
            "resume": self._mark_task_resumed,
            "retry": self._reset_task_for_retry,
        }
        with self._durable_task_mutations(plan.mutated_tasks):
            for transition, child in plan.transitions:
                mutators[transition](child)

    def _apply_collection_cascade_queue(
        self,
        plan: _CollectionCascadePlan,
    ) -> bool:
        remove_from_queue = set(plan.transition_ids(
            "pause_queued", "cancel_queued", "cancel_orphan",
        ))
        if remove_from_queue:
            self.queue.remove_all(remove_from_queue)

        resume_ids = plan.transition_ids("resume")
        for child_id in reversed(resume_ids):
            self.queue.appendleft_unique(child_id)

        retry_ids = plan.transition_ids("retry")
        self._pending_runtime_retries.difference_update(
            plan.pending_retry_discards
        )
        for child_id in retry_ids:
            self._pending_runtime_retries.discard(child_id)
            self.queue.append_unique(child_id)
        for child in plan.pending_retries:
            self._pending_runtime_retries.add(child.id)
        return bool(resume_ids or retry_ids)

    def _request_collection_cascade_stops(
        self,
        plan: _CollectionCascadePlan,
    ) -> None:
        for child, worker, reason in plan.worker_cancels:
            try:
                worker.cancel(reason)
            except Exception as exc:
                self._best_effort_service_log(
                    child.id,
                    "warning",
                    "用户操作",
                    "请求任务停止失败，等待线程状态回调处理",
                    error=str(exc),
                    reason=reason,
                )
        for child, worker in plan.conversion_cancels:
            self._mark_conversion_canceling(child, "正在取消格式转换")
            try:
                worker.cancel()
            except Exception as exc:
                self._best_effort_service_log(
                    child.id,
                    "warning",
                    "用户操作",
                    "请求格式转换停止失败，等待线程状态回调处理",
                    error=str(exc),
                )

    def _publish_collection_cascade(self, plan: _CollectionCascadePlan) -> None:
        action_messages = {
            "pause_active": "已请求暂停任务",
            "cancel_active": "已请求取消任务",
            "cancel_queued": "排队任务已取消",
            "resume": "任务已恢复并重新排队",
            "retry": "任务已重试",
        }
        for transition, child in plan.transitions:
            self.task_updated.emit(child)
            message = action_messages.get(transition)
            if message:
                self._best_effort_service_log(
                    child.id, "info", "用户操作", message,
                )
        for child, _worker in plan.conversion_cancels:
            self._best_effort_service_log(
                child.id, "info", "用户操作", "已请求取消格式转换",
            )
        for child in plan.pending_retries:
            self._best_effort_service_log(
                child.id,
                "info",
                "用户操作",
                "已请求重试；当前线程清理完成后自动重新排队",
            )
        self._refresh_collection_tree(plan.tree_tasks)

    def _cascade_collection(self, task_id: str, action: str) -> bool:
        plan = self._build_collection_cascade_plan(task_id, action)
        if plan is None:
            return False

        # SQLite is authoritative. Queue mutation and worker cancellation must
        # happen only after the complete group transition has been committed.
        self._persist_collection_cascade(plan)
        should_start = self._apply_collection_cascade_queue(plan)
        self._request_collection_cascade_stops(plan)
        self._publish_collection_cascade(plan)
        if should_start:
            self._start_next()
        return True

    @staticmethod
    def _resolved_duplicate_identity(task: DownloadTask) -> tuple[str, str, str]:
        title = normalize_media_title(task.title)
        if title in {
            normalize_media_title("等待获取视频信息"),
            normalize_media_title("正在解析合集"),
            normalize_media_title("解析中"),
            normalize_media_title("Parsing Collection"),
        }:
            title = ""
        return (
            normalize_source_key(task.source_key),
            normalize_source_url(task.url),
            title,
        )

    def _coalesce_resolved_duplicates(self, task: DownloadTask) -> str:
        """Keep one active task after aliases resolve to the same video."""

        identity = self._resolved_duplicate_identity(task)
        if self._resolved_identity_state.get(task.id) == identity:
            return task.id
        self._resolved_identity_state[task.id] = identity
        source_key, source_url, title = identity
        if not source_key and not source_url and not title:
            return task.id

        matches: list[DownloadTask] = []
        for candidate in self.tasks.values():
            if candidate.task_kind != "video" or candidate.status not in ACTIVE_DUPLICATE_STATUSES:
                continue
            candidate_key, candidate_url, candidate_title = self._resolved_duplicate_identity(candidate)
            if (
                (source_key and candidate_key == source_key)
                or (source_url and candidate_url == source_url)
                or (title and candidate_title == title)
            ):
                matches.append(candidate)
        if len(matches) <= 1:
            return task.id

        # A manual conversion already owns the completed file and must never
        # be discarded underneath its FFmpeg worker. Otherwise preserve task
        # insertion order, which corresponds to the first user submission.
        canonical = next(
            (candidate for candidate in matches if candidate.status == "processing"),
            matches[0],
        )
        for duplicate in matches:
            if duplicate.id == canonical.id or duplicate.id in self.conversion_workers:
                continue
            self._best_effort_service_log(
                duplicate.id,
                "info",
                "任务",
                "解析结果与已有任务重复，已合并到先提交的任务",
                canonical_task_id=canonical.id,
                source_key=source_key,
            )
            self.discard_task(duplicate.id)
        return canonical.id

    def find_active_duplicate(
        self,
        url: str,
        output_dir: str,
        quality: str,
        playlist_mode: str,
        *,
        exclude_task_id: str = "",
        transcode_codec: str = "",
        transcode_device: str = "",
        transcode_encoder: str = "",
        subtitle_language: str = "",
        source_key: str = "",
        title: str = "",
    ) -> str | None:
        """Return unfinished work for the same source identity.

        Paused work is included because it already has a durable database row
        and can be resumed. Completed, failed and cancelled rows remain valid
        download-history entries and do not block an intentional new run. A
        A platform content ID, source link or normalized video name is
        sufficient: changing quality, output directory or post-processing
        options must not create two simultaneous downloads of the same video.
        """

        normalized_url = normalize_source_url(url)
        normalized_source_key = normalize_source_key(source_key)
        normalized_title = normalize_media_title(title)
        if not normalized_url and not normalized_source_key and not normalized_title:
            return None
        for task in self.tasks.values():
            if task.id == exclude_task_id or task.status not in ACTIVE_DUPLICATE_STATUSES:
                continue
            if normalized_url and normalize_source_url(task.url) == normalized_url:
                return task.id
            if (
                normalized_source_key
                and normalize_source_key(task.source_key) == normalized_source_key
            ):
                return task.id
            if (
                normalized_title
                and task.task_kind == "video"
                and normalize_media_title(task.title) == normalized_title
            ):
                return task.id
        return None

    def cancel(self, task_id: str | None = None) -> None:
        task_id = task_id or self.active_task_id
        if not task_id or task_id not in self.tasks:
            return
        task = self.tasks[task_id]
        if self._cascade_collection(task_id, "cancel"):
            return
        if task.status == "waiting_selection" and not task.parent_task_id:
            self.discard_task(task_id)
            return
        if task.status == "paused":
            with self._durable_task_mutation(task):
                self._mark_task_canceled(task)
            self.task_updated.emit(task)
            if task.parent_task_id:
                self._refresh_collection(task.parent_task_id)
            return
        if task.status == "queued":
            # The row action is labelled "取消".  A queued task has no
            # worker to interrupt, so mark it as canceled rather than
            # silently turning it into a paused task.  This keeps the UI
            # semantics consistent and allows the normal retry action.
            with self._durable_task_mutation(task):
                self._mark_task_canceled(task)
            self.queue.remove_all((task_id,))
            self.task_updated.emit(task)
            self._best_effort_service_log(
                task_id,
                "info",
                "用户操作",
                "排队任务已取消",
            )
            return
        conversion_worker = self.conversion_workers.get(task_id)
        if conversion_worker is not None and task.status == "processing":
            self._mark_conversion_canceling(task, "正在取消格式转换")
            conversion_worker.cancel()
            self._best_effort_service_log(
                task_id,
                "info",
                "用户操作",
                "已请求取消格式转换",
            )
            return
        worker = self.workers.get(task_id)
        if worker:
            with self._durable_task_mutation(task):
                self._mark_task_canceling(task)
            self.task_updated.emit(task)
            worker.cancel("cancel")
            self._best_effort_service_log(
                task_id,
                "info",
                "用户操作",
                "已请求取消任务",
            )

    def pause(self, task_id: str | None = None) -> None:
        task_id = task_id or self.active_task_id
        if not task_id or task_id not in self.tasks:
            return
        task = self.tasks[task_id]
        if self._cascade_collection(task_id, "pause"):
            return
        worker = self.workers.get(task_id)
        if worker and task.status in {"downloading", "waiting_selection"}:
            with self._durable_task_mutation(task):
                self._mark_task_pausing(task)
            self.task_updated.emit(task)
            worker.cancel("pause")
            self._best_effort_service_log(
                task_id,
                "info",
                "用户操作",
                "已请求暂停任务",
            )
        elif task.status == "queued":
            # A queued task has no worker yet; pausing it means removing it
            # from the queue and retaining the task record for later resume.
            with self._durable_task_mutation(task):
                self._mark_task_paused(task)
            self.queue.remove_all((task_id,))
            self.task_updated.emit(task)

    def resume(self, task_id: str) -> None:
        task = self.tasks.get(task_id)
        if task and task.task_kind == "collection":
            self._cascade_collection(task_id, "resume")
            return
        if not task or task.status != "paused":
            return
        with self._durable_task_mutation(task):
            self._mark_task_resumed(task)
        self.queue.appendleft_unique(task.id)
        self.task_updated.emit(task)
        self._best_effort_service_log(
            task_id,
            "info",
            "用户操作",
            "任务已恢复并重新排队",
        )
        self._start_next()

    def retry(self, task_id: str) -> None:
        task = self.tasks.get(task_id)
        if task and task.task_kind == "collection":
            self._cascade_collection(task_id, "retry")
            return
        if task and task.status == "deleted":
            self.redownload(task_id)
            return
        if not task or task.status not in {"failed", "canceled"}:
            return
        if (
            task_id in self.workers
            or task_id in self.threads
            or task_id in self._disk_leases
        ):
            # Worker outcome signals reach the GUI before QThread.finished.
            # Preserve a retry click made in that short window instead of
            # overwriting the still-owned runtime or silently ignoring it.
            self._pending_runtime_retries.add(task_id)
            self._best_effort_service_log(
                task_id,
                "info",
                "用户操作",
                "已请求重试；当前线程清理完成后自动重新排队",
            )
            return
        self._queue_task_retry(task)

    def _queue_task_retry(self, task: DownloadTask) -> None:
        task_id = task.id
        with self._durable_task_mutation(task):
            self._reset_task_for_retry(task)
        self._pending_runtime_retries.discard(task_id)
        self.queue.append_unique(task_id)
        self.task_updated.emit(task)
        self._best_effort_service_log(
            task_id,
            "info",
            "用户操作",
            "任务已重试",
        )
        self._start_next()

    def _start_pending_runtime_retry(self, task_id: str) -> bool:
        if task_id not in self._pending_runtime_retries:
            return False
        if self._shutting_down:
            self._pending_runtime_retries.discard(task_id)
            return False
        task = self.tasks.get(task_id)
        if task is None or task.status not in {"failed", "canceled"}:
            self._pending_runtime_retries.discard(task_id)
            return False
        try:
            self._queue_task_retry(task)
        except Exception as exc:
            self._best_effort_service_log(
                task_id,
                "warning",
                "数据库/持久化",
                "保存自动重试状态失败，将稍后重试",
                error=str(exc),
            )
            if not self._shutting_down:
                QTimer.singleShot(
                    1000,
                    partial(self._start_pending_runtime_retry, task_id),
                )
            return False
        return True

    def start_task(self, task_id: str) -> None:
        task = self.tasks.get(task_id)
        if not task:
            return
        if task.status == "queued":
            self.queue.append_unique(task_id)
            self._start_next()
        elif task.status == "paused":
            self.resume(task_id)
        elif task.status in {"failed", "partial_failed", "canceled"}:
            self.retry(task_id)
        elif task.status in {"completed", "deleted"}:
            self.redownload(task_id)

    def redownload(self, task_id: str, quality_override: str | None = None) -> str | None:
        task = self.tasks.get(task_id)
        if not task:
            return None
        # Avoid creating duplicate rows when the user double-clicks a context
        # menu action or repeats it while the previous redownload is still
        # queued/running.  A completed task can still be intentionally
        # downloaded again after the existing run has finished.
        requested_quality = quality_override or task.quality
        existing_id = self.find_active_duplicate(
            task.url,
            task.output_dir,
            requested_quality,
            task.playlist_mode,
            exclude_task_id=task.id,
            transcode_codec=task.transcode_codec,
            transcode_device=task.transcode_device,
            transcode_encoder=task.transcode_encoder,
            subtitle_language=task.subtitle_language,
            source_key=task.source_key,
            title=task.title,
        )
        if existing_id:
            return existing_id
        return self.enqueue(task.url, task.output_dir, task.proxy, task.cookie_file, quality=requested_quality,
                            filename_template=task.filename_template, ffmpeg_path=task.ffmpeg_path,
                           format_selector="" if quality_override else task.format_selector,
                            download_album=task.download_album, playlist_mode=task.playlist_mode,
                            cookie_source=task.cookie_source, cookie_browser=task.cookie_browser,
                            cookie_profile=task.cookie_profile, cookie_keyring=task.cookie_keyring,
                            cookie_container=task.cookie_container,
                            transcode_codec=task.transcode_codec,
                            transcode_device=task.transcode_device,
                            transcode_encoder=task.transcode_encoder,
                            subtitle_language=task.subtitle_language,
                            source_key=task.source_key,
                            options_json=task.options_json)

    def _connect_completed_conversion_runtime(
        self,
        runtime: _CompletedConversionRuntime,
    ) -> None:
        thread = runtime.thread
        worker = runtime.worker
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_completed_conversion_progress, Qt.QueuedConnection)
        worker.completed.connect(self._on_completed_conversion_completed, Qt.QueuedConnection)
        worker.skipped.connect(self._on_completed_conversion_skipped, Qt.QueuedConnection)
        worker.failed.connect(self._on_completed_conversion_failed, Qt.QueuedConnection)
        worker.canceled.connect(self._on_completed_conversion_canceled, Qt.QueuedConnection)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(
            self._conversion_thread_finished_from_signal,
            Qt.QueuedConnection,
        )

    def _prepare_completed_conversion_runtime(
        self,
        task: DownloadTask,
        encoder: str,
        ffmpeg_path: str,
        ffprobe_path: str,
    ) -> _CompletedConversionRuntime:
        task_options = DownloadOptions.from_mapping(task.options_json)
        disk_lease = DiskReservationLease(self.disk_capacity_manager)
        worker = CompletedMediaTranscodeWorker(
            task.id,
            task.media_path,
            ffmpeg_path,
            ffprobe_path,
            encoder,
            self.disk_capacity_manager,
            disk_lease,
            task_options.processing_temp_dir,
            task_options.container,
        )
        thread = QThread()
        runtime = _CompletedConversionRuntime(thread, worker, disk_lease)
        try:
            thread.setProperty("conversion_task_id", task.id)
            worker.moveToThread(thread)
            self._connect_completed_conversion_runtime(runtime)
        except Exception:
            delete_unstarted_worker(worker, thread)
            raise
        return runtime

    def _report_completed_conversion_start_failure(
        self,
        task: DownloadTask,
        error: Exception,
    ) -> None:
        message = f"无法启动格式转换：{error}"
        self._best_effort_service_log(
            task.id,
            "warning",
            "格式/转换",
            "格式转换启动失败，原文件保持不变",
            error=str(error),
            input_path=task.media_path,
        )
        self._best_effort_service_log_flush(task.id)
        self.conversion_failed.emit(task.id, message)

    def _reject_completed_conversion(self, task_id: str, message: str) -> None:
        self.conversion_failed.emit(task_id, message)

    def _completed_conversion_request(
        self,
        task_id: str,
        encoder: str,
        ffmpeg_path: str,
        ffprobe_path: str,
    ) -> _CompletedConversionRequest | None:
        task = self.tasks.get(task_id)
        if task is None or task.task_kind != "video":
            return None
        if self._shutting_down:
            self._reject_completed_conversion(task_id, "下载服务正在退出，无法开始格式转换")
            return None
        if task_id in self.conversion_workers:
            self._reject_completed_conversion(task_id, "该任务正在转换格式")
            return None
        if task.status != "completed":
            self._reject_completed_conversion(task_id, "只有已完成的下载任务可以转换格式")
            return None
        if not Path(task.media_path).is_file():
            self._reject_completed_conversion(task_id, "已完成任务的媒体文件不存在")
            return None

        normalized_encoder = normalize_transcode_encoder(encoder)
        if normalized_encoder == "original":
            return _CompletedConversionRequest(task, normalized_encoder)
        configured_ffmpeg = ffmpeg_runtime_path(ffmpeg_path or task.ffmpeg_path)
        if not configured_ffmpeg:
            self._reject_completed_conversion(task_id, "未找到 FFmpeg，无法转换格式")
            return None
        configured_ffprobe = ffprobe_runtime_path(
            ffmpeg_path or task.ffmpeg_path,
            ffprobe_path or self.ffprobe_path,
        )
        if not configured_ffprobe:
            self._reject_completed_conversion(task_id, "未找到 FFprobe，无法识别当前视频格式")
            return None
        return _CompletedConversionRequest(
            task,
            normalized_encoder,
            configured_ffmpeg,
            configured_ffprobe,
        )

    def _activate_completed_conversion_runtime(
        self,
        request: _CompletedConversionRequest,
        runtime: _CompletedConversionRuntime,
    ) -> None:
        task = request.task
        task.status = "processing"
        task.error = ""
        task.completion_warning = ""
        task.stage = "verifying"
        task.stage_text = "正在读取当前视频格式"
        task.stage_progress = 0.0
        task.current_transcode_encoder = request.encoder
        # Manual conversion is deliberately transient. Keep the durable task
        # completed until the converted file and media catalog are committed
        # together; a crash naturally restores the original completed row.
        self._sync_task_indexes(task)
        self.conversion_threads[task.id] = runtime.thread
        self.conversion_workers[task.id] = runtime.worker
        self._conversion_disk_leases[task.id] = runtime.disk_lease
        self.task_updated.emit(task)
        self._refresh_parent_collection(task)
        self._best_effort_service_log(
            task.id,
            "info",
            "格式/转换",
            "用户请求转换已完成任务的格式",
            encoder=request.encoder,
            input_path=task.media_path,
        )

    def _rollback_completed_conversion_start(
        self,
        task: DownloadTask,
        runtime: _CompletedConversionRuntime,
        error: Exception,
    ) -> None:
        self.conversion_threads.pop(task.id, None)
        self.conversion_workers.pop(task.id, None)
        self._conversion_disk_leases.pop(task.id, None)
        self._deferred_conversion_finishes.discard(runtime.thread)
        delete_unstarted_worker(runtime.worker, runtime.thread)
        self._release_download_capacity_lease(task.id, runtime.disk_lease)
        warning = "无法启动格式转换，已保留原文件"
        self._restore_completed_after_conversion(task, warning)
        task.completion_warning = warning
        self._sync_task_indexes(task)
        self.task_updated.emit(task)
        self._refresh_parent_collection(task)
        self._report_completed_conversion_start_failure(task, error)

    def convert_completed_task(
        self,
        task_id: str,
        encoder: str,
        *,
        ffmpeg_path: str = "",
        ffprobe_path: str = "",
    ) -> bool:
        request = self._completed_conversion_request(
            task_id,
            encoder,
            ffmpeg_path,
            ffprobe_path,
        )
        if request is None:
            return False
        if request.encoder == "original":
            self.conversion_finished.emit(task_id, "keep_original", True)
            return True

        try:
            runtime = self._prepare_completed_conversion_runtime(
                request.task,
                request.encoder,
                request.ffmpeg_path,
                request.ffprobe_path,
            )
        except Exception as exc:
            self._report_completed_conversion_start_failure(request.task, exc)
            return False

        self._activate_completed_conversion_runtime(request, runtime)
        try:
            runtime.thread.start()
        except Exception as exc:
            self._rollback_completed_conversion_start(request.task, runtime, exc)
            return False
        return True

    def _restore_completed_after_conversion(self, task: DownloadTask, text: str = "下载完成") -> None:
        task.status = "completed"
        task.progress = 100.0
        task.error = ""
        task.stage = "completed"
        task.stage_text = text
        task.stage_progress = 100.0
        task.current_transcode_encoder = ""

    def _mark_conversion_canceling(self, task: DownloadTask, text: str) -> None:
        """Publish cancellation without replacing the durable completed row."""

        task.status = "canceling"
        task.stage = "canceling"
        task.stage_text = text
        task.stage_progress = 0.0
        self._sync_task_indexes(task)
        self.task_updated.emit(task)
        self._refresh_parent_collection(task)

    @Slot(str, object)
    def _on_completed_conversion_progress(self, task_id: str, payload: dict) -> None:
        task = self.tasks.get(task_id)
        if task is None:
            return
        previous_stage_progress = self._progress_float(task.stage_progress)
        task.status = "processing"
        stage_changed = self._apply_stage_progress(task, payload)
        # Manual conversion progress is runtime-only. Persisting the transient
        # ``processing`` state could strand a completed download after a crash
        # and used to conflict with the atomic converted-file commit below.
        self._sync_task_indexes(task)
        now = time.monotonic()
        reached_stage_end = previous_stage_progress < 100.0 <= task.stage_progress
        last_emit = self._last_progress_emit.get(task_id, 0.0)
        if stage_changed or reached_stage_end or now - last_emit >= 0.15:
            self._last_progress_emit[task_id] = now
            self.task_progress.emit(task_id, payload)

    @Slot(str, object)
    def _on_completed_conversion_skipped(self, task_id: str, payload: dict) -> None:
        task = self.tasks.get(task_id)
        if task is None:
            return
        reason = str(payload.get("reason") or "already_target")
        codec = str(payload.get("codec") or "")
        self._restore_completed_after_conversion(task)
        self._sync_task_indexes(task)
        self.task_updated.emit(task)
        self._refresh_parent_collection(task)
        self._best_effort_service_log(
            task_id,
            "info",
            "格式/转换",
            "当前媒体已经符合目标格式，跳过转换",
            reason=reason,
            codec=codec,
            encoder=str(payload.get("encoder") or ""),
        )
        result = reason if not codec else f"{reason}:{codec}"
        self.conversion_finished.emit(task_id, result, True)

    @Slot(str, object)
    def _on_completed_conversion_completed(self, task_id: str, payload: dict) -> None:
        commit = self._completed_conversion_commit(task_id, payload)
        if commit is None:
            return
        try:
            self._commit_completed_conversion(commit)
        except Exception as exc:
            self._rollback_completed_conversion(task_id, commit.publication)
            self._on_completed_conversion_failed(task_id, str(exc))
            return
        self._finalize_completed_conversion(commit)
        self._publish_completed_conversion_success(commit)

    def _completed_conversion_commit(
        self,
        task_id: str,
        payload: Mapping[str, Any],
    ) -> _CompletedConversionCommit | None:
        publication = payload.get("publication")
        task = self.tasks.get(task_id)
        if task is None:
            # The worker has published a validated file and handed rollback
            # ownership to this service. A reset may have removed the task.
            self._rollback_completed_conversion(
                task_id,
                publication if isinstance(publication, PublishedTranscode) else None,
            )
            return None
        old_path = str(payload.get("old_path") or task.media_path)
        new_path = str(payload.get("new_path") or old_path)
        encoder = normalize_transcode_encoder(payload.get("encoder"))
        return _CompletedConversionCommit(
            task=task,
            publication=(
                publication if isinstance(publication, PublishedTranscode) else None
            ),
            old_path=old_path,
            new_path=new_path,
            encoder=encoder,
            codec=transcode_encoder_codec(encoder),
            device=transcode_encoder_device(encoder),
        )

    def _commit_completed_conversion(
        self,
        commit: _CompletedConversionCommit,
    ) -> None:
        self.db.replace_completed_media_path(
            commit.task.id,
            commit.old_path,
            commit.new_path,
            transcode_codec=commit.codec,
            transcode_device=commit.device,
            transcode_encoder=commit.encoder,
        )

    def _rollback_completed_conversion(
        self,
        task_id: str,
        publication: PublishedTranscode | None,
    ) -> None:
        if publication is None:
            return
        try:
            publication.rollback()
        except OSError as exc:
            self._best_effort_service_log(
                task_id,
                "error",
                "文件/清理",
                "数据库更新失败且原文件回滚失败",
                error=str(exc),
            )

    def _finalize_completed_conversion(
        self,
        commit: _CompletedConversionCommit,
    ) -> None:
        if commit.publication is None:
            return
        try:
            commit.publication.finalize()
        except OSError as exc:
            self._best_effort_service_log(
                commit.task.id,
                "warning",
                "文件/清理",
                "格式转换已入库，但旧文件清理失败",
                error=str(exc),
            )

    def _publish_completed_conversion_success(
        self,
        commit: _CompletedConversionCommit,
    ) -> None:
        task = commit.task
        task_id = task.id
        task.media_path = commit.new_path
        task.transcode_codec = commit.codec
        task.transcode_device = commit.device
        task.transcode_encoder = commit.encoder
        self._restore_completed_after_conversion(task, "格式转换完成")
        self._progress_persistence.mark_persisted((task,), time.monotonic())
        self._sync_task_indexes(task)
        self.task_updated.emit(task)
        self._refresh_parent_collection(task)
        self.task_media_completed.emit(task_id, MediaItem(
            source_url=task.url,
            title=task.title,
            uploader=task.uploader,
            thumbnail_path=task.thumbnail_path,
            video_path=commit.new_path,
        ))
        self._best_effort_service_log(
            task_id,
            "info",
            "格式/转换",
            "已完成任务的格式转换成功",
            encoder=commit.encoder,
            old_path=commit.old_path,
            new_path=commit.new_path,
        )
        self._best_effort_service_log_flush(task_id)
        self.conversion_finished.emit(task_id, commit.encoder, False)

    @Slot(str, str)
    def _on_completed_conversion_failed(self, task_id: str, error: str) -> None:
        task = self.tasks.get(task_id)
        if task is None:
            return
        self._restore_completed_after_conversion(task, "格式转换失败，已保留原文件")
        task.completion_warning = "格式转换失败，已保留原文件"
        self._sync_task_indexes(task)
        self.task_updated.emit(task)
        self._refresh_parent_collection(task)
        self._best_effort_service_log(
            task_id,
            "warning",
            "格式/转换",
            "已完成任务的格式转换失败，原文件保持不变",
            error=error,
            input_path=task.media_path,
        )
        self._best_effort_service_log_flush(task_id)
        self.conversion_failed.emit(task_id, error)

    @Slot(str)
    def _on_completed_conversion_canceled(self, task_id: str) -> None:
        task = self.tasks.get(task_id)
        if task is None:
            return
        self._restore_completed_after_conversion(task)
        self._sync_task_indexes(task)
        self.task_updated.emit(task)
        self._refresh_parent_collection(task)
        self._best_effort_service_log(
            task_id,
            "info",
            "格式/转换",
            "格式转换已取消，原文件保持不变",
        )
        self._best_effort_service_log_flush(task_id)

    @Slot()
    def _conversion_thread_finished_from_signal(self) -> None:
        thread = self.sender()
        if not isinstance(thread, QThread):
            return
        task_id = str(thread.property("conversion_task_id") or "")
        if task_id:
            self._defer_conversion_thread_finished(task_id, thread)

    def _defer_conversion_thread_finished(
        self,
        task_id: str,
        thread: QThread | None = None,
    ) -> None:
        """Let queued conversion outcome handlers take publication ownership."""

        owned_thread = thread or self.conversion_threads.get(task_id)
        if not isinstance(owned_thread, QThread):
            return
        if owned_thread in self._deferred_conversion_finishes:
            return
        self._deferred_conversion_finishes.add(owned_thread)
        QTimer.singleShot(
            0,
            partial(
                self._complete_deferred_conversion_thread_finish,
                task_id,
                owned_thread,
            ),
        )

    def _complete_deferred_conversion_thread_finish(
        self,
        task_id: str,
        thread: QThread,
    ) -> None:
        self._deferred_conversion_finishes.discard(thread)
        if self.conversion_threads.get(task_id) is thread:
            self._conversion_thread_finished(task_id, expected_thread=thread)
        try:
            thread.deleteLater()
        except RuntimeError:
            pass

    def _conversion_thread_finished(
        self,
        task_id: str,
        *,
        expected_thread: QThread | None = None,
    ) -> None:
        current_thread = self.conversion_threads.get(task_id)
        if expected_thread is not None and current_thread is not expected_thread:
            return
        if (
            task_id not in self.conversion_threads
            and task_id not in self.conversion_workers
            and task_id not in self._conversion_disk_leases
        ):
            return
        if isinstance(current_thread, QThread):
            self._deferred_conversion_finishes.discard(current_thread)
        self.conversion_threads.pop(task_id, None)
        self.conversion_workers.pop(task_id, None)
        lease = self._conversion_disk_leases.pop(task_id, None)
        if lease is not None:
            self._release_download_capacity_lease(task_id, lease)
        self._last_progress_emit.pop(task_id, None)
        self._progress_persistence.pending.pop(task_id, None)
        task = self.tasks.get(task_id)
        if task is not None:
            self._recover_unreported_conversion_outcome(task)
        root_task_id = self._collection_delete_root_by_child.pop(task_id, "")
        if root_task_id:
            self._try_finish_pending_collection_delete(root_task_id)
            return
        self._finish_pending_conversion_delete(task_id)

    def _recover_unreported_conversion_outcome(self, task: DownloadTask) -> bool:
        if task.status not in {"processing", "canceling"}:
            return False
        was_canceling = task.status == "canceling"
        text = (
            "格式转换已取消，已保留原文件"
            if was_canceling else "格式转换意外结束，已保留原文件"
        )
        self._restore_completed_after_conversion(task, text)
        task.completion_warning = "" if was_canceling else text
        self._sync_task_indexes(task)
        self.task_updated.emit(task)
        self._best_effort_service_log(
            task.id,
            "info" if was_canceling else "warning",
            "格式/转换",
            (
                "格式转换取消后线程已结束，已恢复完成状态"
                if was_canceling
                else "格式转换线程结束但未报告结果，已恢复完成状态"
            ),
            input_path=task.media_path,
        )
        if not was_canceling:
            self.conversion_failed.emit(task.id, text)
        return True

    def _finish_pending_conversion_delete(self, task_id: str) -> bool:
        if task_id not in self._pending_deletes:
            return False
        delete_files = self._pending_deletes.pop(task_id)
        try:
            self._remove_task_record(task_id, delete_files)
        except Exception as exc:
            # The conversion runtime is already gone. Keep the completed task
            # and its files visible so the user can retry deletion without
            # rerunning the conversion or losing the original media record.
            self._best_effort_service_log(
                task_id,
                "error",
                "数据库/删除",
                "格式转换已停止，但删除任务记录失败，可重新删除",
                error=str(exc),
                delete_files=bool(delete_files),
            )
        return True

    def delete_task(self, task_id: str, delete_files: bool = False) -> bool:
        task = self.tasks.get(task_id)
        if not task:
            return False
        if task.task_kind == "collection":
            tree_tasks = self._collection_tree_tasks(task_id)
            active_children = [
                item
                for item in tree_tasks
                if item.id in self.workers or item.id in self.conversion_workers
            ]
            if not active_children:
                pending = self._pending_collection_deletes.get(task_id)
                effective_delete_files = bool(
                    delete_files or (pending is not None and pending.delete_files)
                )
                self._remove_collection_task_tree(
                    task_id,
                    effective_delete_files,
                    cleanup_plans=(
                        pending.cleanup_plans if pending is not None else None
                    ),
                )
                self._best_effort_service_log(
                    task_id,
                    "info",
                    "用户操作",
                    "合集任务记录已删除",
                    delete_files=effective_delete_files,
                )
                return True
            self._request_running_collection_delete(
                task,
                tree_tasks,
                active_children,
                delete_files,
            )
            return True
        if task_id in self.conversion_workers:
            self._pending_deletes[task_id] = bool(delete_files)
            self._mark_conversion_canceling(task, "正在取消格式转换并删除任务")
            self.conversion_workers[task_id].cancel()
            self._best_effort_service_log(
                task_id,
                "info",
                "用户操作",
                "已请求取消格式转换并删除任务",
                delete_files=bool(delete_files),
            )
            return True
        if task_id in self.workers:
            # Deleting an active task also implies cancelling it.  Keep the
            # requested file policy and remove everything after the worker
            # unwinds, so the user does not need to pause first.
            with self._durable_task_mutation(task):
                task.pause_requested = False
                task.cancel_requested = True
                task.status = "canceling"
            self._pending_deletes[task_id] = bool(delete_files)
            self.task_updated.emit(task)
            self.workers[task_id].cancel("delete")
            self._best_effort_service_log(
                task_id,
                "info",
                "用户操作",
                "已请求删除任务",
                delete_files=bool(delete_files),
            )
            return True
        self._remove_task_record(task_id, delete_files)
        self._best_effort_service_log(
            task_id,
            "info",
            "用户操作",
            "任务记录已删除",
            delete_files=bool(delete_files),
        )
        return True

    def _task_file_cleanup_plan(self, task: DownloadTask) -> tuple[set[Path], Path]:
        """Resolve the bounded set of app-managed files before deleting its DB manifest."""

        manifest_rows = self.db.list_download_task_files(task.id)
        files: set[Path] = set()
        output_root = _resolved_output_root(task.output_dir)
        for row in manifest_rows:
            if not int(row["managed"] or 0):
                continue
            local_path = _managed_output_file(
                output_root,
                str(row["path"] or ""),
            )
            if local_path is not None:
                files.add(local_path)
        # Incomplete downloads do not yet have a final manifest. Their tightly
        # bounded .part family still needs cleanup on explicit "delete files".
        # Completed tasks never fall back to filename-prefix guesses.
        if not files and task.status != "completed":
            files = task_download_artifact_paths(task)
            if task.thumbnail_path:
                thumbnail = _managed_output_file(output_root, task.thumbnail_path)
                if thumbnail is not None:
                    files.add(thumbnail)
        return files, output_root

    @staticmethod
    def _delete_planned_task_files(
        task: DownloadTask,
        files: set[Path],
        output_root: Path,
    ) -> None:
        candidate_dirs = {path.parent for path in files}
        for file_path in files:
            try:
                file_path.unlink(missing_ok=True)
            except OSError:
                pass
        if not DownloadOptions.from_mapping(task.options_json).organize_task_folder:
            return
        for directory in sorted(candidate_dirs, key=lambda value: len(value.parts), reverse=True):
            try:
                directory.resolve().relative_to(output_root)
                if directory != output_root and not any(directory.iterdir()):
                    directory.rmdir()
            except (OSError, ValueError):
                pass

    @staticmethod
    def _cleanup_task_processing_workspaces(task: DownloadTask) -> None:
        task_options = DownloadOptions.from_mapping(task.options_json)
        for operation in ("download", "manual-transcode"):
            cleanup_processing_workspace(processing_temp_workspace_path(
                task_options.processing_temp_dir,
                task.id,
                operation,
            ))

    def _forget_task_runtime_state(self, task_id: str) -> DownloadTask | None:
        task = self._unregister_task(task_id)
        if task is None:
            return None
        self.queue.remove_all((task_id,))
        self._pending_runtime_retries.discard(task_id)
        self._discard_tasks.discard(task_id)
        self._pending_deletes.pop(task_id, None)
        self._progress_persistence.forget(task_id)
        self._last_progress_emit.pop(task_id, None)
        if not self._progress_persistence.pending:
            self._progress_flush_timer.stop()
        return task

    def _collection_tree_tasks(self, root_task_id: str) -> list[DownloadTask]:
        """Return one collection subtree from the in-memory parent index."""

        pending: deque[tuple[str, int]] = deque(((str(root_task_id), 0),))
        visited: set[str] = set()
        descendants: list[tuple[int, DownloadTask]] = []
        while pending:
            task_id, depth = pending.popleft()
            if task_id in visited:
                continue
            visited.add(task_id)
            task = self.tasks.get(task_id)
            if task is not None:
                descendants.append((depth, task))
            pending.extend(
                (child_id, depth + 1)
                for child_id in self._task_index.child_ids(task_id)
                if child_id not in visited
            )
        # Remove deepest descendants first so parent indexes and aggregate
        # counters never temporarily reference an already-removed parent.
        descendants.sort(
            key=lambda item: (
                -item[0],
                item[1].collection_index,
                item[1].id,
            )
        )
        return [task for _depth, task in descendants]

    def _request_running_collection_delete(
        self,
        root: DownloadTask,
        tree_tasks: list[DownloadTask],
        active_children: list[DownloadTask],
        delete_files: bool,
    ) -> None:
        root_id = root.id
        pending = self._pending_collection_deletes.get(root_id)
        if pending is not None:
            if delete_files and not pending.delete_files:
                pending.delete_files = True
                pending.cleanup_plans.update(
                    self._collection_delete_cleanup_plans(tree_tasks)
                )
            return

        pending = _PendingCollectionDelete(delete_files=bool(delete_files))
        if pending.delete_files:
            pending.cleanup_plans = self._collection_delete_cleanup_plans(
                tree_tasks
            )

        # Pending-delete intent is runtime-only until the tree deletion can be
        # committed atomically. Persisting ``canceling`` without persisting the
        # matching delete intent used to turn an interrupted deletion into a
        # collection of unrelated paused tasks after restart.
        for task in tree_tasks:
            if task.id != root_id and task.status in DOWNLOAD_TERMINAL_STATUSES:
                continue
            task.status = "canceling"
            task.stage = "canceling"
            task.stage_text = "正在取消并删除合集"
            task.pause_requested = False
            task.cancel_requested = True
            self._sync_task_indexes(task)
            self._progress_persistence.pending.pop(task.id, None)
            self._progress_persistence.persisted_at[task.id] = time.monotonic()
            self.task_updated.emit(task)

        tree_ids = {task.id for task in tree_tasks}
        self.queue.remove_all(tree_ids)
        self._pending_collection_deletes[root_id] = pending
        for child in active_children:
            self._collection_delete_root_by_child[child.id] = root_id
            conversion_worker = self.conversion_workers.get(child.id)
            if conversion_worker is not None:
                conversion_worker.cancel()
            download_worker = self.workers.get(child.id)
            if download_worker is not None:
                download_worker.cancel("delete")
        self._best_effort_service_log(
            root_id,
            "info",
            "用户操作",
            "已请求取消运行中的合集并等待全部子任务退出",
            active_count=len(active_children),
            delete_files=bool(delete_files),
        )

    def _try_finish_pending_collection_delete(self, root_task_id: str) -> None:
        pending = self._pending_collection_deletes.get(root_task_id)
        if pending is None:
            return
        tree_tasks = self._collection_tree_tasks(root_task_id)
        if any(
            task.id in self.workers or task.id in self.conversion_workers
            for task in tree_tasks
        ):
            return
        try:
            self._remove_collection_task_tree(
                root_task_id,
                pending.delete_files,
                cleanup_plans=pending.cleanup_plans,
            )
        except Exception as exc:
            # Keep the coordinated delete intent and every task record. A
            # repeated user action can retry the durable tree deletion without
            # restarting already-canceled workers.
            self._best_effort_service_log(
                root_task_id,
                "error",
                "数据库/删除",
                "运行中的合集已停止，但删除任务记录失败，可重新删除",
                error=str(exc),
            )

    def _collection_delete_cleanup_plans(
        self,
        tree_tasks: list[DownloadTask],
    ) -> dict[str, tuple[set[Path], Path]]:
        return {
            task.id: self._task_file_cleanup_plan(task)
            for task in tree_tasks
            if task.task_kind == "video"
        }

    def _remove_collection_task_tree(
        self,
        root_task_id: str,
        delete_files: bool,
        *,
        cleanup_plans: Mapping[str, tuple[set[Path], Path]] | None = None,
    ) -> None:
        tree_tasks = self._collection_tree_tasks(root_task_id)
        root = self.tasks.get(root_task_id)
        if root is None:
            return
        prepared_cleanup_plans = dict(cleanup_plans or {})
        if delete_files:
            for task in tree_tasks:
                if (
                    task.task_kind == "video"
                    and task.id not in prepared_cleanup_plans
                ):
                    prepared_cleanup_plans[task.id] = (
                        self._task_file_cleanup_plan(task)
                    )

        # Idle collection trees can be removed in one durable transaction.
        # This also clears descendants that are still deferred during startup
        # restore and therefore do not yet have an in-memory task object.
        self.db.delete_download_task_tree(
            root_task_id,
            delete_media=delete_files,
        )
        self._pending_collection_deletes.pop(root_task_id, None)
        for task_id, pending_root_id in tuple(self._collection_delete_root_by_child.items()):
            if pending_root_id == root_task_id:
                self._collection_delete_root_by_child.pop(task_id, None)
        outer_parent_id = root.parent_task_id
        removed_tasks = [
            task
            for task in tree_tasks
            if self._forget_task_runtime_state(task.id) is not None
        ]
        for task in removed_tasks:
            plan = prepared_cleanup_plans.get(task.id)
            if plan is not None:
                self._delete_planned_task_files(task, *plan)
            self._cleanup_task_processing_workspaces(task)
            self.task_deleted.emit(task.id)
        if outer_parent_id:
            self._schedule_collection_refresh(outer_parent_id)

    def _remove_task_record(self, task_id: str, delete_files: bool = False) -> None:
        task = self.tasks.get(task_id)
        if not task:
            return
        cleanup_files: set[Path] = set()
        output_root = Path(task.output_dir)
        if delete_files:
            cleanup_files, output_root = self._task_file_cleanup_plan(task)

        # Commit the durable removal before changing in-memory indexes or
        # deleting irreversible filesystem data. If SQLite is busy/fails, the
        # task remains fully visible and retryable and no user file is touched.
        self.db.delete_download_task(
            task_id,
            source_url=task.url,
            media_path=task.media_path,
            delete_media=delete_files,
        )
        # A throttled progress write may still be queued when the user deletes
        # a paused/active task. Drop it only after the durable delete commits;
        # otherwise a database failure would make the visible task disappear.
        self._forget_task_runtime_state(task_id)
        if delete_files:
            self._delete_planned_task_files(task, cleanup_files, output_root)
        self._cleanup_task_processing_workspaces(task)
        self.task_deleted.emit(task_id)
        if task.parent_task_id:
            self._schedule_collection_refresh(task.parent_task_id)

    def discard_task(self, task_id: str) -> None:
        """Cancel a task that is still in pre-download format selection.

        Closing the format picker means the user only inspected formats; it
        must not leave a cancelled task in the persistent download list.
        The worker is allowed to unwind first, then its DB row/card is removed
        from _thread_finished.
        """
        task = self.tasks.get(task_id)
        if not task:
            return
        worker = self.workers.get(task_id)
        if worker:
            self._discard_tasks.add(task_id)
            worker.cancel("discard")
            return
        self._remove_task_record(task_id, False)

    def set_format_selector(self, task_id: str, selector: str) -> bool:
        return self.set_format_selection(task_id, {"selector": selector})

    def set_format_selection(self, task_id: str, selection: Mapping[str, Any]) -> bool:
        task = self.tasks.get(task_id)
        worker = self.workers.get(task_id)
        if not task or not worker:
            return False
        selector = str(selection.get("selector") or "")
        content_mode = str(selection.get("content_mode") or "")
        audio_format = str(selection.get("audio_format") or "")
        with self._durable_task_mutation(task):
            task.format_selector = selector
            if content_mode or audio_format:
                selected_options = DownloadOptions.from_mapping(task.options_json).to_dict()
                if content_mode:
                    selected_options["content_mode"] = content_mode
                if audio_format:
                    selected_options["audio_format"] = audio_format
                task.options_json = DownloadOptions.from_mapping(selected_options).to_dict()
            task.error = ""
            task.pause_requested = False
            task.cancel_requested = False
            task.status = "downloading" if selector else "canceled"
            task.stage = "downloading" if selector else "canceled"
            task.stage_text = "正在准备下载视频和音频" if selector else "已取消"
        self.task_updated.emit(task)
        self._best_effort_service_log(
            task_id,
            "info",
            "用户操作",
            "已选择视频格式" if selector else "已取消格式选择",
            selector=selector,
        )
        if selector:
            self.task_progress.emit(task_id, {"status": "downloading", "format_selected": True})
        worker.set_format_selector(
            selector,
            content_mode=content_mode,
            audio_format=audio_format,
        )
        return True

    def _take_next_queued_task(self) -> DownloadTask | None:
        """Discard stale queue entries and return the next runnable task."""

        return self.queue.take_next(
            self.tasks.get,
            lambda task: task.status == "queued",
        )

    def _drop_missing_queued_task(
        self,
        task: DownloadTask,
        error: LookupError,
    ) -> QueueStartOutcome:
        """Honor a missing durable row without blocking later queue items."""

        parent_id = str(task.parent_task_id or "")
        self._forget_task_runtime_state(task.id)
        self._best_effort_service_log(
            task.id,
            "warning",
            "数据库/持久化",
            "排队任务记录已不存在，已从内存队列移除",
            error=str(error),
        )
        self.task_deleted.emit(task.id)
        if parent_id:
            self._schedule_collection_refresh(parent_id)
        return QueueStartOutcome.DROPPED

    def _mark_download_task_starting(
        self,
        task: DownloadTask,
    ) -> QueueStartOutcome:
        previous = (task.status, task.stage, task.stage_text, task.stage_progress)
        task.status = "downloading"
        task.stage = "parsing"
        task.stage_text = "正在解析视频信息"
        task.stage_progress = 0.0
        try:
            self._persist(task)
        except LookupError as exc:
            return self._drop_missing_queued_task(task, exc)
        except Exception as exc:
            (
                task.status,
                task.stage,
                task.stage_text,
                task.stage_progress,
            ) = previous
            self._sync_task_indexes(task)
            # Rotate a persistently failing row behind its peers. The retry
            # remains rate-limited, while one bad record cannot monopolize
            # the queue forever.
            self.queue.requeue_back(task.id)
            self._best_effort_service_log(
                task.id,
                "error",
                "数据库/持久化",
                "保存任务启动状态失败，任务仍在队列中等待重试",
                error=str(exc),
            )
            QTimer.singleShot(1000, self._start_next)
            return QueueStartOutcome.RETRY_LATER
        self.task_updated.emit(task)
        return QueueStartOutcome.READY

    def _create_download_runtime(self, task: DownloadTask) -> _DownloadRuntime:
        disk_lease = DiskReservationLease(self.disk_capacity_manager)
        worker = DownloadWorker(
            task.id,
            task.url,
            task.output_dir,
            self.db,
            proxy=task.proxy,
            cookie_file=task.cookie_file,
            quality=task.quality,
            filename_template=task.filename_template,
            ffmpeg_path=task.ffmpeg_path,
            format_selector=task.format_selector,
            download_album=task.download_album,
            playlist_mode=task.playlist_mode,
            request_delay=self.request_delay,
            fragment_concurrent=self.fragment_concurrent,
            cookie_source=task.cookie_source,
            cookie_browser=task.cookie_browser,
            cookie_profile=task.cookie_profile,
            cookie_keyring=task.cookie_keyring,
            cookie_container=task.cookie_container,
            disk_lease=disk_lease,
            ytdlp_core_mode=self.ytdlp_core_mode,
            deno_path=self.deno_path,
            ffprobe_path=self.ffprobe_path,
            ytdlp_ejs_source=self.ytdlp_ejs_source,
            transcode_codec=task.transcode_codec,
            transcode_device=task.transcode_device,
            transcode_encoder=task.transcode_encoder,
            subtitle_language=task.subtitle_language,
            cover_convert_jpeg=self.cover_convert_jpeg,
            cover_jpeg_quality=self.cover_jpeg_quality,
            options_json=task.options_json,
            log_service=self.logs,
        )
        thread = QThread()
        try:
            thread.setProperty("download_task_id", task.id)
            worker.moveToThread(thread)
            self._connect_download_runtime(thread, worker)
        except Exception:
            delete_unstarted_worker(worker, thread)
            raise
        return _DownloadRuntime(thread, worker, disk_lease)

    def _connect_download_runtime(
        self,
        thread: QThread,
        worker: DownloadWorker,
    ) -> None:
        thread.started.connect(worker.run)
        # PySide Python callables do not consistently get an automatic queued
        # boundary. Force worker-to-service transitions onto the GUI thread
        # before touching QTimer, SQLite-backed state or UI signals.
        worker.progress.connect(self._on_progress, Qt.QueuedConnection)
        worker.formats_ready.connect(self._on_formats_ready, Qt.QueuedConnection)
        worker.playlist_info.connect(self._on_playlist_info, Qt.QueuedConnection)
        worker.completed.connect(self._on_media_completed, Qt.QueuedConnection)
        worker.failed.connect(self._on_failed, Qt.QueuedConnection)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._thread_finished_from_signal, Qt.QueuedConnection)

    def _register_download_runtime(
        self,
        task_id: str,
        runtime: _DownloadRuntime,
    ) -> None:
        self.threads[task_id] = runtime.thread
        self.workers[task_id] = runtime.worker
        self._disk_leases[task_id] = runtime.disk_lease
        if self.active_task_id is None:
            self.active_task_id = task_id
            self.thread = runtime.thread
            self.worker = runtime.worker

    def _finalize_download_start_failure(
        self,
        task: DownloadTask,
        error: Exception,
        runtime: _DownloadRuntime | None,
    ) -> None:
        if runtime is not None:
            delete_unstarted_worker(runtime.worker, runtime.thread)
        failure_message = f"无法启动下载线程：{error}"
        try:
            self._on_failed(task.id, failure_message)
        except Exception as state_error:
            # Persistence or diagnostic logging can fail independently of Qt
            # thread creation. The dead runtime must still release its slot
            # and capacity lease so later queued tasks can start.
            task.status = "failed"
            task.error = failure_message
            task.stage = "failed"
            task.stage_text = f"下载失败：{failure_message[:120]}"
            task.speed_bps = 0.0
            task.speed = ""
            task.eta = ""
            task.speed_samples.clear()
            self._sync_task_indexes(task)
            self.task_updated.emit(task)
            self._best_effort_service_log(
                task.id,
                "error",
                "数据库/持久化",
                "下载线程启动失败，且保存失败状态时发生异常",
                error=str(state_error),
                start_error=str(error),
            )
        finally:
            if runtime is not None:
                self._release_finished_download_capacity(task.id)
                self._detach_download_runtime(task.id)
        self._best_effort_service_log(
            task.id,
            "error",
            "状态",
            "任务结束：failed",
            error=task.error,
        )
        self._best_effort_service_log_flush(task.id)
        self.task_finished.emit(task.id, task.status, task.error)

    def _best_effort_service_log(
        self,
        task_id: str,
        level: str,
        category: str,
        message: str,
        **details: Any,
    ) -> None:
        try:
            self.logs.write(task_id, level, category, message, **details)
        except Exception:
            pass

    def _best_effort_service_log_flush(self, task_id: str) -> None:
        try:
            self.logs.flush(task_id)
        except Exception:
            pass

    def _accept_download_worker_signal(
        self,
        task_id: str,
        *,
        allow_finished_runtime: bool = False,
    ) -> bool:
        return download_runtime_signal_is_current(
            self.sender(),
            self.workers.get(task_id),
            allow_finished_runtime=allow_finished_runtime,
        )

    def _start_next(self) -> None:
        if self._shutting_down:
            return
        while len(self.workers) < self.max_concurrent:
            task = self._take_next_queued_task()
            if task is None:
                return
            start_outcome = self._mark_download_task_starting(task)
            if start_outcome is QueueStartOutcome.RETRY_LATER:
                return
            if start_outcome is QueueStartOutcome.DROPPED:
                continue
            runtime: _DownloadRuntime | None = None
            try:
                runtime = self._create_download_runtime(task)
                self._register_download_runtime(task.id, runtime)
                runtime.thread.start()
            except Exception as exc:
                self._finalize_download_start_failure(task, exc, runtime)

    @Slot(str, object)
    def _on_formats_ready(self, task_id: str, payload: dict) -> None:
        if not self._accept_download_worker_signal(task_id):
            return
        task = self.tasks.get(task_id)
        if task:
            if payload.get("title"):
                task.title = payload["title"]
            if payload.get("thumbnail_path"):
                task.thumbnail_path = payload["thumbnail_path"]
            task.status = "waiting_selection"
            task.stage = "waiting_selection"
            task.stage_text = "等待选择下载内容或格式"
            if not self._persist_progress(task, force=True):
                return
            self.task_updated.emit(task)
            self.formats_ready.emit(task_id, payload)

    @Slot(str, object)
    def _on_playlist_info(self, task_id: str, payload: dict) -> None:
        if not self._accept_download_worker_signal(task_id):
            return
        task = self.tasks.get(task_id)
        if not task:
            return
        if payload.get("is_playlist"):
            count = int(payload.get("count") or 0)
            base = payload.get("title") or task.title or "播放列表"
            task.title = f"{base}（共 {count} 个视频）" if count else f"{base}（播放列表）"
            if not self._persist_progress(task, force=True):
                return
            self.task_updated.emit(task)
        self.playlist_info.emit(task_id, payload)

    @Slot(str, object)
    def _on_media_completed(self, task_id: str, media: MediaItem) -> None:
        if not self._accept_download_worker_signal(
            task_id,
            allow_finished_runtime=True,
        ):
            return
        task = self.tasks.get(task_id)
        if task:
            task.title = media.title or task.title
            task.media_path = media.video_path
            task.thumbnail_path = media.thumbnail_path
            task.uploader = media.uploader or ""
            task.downloaded_at = media.downloaded_at or ""
            if task.total_bytes <= 0 and media.video_path:
                try:
                    completed_size = max(0, Path(media.video_path).stat().st_size)
                except OSError:
                    completed_size = 0
                if completed_size:
                    task.total_bytes = completed_size
                    task.downloaded_bytes = completed_size
            # DownloadWorker already committed all media rows and the task's
            # completed state in one SQLite transaction.  Keep the in-memory
            # model aligned without writing the old "downloading" state back
            # over that durable commit while queued worker signals drain.
            task.status = "completed"
            task.pause_requested = False
            task.cancel_requested = False
            task.progress = 100.0
            task.error = ""
            task.stage = "completed"
            task.stage_text = (
                f"下载完成；{task.completion_warning}"
                if task.completion_warning else "下载完成"
            )
            task.stage_progress = 100.0
            task.reconnect_message = ""
            task.speed_bps = 0.0
            task.speed = ""
            task.eta = ""
            task.speed_samples.clear()
            self._sync_task_indexes(task)
        self.task_media_completed.emit(task_id, media)
        if task and task.parent_task_id:
            self._refresh_collection(task.parent_task_id)

    @staticmethod
    def _progress_int(value: Any, default: int = 0) -> int:
        return non_negative_int(value, default)

    @staticmethod
    def _progress_float(value: Any, default: float = 0.0) -> float:
        return non_negative_float(value, default)

    @staticmethod
    def _progress_info(data: Mapping[str, Any]) -> Mapping[str, Any]:
        info = data.get("info_dict")
        return info if isinstance(info, Mapping) else {}

    def _apply_resolved_transfer_identity(
        self,
        task: DownloadTask,
        info: Mapping[str, Any],
    ) -> bool:
        """Resolve aliases before applying mutable progress to a task.

        A duplicate worker may emit several queued callbacks while it is
        unwinding. Only the source identity needed for coalescing belongs on
        that temporary task; counters, stages and storage previews must remain
        owned by the canonical task.
        """

        if not task.source_key:
            task.source_key = media_source_key(info)
        title = info.get("title")
        if title:
            task.title = str(title)
        canonical_task_id = self._coalesce_resolved_duplicates(task)
        return canonical_task_id == task.id and task.id not in self._discard_tasks

    def _apply_storage_preview(self, task: DownloadTask, data: Mapping[str, Any]) -> bool:
        preview = data.get("storage_preview")
        if not isinstance(preview, Mapping):
            return False
        normalized = {
            "known": bool(preview.get("known")),
            "temporary_bytes": self._progress_int(preview.get("temporary_bytes")),
            "final_bytes": self._progress_int(preview.get("final_bytes")),
            "entry_count": self._progress_int(preview.get("entry_count")),
            "merge_entry_count": self._progress_int(preview.get("merge_entry_count")),
            "temporary_dir": str(preview.get("temporary_dir") or ""),
            "final_dir": str(preview.get("final_dir") or ""),
            "cross_volume": bool(preview.get("cross_volume")),
        }
        if task.options_json.get("_storage_preview") == normalized:
            return False
        task.options_json["_storage_preview"] = normalized
        return True

    def _apply_stage_progress(self, task: DownloadTask, data: Mapping[str, Any]) -> bool:
        merged = merge_stage_progress(
            StageProgressState(
                stage=str(task.stage or "queued"),
                stage_text=str(task.stage_text or ""),
                stage_progress=self._progress_float(task.stage_progress),
                retry_count=self._progress_int(task.retry_count),
                retry_total=self._progress_int(task.retry_total),
                reconnect_message=str(task.reconnect_message or ""),
                elapsed_seconds=self._progress_float(task.elapsed_seconds),
                stage_elapsed_seconds=self._progress_float(task.stage_elapsed_seconds),
                transcode_encoder=str(task.current_transcode_encoder or ""),
            ),
            data,
        )
        state = merged.state
        task.stage = state.stage
        task.stage_text = state.stage_text
        task.stage_progress = state.stage_progress
        task.retry_count = state.retry_count
        task.retry_total = state.retry_total
        task.reconnect_message = state.reconnect_message
        task.elapsed_seconds = state.elapsed_seconds
        task.stage_elapsed_seconds = state.stage_elapsed_seconds
        task.current_transcode_encoder = state.transcode_encoder
        if "completion_warning" in data:
            task.completion_warning = str(data.get("completion_warning") or "")

        if merged.reset_transfer_rate:
            # A different stage or stream invalidates the previous rolling
            # network estimate. Keeping it also pollutes collection speed.
            task.speed_bps = 0.0
            task.speed = ""
            task.eta = ""
            task.speed_samples.clear()
        return merged.stage_changed

    def _apply_transfer_counters(self, task: DownloadTask, data: Mapping[str, Any]) -> None:
        counters = merge_transfer_counters(
            TransferCounterState(
                progress=task.progress,
                downloaded_bytes=task.downloaded_bytes,
                total_bytes=task.total_bytes,
                visible_progress=task.visible_progress,
                visible_downloaded_bytes=task.visible_downloaded_bytes,
                visible_total_bytes=task.visible_total_bytes,
            ),
            data,
        )
        task.progress = counters.progress
        task.downloaded_bytes = counters.downloaded_bytes
        task.total_bytes = counters.total_bytes
        task.visible_progress = counters.visible_progress
        task.visible_downloaded_bytes = counters.visible_downloaded_bytes
        task.visible_total_bytes = counters.visible_total_bytes

    def _apply_selected_transfer_quality(
        self,
        task: DownloadTask,
        data: Mapping[str, Any],
        info: Mapping[str, Any],
    ) -> None:
        quality_text = str(
            data.get("selected_quality") or selected_video_quality(info)
        ).strip()
        if quality_text:
            task.selected_quality = quality_text

    def _apply_transfer_rate(self, task: DownloadTask, data: Mapping[str, Any]) -> None:
        raw_speed = optional_non_negative_float(data.get("speed"))
        if raw_speed:
            valid_samples = [
                sample
                for value in task.speed_samples
                if (sample := optional_non_negative_float(value)) is not None
                and sample > 0.0
            ]
            valid_samples.append(raw_speed)
            task.speed_samples.clear()
            sample_limit = task.speed_samples.maxlen or 6
            task.speed_samples.extend(valid_samples[-sample_limit:])
            task.speed_bps = sum(task.speed_samples) / len(task.speed_samples)
        else:
            task.speed_bps = self._progress_float(task.speed_bps)
        speed_text = format_speed(task.speed_bps) or data.get("_speed_str") or ""
        if speed_text:
            task.speed = str(speed_text)
            task.visible_speed = str(speed_text)
        remaining = max(
            0,
            self._progress_int(task.total_bytes)
            - self._progress_int(task.downloaded_bytes),
        )
        eta_text = (
            format_eta(remaining / task.speed_bps)
            if task.speed_bps > 0 and remaining > 0
            else data.get("_eta_str") or ""
        )
        if eta_text:
            task.eta = str(eta_text)
            task.visible_eta = str(eta_text)

    @staticmethod
    def _apply_transfer_size(task: DownloadTask, data: Mapping[str, Any]) -> None:
        size_text = data.get("_total_bytes_str") or data.get("_total_bytes_estimate_str") or ""
        if size_text:
            task.size = str(size_text)
            task.visible_size = str(size_text)

    def _apply_stream_progress(self, task: DownloadTask, data: Mapping[str, Any]) -> None:
        stream_kind = data.get("stream_kind")
        if stream_kind not in {"video", "audio"}:
            return
        attribute = "video_progress" if stream_kind == "video" else "audio_progress"
        current = getattr(task, attribute, 0.0)
        setattr(task, attribute, merge_stream_progress(current, data.get("stream_progress")))

    @staticmethod
    def _apply_transfer_paths(task: DownloadTask, data: Mapping[str, Any]) -> None:
        if data.get("thumbnail_path"):
            task.thumbnail_path = str(data["thumbnail_path"])
        if data.get("filename"):
            task.current_filename = str(data.get("filename"))

    def _apply_transfer_details(
        self,
        task: DownloadTask,
        data: Mapping[str, Any],
        info: Mapping[str, Any],
    ) -> None:
        self._apply_selected_transfer_quality(task, data, info)
        self._apply_transfer_rate(task, data)
        self._apply_transfer_size(task, data)
        self._apply_stream_progress(task, data)
        self._apply_transfer_paths(task, data)

    @Slot(str, object)
    def _on_progress(self, task_id: str, data: dict) -> None:
        if not self._accept_download_worker_signal(task_id):
            return
        task = self.tasks.get(task_id)
        if not task:
            return
        info = self._progress_info(data)
        if not self._apply_resolved_transfer_identity(task, info):
            return
        force_persist = self._apply_storage_preview(task, data)
        stage_changed = self._apply_stage_progress(task, data)
        self._apply_transfer_counters(task, data)
        self._apply_transfer_details(task, data, info)
        now = time.monotonic()
        transfer_finished = data.get("status") == "finished"
        # ``task.progress`` remains at 100 while FFmpeg merges/transcodes.
        # Forcing a synchronous SQLite commit for every local-processing
        # callback can stall the GUI badly when another task is downloading.
        # Only a real transfer-finished event or durable option change needs
        # an immediate write; ordinary stage progress uses the 900 ms batch.
        self._persist_progress(task, force=force_persist or transfer_finished)
        last_emit = self._last_progress_emit.get(task_id, 0.0)
        # Stage transitions must never be swallowed by progress throttling: a
        # transition can be followed by a long blocking disk wait or local
        # verification with no further callbacks, which otherwise looks like
        # the application froze on the previous stage.
        if now - last_emit >= 0.15 or stage_changed or force_persist or transfer_finished:
            self._last_progress_emit[task_id] = now
            self.task_progress.emit(task_id, data)
        if task.parent_task_id:
            self._schedule_collection_refresh(task.parent_task_id)

    @Slot(str, str)
    def _on_failed(self, task_id: str, error: str) -> None:
        if not self._accept_download_worker_signal(
            task_id,
            allow_finished_runtime=True,
        ):
            return
        task = self.tasks.get(task_id)
        if task is None:
            return
        already_finalized = (
            task_id not in self.workers
            and task_id not in self.threads
            and task_id not in self._disk_leases
            and task.status in DOWNLOAD_TERMINAL_STATUSES
        )
        task.error = error
        task.status = "failed"
        task.stage = "failed"
        task.stage_text = f"下载失败：{error[:120]}"
        task.speed_bps = 0.0
        task.speed = ""
        task.eta = ""
        task.speed_samples.clear()
        self._persist(task)
        self.task_updated.emit(task)
        category = DownloadLogService.classify_error(error)
        self._best_effort_service_log(
            task_id,
            "error",
            category,
            "下载服务报告失败",
            error=error,
        )
        # Normally the QThread-finished handler is deferred one event-loop
        # turn, so this signal wins first. If a foreign/manual teardown has
        # already finalized the run, publish the correction instead of
        # leaving observers with a false completed state.
        if already_finalized:
            self.task_finished.emit(task_id, task.status, task.error)
        if task.parent_task_id:
            self._refresh_collection(task.parent_task_id)

    @Slot()
    def _thread_finished_from_signal(self) -> None:
        thread = self.sender()
        if not isinstance(thread, QThread):
            return
        task_id = str(thread.property("download_task_id") or "")
        if task_id:
            self._defer_thread_finished(task_id, thread)

    def _defer_thread_finished(
        self,
        task_id: str,
        thread: QThread | None = None,
    ) -> None:
        """Let queued worker outcome signals settle before deriving status.

        ``worker.failed`` and ``QThread.finished`` have different senders, so
        Qt does not promise their queued callbacks will be delivered in the
        order the worker emitted them. One GUI event-loop turn closes that
        race without blocking shutdown or the next download slot.
        """

        owned_thread = thread or self.threads.get(task_id)
        if not isinstance(owned_thread, QThread):
            return
        if owned_thread in self._deferred_thread_finishes:
            return
        self._deferred_thread_finishes.add(owned_thread)
        QTimer.singleShot(
            0,
            partial(
                self._complete_deferred_thread_finish,
                task_id,
                owned_thread,
            ),
        )

    def _complete_deferred_thread_finish(
        self,
        task_id: str,
        thread: QThread,
    ) -> None:
        self._deferred_thread_finishes.discard(thread)
        if self.threads.get(task_id) is thread:
            self._thread_finished(task_id, expected_thread=thread)
        try:
            thread.deleteLater()
        except RuntimeError:
            pass

    def _detach_download_runtime(self, task_id: str) -> None:
        """Remove one finished worker run from all runtime-only indexes."""

        self.threads.pop(task_id, None)
        self.workers.pop(task_id, None)
        self._last_progress_emit.pop(task_id, None)
        self._progress_persistence.forget(task_id)
        if self.active_task_id != task_id:
            return
        self.active_task_id = next(iter(self.workers), None)
        self.thread = self.threads.get(self.active_task_id) if self.active_task_id else None
        self.worker = self.workers.get(self.active_task_id) if self.active_task_id else None

    def _release_download_capacity_lease(
        self,
        task_id: str,
        lease: DiskReservationLease,
    ) -> None:
        try:
            released = lease.release_all()
        except Exception as exc:
            self._best_effort_service_log(
                task_id,
                "warning",
                "磁盘/存储",
                "线程结束释放磁盘容量预留失败，将自动重试",
                error=str(exc),
                remaining=lease.active_count,
            )
            if not self._shutting_down:
                QTimer.singleShot(
                    1000,
                    partial(self._release_download_capacity_lease, task_id, lease),
                )
            return
        if released:
            self._best_effort_service_log(
                task_id,
                "warning",
                "磁盘/存储",
                "线程结束兜底释放了磁盘容量预留",
                released=released,
            )

    def _release_finished_download_capacity(self, task_id: str) -> None:
        # The worker normally releases from its finally block. Keep a second
        # idempotent release outside the QObject lifetime so even an unusual
        # teardown path cannot strand a same-volume reservation forever.
        # Pop the mapping first: a retry reuses the task id and must be free to
        # register a new lease while any failed old release is retried through
        # the callback's direct object reference.
        lease = self._disk_leases.pop(task_id, None)
        if lease is not None:
            self._release_download_capacity_lease(task_id, lease)

    def _complete_download_runtime_cleanup(
        self,
        task_id: str,
        *,
        collection_delete_root: str = "",
    ) -> None:
        self._detach_download_runtime(task_id)
        if collection_delete_root:
            self._try_finish_pending_collection_delete(collection_delete_root)
        self._start_pending_runtime_retry(task_id)
        self._start_next()

    def _handle_finished_download_removal(
        self,
        task_id: str,
        task: DownloadTask | None,
    ) -> bool:
        root_task_id = self._collection_delete_root_by_child.pop(task_id, "")
        if root_task_id:
            self._complete_download_runtime_cleanup(
                task_id,
                collection_delete_root=root_task_id,
            )
            return True
        if task is None:
            self._complete_download_runtime_cleanup(task_id)
            return True
        if task_id in self._pending_deletes:
            delete_files = self._pending_deletes.pop(task_id)
            try:
                self._remove_task_record(task_id, delete_files)
            except Exception as exc:
                # The worker is already gone, so never strand its concurrency
                # slot because SQLite was temporarily busy. Keep the visible
                # task record for an explicit retry and finish normal cleanup.
                self._best_effort_service_log(
                    task_id,
                    "error",
                    "数据库/删除",
                    "下载已停止，但删除任务记录失败，可重新删除",
                    error=str(exc),
                )
                return False
            self._complete_download_runtime_cleanup(task_id)
            return True
        if task_id in self._discard_tasks:
            self._discard_tasks.discard(task_id)
            try:
                self._remove_task_record(task_id, False)
            except Exception as exc:
                self._best_effort_service_log(
                    task_id,
                    "error",
                    "数据库/删除",
                    "关闭格式选择后删除临时任务失败，记录已保留",
                    error=str(exc),
                )
                return False
            self._complete_download_runtime_cleanup(task_id)
            return True
        return False

    @staticmethod
    def _apply_finished_download_state(task: DownloadTask) -> None:
        state = finished_download_state(
            status=task.status,
            error=task.error,
            pause_requested=task.pause_requested,
            cancel_requested=task.cancel_requested,
            completion_warning=task.completion_warning,
        )
        task.status = state.status
        task.error = state.error
        task.stage = state.stage
        task.stage_text = state.stage_text
        if state.progress is not None:
            task.progress = state.progress
        task.stage_progress = state.stage_progress
        task.reconnect_message = state.reconnect_message
        task.speed_bps = 0.0
        task.speed = ""
        task.eta = ""
        task.speed_samples.clear()

    def _publish_finished_download_state(self, task: DownloadTask) -> bool:
        task_id = task.id
        try:
            self._persist(task)
        except LookupError as exc:
            # A removed database row is authoritative. Never recreate it from
            # a late worker callback or leave a visible in-memory ghost.
            self._unregister_task(task_id)
            self._best_effort_service_log(
                task_id,
                "warning",
                "数据库/持久化",
                "任务记录已不存在，线程结果不会重新创建该任务",
                error=str(exc),
            )
            self._best_effort_service_log_flush(task_id)
            self.task_deleted.emit(task_id)
            return False
        except Exception as exc:
            # The worker is already stopped. Keep the truthful in-memory
            # outcome and release its slot even if SQLite is temporarily busy.
            self._sync_task_indexes(task)
            self._best_effort_service_log(
                task_id,
                "error",
                "数据库/持久化",
                "保存任务最终状态失败，已保留当前界面状态",
                error=str(exc),
                status=task.status,
            )
        self._best_effort_service_log(
            task_id,
            "info" if task.status in {"completed", "paused", "canceled"} else "error",
            "状态",
            f"任务结束：{task.status}",
            error=task.error if task.error else "",
        )
        self._best_effort_service_log_flush(task_id)
        self.task_updated.emit(task)
        self.task_finished.emit(task_id, task.status, task.error)
        if task.parent_task_id:
            self._refresh_collection(task.parent_task_id)
        return True

    def _thread_finished(
        self,
        task_id: str,
        *,
        expected_thread: QThread | None = None,
    ) -> None:
        current_thread = self.threads.get(task_id)
        if expected_thread is not None and current_thread is not expected_thread:
            return
        task = self.tasks.get(task_id)
        runtime_active = (
            task_id in self.threads
            or task_id in self.workers
            or task_id in self._disk_leases
        )
        if not runtime_active and (
            task is None or task.status in DOWNLOAD_TERMINAL_STATUSES
        ):
            return
        if isinstance(current_thread, QThread):
            self._deferred_thread_finishes.discard(current_thread)
        self._release_finished_download_capacity(task_id)
        if self._handle_finished_download_removal(task_id, task):
            return
        try:
            if task is not None:
                self._apply_finished_download_state(task)
                self._publish_finished_download_state(task)
        finally:
            self._complete_download_runtime_cleanup(task_id)

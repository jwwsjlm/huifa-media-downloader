from __future__ import annotations

import json
import subprocess
import sys
import threading
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

from PySide6.QtCore import QObject, Signal, Slot

from app.core.download_options import DownloadOptions
from app.core.download_progress import non_negative_float
from app.core.media_identity import normalize_media_title, normalize_source_url
from app.core.download_service import (
    ytdlp_ejs_runtime_options,
    ytdlp_runtime_path,
)
from app.core.external_ytdlp import (
    build_external_ytdlp_command,
    cached_external_ytdlp_version,
    finish_external_ytdlp_output_reader,
    pump_external_ytdlp_output,
    start_external_ytdlp_output_reader,
    start_external_ytdlp_process,
    terminate_external_ytdlp_process,
)
from app.core.ytdlp_core_selection import (
    normalize_ytdlp_core_mode,
    select_ytdlp_core,
)

try:
    import yt_dlp
except ImportError:  # pragma: no cover
    yt_dlp = None


COLLECTION_TYPES = frozenset({"playlist", "multi_video"})
UNAVAILABLE_VALUES = frozenset({"private", "premium_only", "subscriber_only", "needs_auth", "deleted"})
_MAX_ESTIMATED_BYTES = (1 << 63) - 1


def collection_source_key(info: Mapping[str, Any]) -> str:
    extractor = str(info.get("extractor_key") or info.get("extractor") or "generic").strip().casefold()
    identifier = str(info.get("id") or info.get("display_id") or info.get("url") or "").strip()
    return f"{extractor}:{identifier}" if identifier else ""


def collection_entry_url(info: Mapping[str, Any]) -> str:
    for key in ("webpage_url", "original_url", "url"):
        value = str(info.get(key) or "").strip()
        if value:
            return value
    return ""


def _bounded_nonnegative_int(value: object) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, min(number, _MAX_ESTIMATED_BYTES))


def _playlist_index_selected(specification: str, index: int) -> bool:
    text = str(specification or "").strip()
    if not text:
        return True
    for token in text.split(','):
        token = token.strip()
        if not token:
            continue
        try:
            if '-' in token:
                start_text, end_text = token.split('-', 1)
                start = int(start_text or 1)
                end = int(end_text or index)
                if start <= index <= end:
                    return True
            elif ':' in token:
                parts = token.split(':')
                start = int(parts[0] or 1)
                end = int(parts[1]) if len(parts) > 1 and parts[1] else index
                step = int(parts[2]) if len(parts) > 2 and parts[2] else 1
                if start <= index <= end and (index - start) % max(1, step) == 0:
                    return True
            elif int(token) == index:
                return True
        except ValueError:
            continue
    return False


@dataclass(slots=True)
class CollectionEntry:
    source_key: str
    url: str
    title: str = ""
    uploader: str = ""
    duration: float = 0.0
    upload_date: str = ""
    thumbnail: str = ""
    live_status: str = ""
    availability: str = ""
    index: int = 0
    entry_kind: str = "video"
    downloadable: bool = True
    disabled_reason: str = ""
    selected: bool = True
    completed: bool = False
    estimated_bytes: int = 0
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("raw", None)
        return value


@dataclass(slots=True)
class CollectionProbeRequest:
    request_id: str
    url: str
    core_mode: str = "auto"
    proxy: str = ""
    cookie_file: str = ""
    cookie_source: str = "none"
    cookie_browser: str = "chrome"
    cookie_profile: str = ""
    cookie_keyring: str = ""
    cookie_container: str = ""
    deno_path: str = ""
    ytdlp_ejs_source: str = "auto"
    options: dict[str, Any] = field(default_factory=dict)
    completed_source_keys: set[str] = field(default_factory=set)
    completed_urls: set[str] = field(default_factory=set)
    completed_titles: set[str] = field(default_factory=set)
    batch_size: int = 40
    resume_index: int = 0

    def __post_init__(self) -> None:
        self.completed_urls = {
            normalized
            for value in self.completed_urls
            if (normalized := normalize_source_url(value))
        }
        self.completed_titles = {
            normalized
            for value in self.completed_titles
            if (normalized := normalize_media_title(value))
        }


@dataclass(slots=True)
class _ExternalCollectionStream:
    count: int
    first: dict[str, Any] | None = None
    emitted_count: int = 0
    batch: list[CollectionEntry] = field(default_factory=list)
    recent: deque[str] = field(default_factory=lambda: deque(maxlen=20))


class CollectionProbeWorker(QObject):
    """Stream flat collection entries without occupying a download slot."""

    metadata = Signal(str, object)
    entries = Signal(str, object)
    single = Signal(str, object)
    failed = Signal(str, str)
    finished = Signal(str, bool, int)

    def __init__(self, request: CollectionProbeRequest):
        super().__init__()
        self.request = request
        self._cancel = threading.Event()
        self._process: subprocess.Popen[str] | None = None
        self._temporary_cookie: Path | None = None

    @Slot()
    def cancel(self) -> None:
        # The worker output pump observes this within 100 ms and performs tree
        # termination in its own thread. Running taskkill here would block the
        # Qt/UI caller for up to five seconds on a slow process tree.
        self._cancel.set()

    def _cookie_options(self) -> dict[str, Any]:
        source = str(self.request.cookie_source or "none").casefold()
        if source == "embedded":
            from app.core.browser_cookies import CookieVault
            from app.core.cookie_sources import EMBEDDED_DOWNLOAD_PROFILE

            self._temporary_cookie = CookieVault().create_temporary_netscape_file(
                EMBEDDED_DOWNLOAD_PROFILE
            )
            return {"cookiefile": str(self._temporary_cookie)}
        if source == "file" and self.request.cookie_file:
            return {"cookiefile": self.request.cookie_file}
        if source == "browser":
            return {
                "cookiesfrombrowser": (
                    self.request.cookie_browser,
                    self.request.cookie_profile or None,
                    self.request.cookie_keyring or None,
                    self.request.cookie_container or None,
                )
            }
        return {}

    def _cleanup_temporary_cookie(self) -> None:
        path = self._temporary_cookie
        self._temporary_cookie = None
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            # Parsing has already reached its terminal signal. A locked temp
            # Cookie file must not prevent the owning QThread from unwinding.
            pass

    def _probe_options(self) -> dict[str, Any]:
        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": "in_playlist",
            "lazy_playlist": True,
            "ignoreerrors": True,
        }
        if self.request.resume_index > 0:
            options["playliststart"] = self.request.resume_index + 1
        if self.request.proxy:
            options["proxy"] = self.request.proxy
        options.update(self._cookie_options())
        ejs, _deno, _source = ytdlp_ejs_runtime_options(
            self.request.deno_path,
            self.request.ytdlp_ejs_source,
        )
        options.update(ejs)
        return options

    @staticmethod
    def _entry_is_collection(info: Mapping[str, Any]) -> bool:
        kind = str(info.get("_type") or "video").casefold()
        return (
            kind in COLLECTION_TYPES
            or info.get("entries") is not None
            or bool(info.get("playlist_count") or info.get("n_entries"))
        )

    def _entry_availability(
        self,
        info: Mapping[str, Any],
        *,
        is_collection: bool,
        index: int,
        options: DownloadOptions,
    ) -> tuple[str, str, bool, str]:
        url = collection_entry_url(info)
        availability = str(info.get("availability") or "").casefold()
        downloadable = bool(url) and availability not in UNAVAILABLE_VALUES
        disabled_reason = ""
        if not url:
            disabled_reason = "missing_url"
        elif availability in UNAVAILABLE_VALUES:
            disabled_reason = availability
        included, filter_reason = options.collection_match_filter(info)
        if options.first_n and index > options.first_n:
            included, filter_reason = False, "range_filtered"
        if not _playlist_index_selected(options.playlist_items, index):
            included, filter_reason = False, "range_filtered"
        if not included and not is_collection and downloadable:
            downloadable = False
            disabled_reason = filter_reason
        return url, availability, downloadable, disabled_reason

    def _entry_completed(self, info: Mapping[str, Any], url: str) -> tuple[str, bool]:
        source_key = collection_source_key(info)
        normalized_url = normalize_source_url(url)
        normalized_title = normalize_media_title(info.get("title"))
        completed = bool(
            (source_key and source_key in self.request.completed_source_keys)
            or (normalized_url and normalized_url in self.request.completed_urls)
            or (normalized_title and normalized_title in self.request.completed_titles)
        )
        return source_key, completed

    @staticmethod
    def _entry_estimated_bytes(info: Mapping[str, Any], duration: float) -> int:
        for key in ("filesize", "filesize_approx"):
            estimated_bytes = _bounded_nonnegative_int(info.get(key))
            if estimated_bytes:
                return estimated_bytes
        bitrate = non_negative_float(info.get("tbr"))
        if not duration or not bitrate:
            return 0
        return _bounded_nonnegative_int(bitrate * 1000.0 / 8.0 * duration * 1.35)

    def _entry(self, raw: Mapping[str, Any], index: int, options: DownloadOptions) -> CollectionEntry:
        info = dict(raw)
        is_collection = self._entry_is_collection(info)
        url, availability, downloadable, disabled_reason = self._entry_availability(
            info,
            is_collection=is_collection,
            index=index,
            options=options,
        )
        source_key, completed = self._entry_completed(info, url)
        duration = non_negative_float(info.get("duration"))
        estimated_bytes = self._entry_estimated_bytes(info, duration)
        return CollectionEntry(
            source_key=source_key,
            url=url,
            title=str(info.get("title") or info.get("id") or url),
            uploader=str(info.get("uploader") or info.get("channel") or info.get("creator") or ""),
            duration=duration,
            upload_date=str(info.get("upload_date") or info.get("release_date") or ""),
            thumbnail=str(info.get("thumbnail") or ""),
            live_status=str(info.get("live_status") or ("is_live" if info.get("is_live") else "")),
            availability=availability,
            index=index,
            entry_kind="collection" if is_collection else "video",
            downloadable=downloadable,
            disabled_reason=disabled_reason,
            selected=bool(downloadable and not completed),
            completed=completed,
            estimated_bytes=estimated_bytes,
            raw=info,
        )

    def _emit_batch(self, batch: list[CollectionEntry]) -> None:
        if batch:
            self.entries.emit(self.request.request_id, [item.to_dict() for item in batch])
            batch.clear()

    def _run_builtin(self, probe_options: dict[str, Any], options: DownloadOptions) -> tuple[bool, int]:
        if yt_dlp is None:
            raise RuntimeError("内置 yt-dlp 模块不可用")
        with yt_dlp.YoutubeDL(probe_options) as ydl:
            info = ydl.extract_info(self.request.url, download=False, process=False)
        if not isinstance(info, Mapping):
            raise RuntimeError("yt-dlp 未返回可解析的信息")
        info_type = str(info.get("_type") or "video").casefold()
        entries = info.get("entries")
        if info_type not in COLLECTION_TYPES and entries is None:
            self.single.emit(self.request.request_id, dict(info))
            return False, 0
        self.metadata.emit(self.request.request_id, {
            "title": str(info.get("title") or info.get("playlist_title") or self.request.url),
            "source_key": collection_source_key(info),
            "extractor_key": str(info.get("extractor_key") or info.get("extractor") or ""),
            "webpage_url": str(info.get("webpage_url") or self.request.url),
        })
        batch: list[CollectionEntry] = []
        count = max(0, int(self.request.resume_index or 0))
        iterable = entries or ()
        for raw in iterable:
            if self._cancel.is_set():
                break
            if not isinstance(raw, Mapping):
                continue
            count += 1
            batch.append(self._entry(raw, count, options))
            if len(batch) >= max(1, self.request.batch_size):
                self._emit_batch(batch)
        self._emit_batch(batch)
        return True, count

    def _emit_external_metadata(self, raw: Mapping[str, Any]) -> None:
        title = str(raw.get("playlist_title") or raw.get("playlist") or self.request.url)
        extractor_name = str(raw.get("extractor_key") or raw.get("extractor") or "")
        extractor = (extractor_name or "generic").casefold()
        playlist_id = str(raw.get("playlist_id") or "")
        self.metadata.emit(self.request.request_id, {
            "title": title,
            "source_key": f"{extractor}:{playlist_id}" if playlist_id else "",
            "extractor_key": extractor_name,
            "webpage_url": self.request.url,
        })

    def _consume_external_line(
        self,
        line: str,
        stream: _ExternalCollectionStream,
        options: DownloadOptions,
    ) -> None:
        text = line.strip()
        if not text:
            return
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            stream.recent.append(text)
            return
        if not isinstance(raw, dict):
            return
        if stream.first is None:
            stream.first = raw
            self._emit_external_metadata(raw)
        stream.count += 1
        stream.emitted_count += 1
        stream.batch.append(self._entry(raw, stream.count, options))
        if len(stream.batch) >= max(1, self.request.batch_size):
            self._emit_batch(stream.batch)

    def _external_collection_result(
        self,
        stream: _ExternalCollectionStream,
        return_code: int,
    ) -> tuple[bool, int]:
        if return_code != 0:
            detail = stream.recent[-1] if stream.recent else ""
            raise RuntimeError(detail or f"外置 yt-dlp 退出，代码 {return_code}")
        if stream.first is None:
            raise RuntimeError("外置 yt-dlp 未返回可解析的信息")
        first = stream.first
        is_collection = bool(
            first.get("playlist_id")
            or first.get("playlist_title")
            or self.request.resume_index > 0
            or stream.emitted_count > 1
        )
        if not is_collection:
            self.single.emit(self.request.request_id, first)
            return False, 0
        return True, stream.count

    def _run_external(
        self,
        executable: str,
        probe_options: dict[str, Any],
        options: DownloadOptions,
    ) -> tuple[bool, int]:
        stream_options = dict(probe_options)
        stream_options["dump_json"] = True
        command = build_external_ytdlp_command(
            executable,
            self.request.url,
            stream_options,
            download=False,
        )
        process = start_external_ytdlp_process(command)
        self._process = process
        try:
            lines, reader = start_external_ytdlp_output_reader(process)
        except BaseException:
            self._process = None
            raise
        stream = _ExternalCollectionStream(
            count=max(0, int(self.request.resume_index or 0)),
        )
        try:
            return_code = pump_external_ytdlp_output(
                process,
                reader,
                lines,
                cancel_event=self._cancel,
                consume_line=lambda line: self._consume_external_line(
                    line,
                    stream,
                    options,
                ),
            )
        except InterruptedError:
            terminate_external_ytdlp_process(process)
            return True, stream.count
        except BaseException:
            terminate_external_ytdlp_process(process)
            raise
        finally:
            self._emit_batch(stream.batch)
            finish_external_ytdlp_output_reader(process, reader)
            self._process = None

        return self._external_collection_result(stream, return_code)

    @Slot()
    def run(self) -> None:
        is_collection = False
        count = 0
        try:
            options = DownloadOptions.from_mapping(self.request.options)
            probe_options = self._probe_options()
            mode = normalize_ytdlp_core_mode(self.request.core_mode)
            executable = "" if mode == "builtin" else ytdlp_runtime_path()
            version = cached_external_ytdlp_version(executable) if executable else None
            selection = select_ytdlp_core(
                mode,
                external_executable=executable,
                external_version=version,
                builtin_available=yt_dlp is not None,
                packaged=bool(getattr(sys, "frozen", False)),
            )
            if selection.uses_external:
                is_collection, count = self._run_external(
                    selection.executable,
                    probe_options,
                    options,
                )
            else:
                is_collection, count = self._run_builtin(probe_options, options)
        except Exception as exc:
            if not self._cancel.is_set():
                self.failed.emit(self.request.request_id, str(exc))
        finally:
            try:
                self._cleanup_temporary_cookie()
            finally:
                self.finished.emit(self.request.request_id, is_collection, count)

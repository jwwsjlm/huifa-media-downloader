from __future__ import annotations

import hashlib
import re
import struct
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import requests

from PySide6.QtCore import QObject, QThread, Signal, Slot

from app.core.network_service import detect_public_ip
from app.storage.database import Database
from app.storage.models import MediaItem

try:
    import yt_dlp
except ImportError:  # pragma: no cover
    yt_dlp = None


def bundled_ffmpeg_path() -> Path:
    """Return the bundled FFmpeg matching the current Python process bitness."""
    arch = "x64" if struct.calcsize("P") * 8 >= 64 else "x86"
    return Path(__file__).resolve().parents[2] / "tools" / "ffmpeg" / arch / "ffmpeg.exe"


def format_speed(bytes_per_second: float) -> str:
    if bytes_per_second <= 0:
        return ""
    if bytes_per_second >= 1024 ** 2:
        return f"{bytes_per_second / 1024 ** 2:.2f} MiB/s"
    return f"{bytes_per_second / 1024:.0f} KiB/s"


def format_eta(seconds: float) -> str:
    if seconds <= 0:
        return ""
    seconds = int(seconds)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


@dataclass
class DownloadTask:
    id: str
    url: str
    output_dir: str
    quality: str = "best"
    download_album: bool = False
    playlist_mode: str = "auto"
    proxy: str = ""
    filename_template: str = "%(title)s [%(id)s].%(ext)s"
    ffmpeg_path: str = ""
    format_selector: str = ""
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


class DownloadWorker(QObject):
    progress = Signal(dict)
    completed = Signal(object)
    failed = Signal(str)
    finished = Signal()
    formats_ready = Signal(object)
    playlist_info = Signal(object)

    def __init__(self, url: str, output_dir: str, db: Database, proxy: str = "", cookie_file: str = "",
                 quality: str = "best", filename_template: str = "%(title)s [%(id)s].%(ext)s",
                 ffmpeg_path: str = "", format_selector: str = "", download_album: bool = False,
                 playlist_mode: str = "auto"):
        super().__init__()
        self.url, self.output_dir, self.db, self.proxy, self.cookie_file = url, output_dir, db, proxy, cookie_file
        self.quality, self.filename_template, self.ffmpeg_path = quality, filename_template, ffmpeg_path
        self.download_album = download_album
        self.playlist_mode = playlist_mode if playlist_mode in {"auto", "single", "playlist"} else ("playlist" if download_album else "single")
        self.format_selector = format_selector
        self._cancel = threading.Event()
        self._thumbnail_saved = False
        self._thumbnail_path = ""
        self._format_event = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()
        self._format_event.set()

    def set_format_selector(self, selector: str) -> None:
        self.format_selector = selector
        self._format_event.set()

    @Slot()
    def run(self) -> None:
        if yt_dlp is None:
            self.failed.emit("未安装 yt-dlp，请先 pip install -r requirements.txt")
            self.finished.emit()
            return
        Path(self.output_dir).mkdir(parents=True, exist_ok=True)

        def hook(data: dict[str, Any]) -> None:
            if self._cancel.is_set():
                raise yt_dlp.utils.DownloadError("用户取消下载")
            if not self._thumbnail_saved:
                info = data.get("info_dict") or {}
                thumbnail_url = info.get("thumbnail")
                if thumbnail_url:
                    thumbnail_path = self._save_thumbnail(thumbnail_url, info.get("id") or "video")
                    if thumbnail_path:
                        data = dict(data)
                        data["thumbnail_path"] = thumbnail_path
            self.progress.emit(data)

        formats = {
            "best": "bv*+ba/b",
            "1080p": "bv*[height<=1080]+ba/b[height<=1080]",
            "720p": "bv*[height<=720]+ba/b[height<=720]",
        }
        template = self.filename_template.strip() or "%(title)s [%(id)s].%(ext)s"
        ydl_opts: dict[str, Any] = {
            "outtmpl": str(Path(self.output_dir) / template),
            "format": formats.get(self.quality, formats["best"]),
            "merge_output_format": "mp4",
            "writethumbnail": True,
            "writeinfojson": True,
            "writesubtitles": False,
            "progress_hooks": [hook],
            "noplaylist": self.playlist_mode == "single",
            # Download independent DASH/HLS fragments concurrently as well as
            # running multiple task workers in DownloadService.
            "concurrent_fragment_downloads": 4,
            "quiet": True,
            "no_warnings": True,
        }
        bundled_ffmpeg = bundled_ffmpeg_path()
        configured_ffmpeg = Path(self.ffmpeg_path) if self.ffmpeg_path else bundled_ffmpeg
        if not configured_ffmpeg.exists():
            configured_ffmpeg = bundled_ffmpeg
        if configured_ffmpeg.exists():
            ydl_opts["ffmpeg_location"] = str(configured_ffmpeg)
        if self.proxy:
            ydl_opts["proxy"] = self.proxy
        if self.cookie_file:
            ydl_opts["cookiefile"] = self.cookie_file
        try:
            preview = None
            if self.playlist_mode == "auto" or (self.quality == "custom" and not self.format_selector):
                probe_opts = {k: v for k, v in ydl_opts.items() if k not in {"format", "progress_hooks", "writethumbnail", "writeinfojson"}}
                with yt_dlp.YoutubeDL(probe_opts) as probe:
                    preview = probe.extract_info(self.url, download=False)
                if self.playlist_mode == "auto":
                    entries = preview.get("entries") or []
                    count = preview.get("playlist_count") or preview.get("n_entries")
                    if not count:
                        try:
                            count = len(entries)
                        except TypeError:
                            count = 0
                    is_playlist = preview.get("_type") == "playlist" or bool(count and count > 1)
                    self.playlist_info.emit({"is_playlist": is_playlist, "count": int(count or 0),
                                             "title": preview.get("title") or preview.get("playlist_title") or ""})
            if self.quality == "custom" and not self.format_selector:
                if preview is None:
                    probe_opts = {k: v for k, v in ydl_opts.items() if k not in {"format", "progress_hooks", "writethumbnail", "writeinfojson"}}
                    with yt_dlp.YoutubeDL(probe_opts) as probe:
                        preview = probe.extract_info(self.url, download=False)
                format_info = preview
                if not preview.get("formats") and preview.get("entries"):
                    first_entry = next((entry for entry in preview["entries"] if entry), None)
                    if first_entry:
                        format_info = first_entry
                thumbnail_path = ""
                thumbnail_url = preview.get("thumbnail") or format_info.get("thumbnail")
                if thumbnail_url:
                    thumbnail_path = self._save_thumbnail(thumbnail_url, preview.get("id") or format_info.get("id") or "video")
                choices = self._build_format_choices(format_info)
                self.formats_ready.emit({"title": preview.get("title", ""), "thumbnail_path": thumbnail_path, "choices": choices})
                self._format_event.wait(timeout=900)
                if self._cancel.is_set():
                    raise yt_dlp.utils.DownloadError("用户取消下载")
                if not self.format_selector:
                    raise yt_dlp.utils.DownloadError("未选择视频分辨率")
                ydl_opts["format"] = self.format_selector
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(self.url, download=True)
                entries = info.get("entries") if info.get("_type") == "playlist" else [info]
                for entry in filter(None, entries):
                    requested = entry.get("requested_downloads") or []
                    filepath = entry.get("_filename") or (requested[0].get("filepath") if requested else "")
                    if not filepath:
                        filepath = ydl.prepare_filename(entry)
                    base = Path(filepath)
                    mp4 = base.with_suffix(".mp4")
                    video_path = str(mp4 if mp4.exists() else base)
                    image_suffixes = {".jpg", ".jpeg", ".png", ".webp", ".avif"}
                    thumb = Path(self._thumbnail_path) if self._thumbnail_path else None
                    if not thumb or not thumb.exists():
                        thumb = next((p for p in base.parent.glob(base.stem + ".*") if p.suffix.lower() in image_suffixes), None)
                    info_path = str(base.with_suffix(".info.json"))
                    if not Path(info_path).exists():
                        info_path = str(base.with_suffix(".json"))
                    digest = hashlib.sha256(Path(video_path).read_bytes()).hexdigest() if Path(video_path).exists() else ""
                    item = MediaItem(
                        source_url=entry.get("webpage_url") or self.url,
                        title=entry.get("title") or "",
                        description=entry.get("description") or "",
                        tags=entry.get("tags") or [],
                        uploader=entry.get("uploader") or "",
                        thumbnail_path=str(thumb or ""),
                        video_path=video_path,
                        metadata_json_path=info_path,
                        source_ip=detect_public_ip(self.proxy),
                        proxy_profile=self.proxy,
                        sha256=digest,
                    )
                    item.id = self.db.add_media(item)
                    self.completed.emit(item)
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()

    @staticmethod
    def _build_format_choices(info: dict[str, Any]) -> list[dict[str, str]]:
        choices: list[dict[str, str]] = []
        seen: set[int] = set()
        formats = sorted(info.get("formats") or [], key=lambda item: (item.get("height") or 0, item.get("fps") or 0), reverse=True)
        for item in formats:
            height = item.get("height")
            format_id = item.get("format_id")
            if not height or not format_id or height in seen or item.get("vcodec") == "none":
                continue
            seen.add(height)
            ext = item.get("ext") or "?"
            fps = item.get("fps") or ""
            acodec = item.get("acodec")
            selector = str(format_id) if acodec and acodec != "none" else f"{format_id}+bestaudio/best"
            label = f"{height}p  ·  {ext}  ·  {fps}fps"
            if item.get("format_note"):
                label += f"  ·  {item['format_note']}"
            choices.append({
                "label": label,
                "selector": selector,
                "height": int(height),
                "ext": str(ext),
                "fps": str(fps),
                "format_note": str(item.get("format_note") or ""),
                "filesize": int(item.get("filesize") or item.get("filesize_approx") or 0),
            })
        return choices

    def _save_thumbnail(self, url: str, video_id: str) -> str:
        """Fetch the cover as soon as yt-dlp exposes it, so the task card can show it."""
        safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", str(video_id))[:80] or "video"
        path = Path(self.output_dir) / f"{safe_id}.thumb.jpg"
        try:
            response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            response.raise_for_status()
            path.write_bytes(response.content)
            self._thumbnail_saved = True
            self._thumbnail_path = str(path)
            return str(path)
        except Exception:
            return ""


class DownloadService(QObject):
    task_added = Signal(object)
    task_updated = Signal(object)
    task_progress = Signal(str, dict)
    task_media_completed = Signal(str, object)
    task_finished = Signal(str, str, str)
    task_deleted = Signal(str)
    formats_ready = Signal(str, object)
    playlist_info = Signal(str, object)
    failed = Signal(str)

    def __init__(self, db: Database, max_concurrent: int = 3):
        super().__init__()
        self.db = db
        self.tasks: dict[str, DownloadTask] = {}
        self.queue: deque[str] = deque()
        self.max_concurrent = max(1, min(int(max_concurrent or 1), 8))
        self.active_task_id: str | None = None
        self.thread: QThread | None = None
        self.worker: DownloadWorker | None = None
        self.threads: dict[str, QThread] = {}
        self.workers: dict[str, DownloadWorker] = {}
        self._discard_tasks: set[str] = set()
        self._pending_deletes: dict[str, bool] = {}

    def restore_tasks(self) -> list[DownloadTask]:
        restored: list[DownloadTask] = []
        for row in self.db.list_download_tasks():
            status = row["status"]
            if status in {"downloading", "canceling", "暂停中"}:
                status = "paused"
            task = DownloadTask(
                id=row["id"], url=row["url"], output_dir=row["output_dir"], quality=row["quality"] or "best",
                download_album=bool(row["download_album"] or 0),
                playlist_mode=(row["playlist_mode"] or ("playlist" if row["download_album"] else "single")),
                proxy=row["proxy"] or "", filename_template=row["filename_template"] or "%(title)s [%(id)s].%(ext)s",
                ffmpeg_path=row["ffmpeg_path"] or "", format_selector=row["format_selector"] or "",
                title=row["title"] or "等待获取视频信息", status=status,
                progress=float(row["progress"] or 0), speed=row["speed"] or "", speed_bps=float(row["speed_bps"] or 0),
                downloaded_bytes=int(row["downloaded_bytes"] or 0), total_bytes=int(row["total_bytes"] or 0),
                eta=row["eta"] or "", size=row["size"] or "", error=row["error"] or "",
                media_path=row["media_path"] or "", thumbnail_path=row["thumbnail_path"] or "",
                created_at=row["created_at"] or datetime.now().isoformat(timespec="seconds"),
            )
            changed = False
            if task.status in {"completed", "deleted"} and task.media_path:
                file_exists = Path(task.media_path).exists()
                next_status = "completed" if file_exists else "deleted"
                if task.status != next_status:
                    task.status = next_status
                    changed = True
            if not task.media_path and task.status in {"completed", "deleted"}:
                media = self.db.get_latest_media_for_url(task.url)
                if media:
                    task.media_path = media.video_path
                    task.thumbnail_path = media.thumbnail_path
                    if not Path(task.media_path).exists():
                        task.status = "deleted"
                    changed = True
            if changed:
                self._persist(task)
            self.tasks[task.id] = task
            restored.append(task)
        return restored

    def _persist(self, task: DownloadTask) -> None:
        self.db.upsert_download_task(task)

    def enqueue(self, url: str, output_dir: str, proxy: str = "", cookie_file: str = "",
                quality: str = "best", filename_template: str = "%(title)s [%(id)s].%(ext)s",
                ffmpeg_path: str = "", format_selector: str = "", download_album: bool = False,
                playlist_mode: str = "auto") -> str:
        task = DownloadTask(
            id=uuid4().hex[:10], url=url.strip(), output_dir=output_dir, proxy=proxy,
            quality=quality, download_album=download_album, playlist_mode=playlist_mode, filename_template=filename_template,
            ffmpeg_path=ffmpeg_path, format_selector=format_selector,
        )
        self.tasks[task.id] = task
        self._persist(task)
        self.queue.append(task.id)
        self.task_added.emit(task)
        self._start_next()
        return task.id

    # Backward-compatible name used by older callers.
    start = enqueue

    def cancel(self, task_id: str | None = None) -> None:
        task_id = task_id or self.active_task_id
        if not task_id or task_id not in self.tasks:
            return
        task = self.tasks[task_id]
        if task.status == "waiting_selection":
            self.discard_task(task_id)
            return
        if task.status == "queued":
            task.status = "paused"
            self.queue = deque(queued_id for queued_id in self.queue if queued_id != task_id)
            self._persist(task)
            self.task_updated.emit(task)
            return
        worker = self.workers.get(task_id)
        if worker:
            task.pause_requested = False
            task.cancel_requested = True
            task.status = "canceling"
            self._persist(task)
            self.task_updated.emit(task)
            worker.cancel()

    def pause(self, task_id: str | None = None) -> None:
        task_id = task_id or self.active_task_id
        if not task_id or task_id not in self.tasks:
            return
        task = self.tasks[task_id]
        worker = self.workers.get(task_id)
        if worker and task.status in {"downloading", "waiting_selection"}:
            task.pause_requested = True
            task.status = "暂停中"
            self._persist(task)
            self.task_updated.emit(task)
            worker.cancel()
        elif task.status == "queued":
            # A queued task has no worker yet; pausing it means removing it
            # from the queue and retaining the task record for later resume.
            task.status = "paused"
            self.queue = deque(queued_id for queued_id in self.queue if queued_id != task_id)
            self._persist(task)
            self.task_updated.emit(task)

    def resume(self, task_id: str) -> None:
        task = self.tasks.get(task_id)
        if not task or task.status != "paused":
            return
        task.status = "queued"
        task.pause_requested = False
        task.cancel_requested = False
        task.speed_samples.clear()
        task.speed_bps = 0.0
        task.speed = ""
        self._persist(task)
        self.queue.appendleft(task.id)
        self.task_updated.emit(task)
        self._start_next()

    def retry(self, task_id: str) -> None:
        task = self.tasks.get(task_id)
        if task and task.status == "deleted":
            self.redownload(task_id)
            return
        if not task or task.status not in {"failed", "canceled"}:
            return
        task.status, task.error, task.progress, task.cancel_requested, task.pause_requested = "queued", "", 0.0, False, False
        task.speed_samples.clear()
        task.speed_bps = 0.0
        task.speed = ""
        self._persist(task)
        self.queue.append(task.id)
        self.task_updated.emit(task)
        self._start_next()

    def start_task(self, task_id: str) -> None:
        task = self.tasks.get(task_id)
        if not task:
            return
        if task.status == "queued":
            if task_id not in self.queue:
                self.queue.append(task_id)
            self._start_next()
        elif task.status == "paused":
            self.resume(task_id)
        elif task.status in {"failed", "canceled"}:
            self.retry(task_id)
        elif task.status in {"completed", "deleted"}:
            self.redownload(task_id)

    def redownload(self, task_id: str, quality_override: str | None = None) -> str | None:
        task = self.tasks.get(task_id)
        if not task:
            return None
        return self.enqueue(task.url, task.output_dir, task.proxy, quality=quality_override or task.quality,
                            filename_template=task.filename_template, ffmpeg_path=task.ffmpeg_path,
                            format_selector="" if quality_override else task.format_selector,
                            download_album=task.download_album, playlist_mode=task.playlist_mode)

    def delete_task(self, task_id: str, delete_files: bool = False) -> bool:
        task = self.tasks.get(task_id)
        if not task:
            return False
        if task_id in self.workers:
            # Deleting an active task also implies cancelling it.  Keep the
            # requested file policy and remove everything after the worker
            # unwinds, so the user does not need to pause first.
            self._pending_deletes[task_id] = bool(delete_files)
            task.pause_requested = False
            task.cancel_requested = True
            task.status = "canceling"
            self._persist(task)
            self.task_updated.emit(task)
            self.workers[task_id].cancel()
            return True
        self.queue = deque(queued_id for queued_id in self.queue if queued_id != task_id)
        self._remove_task_record(task_id, delete_files)
        return True

    def _remove_task_record(self, task_id: str, delete_files: bool = False) -> None:
        task = self.tasks.pop(task_id, None)
        if not task:
            return
        self.queue = deque(queued_id for queued_id in self.queue if queued_id != task_id)
        if delete_files:
            for file_path in (task.media_path, task.thumbnail_path):
                if file_path:
                    try:
                        Path(file_path).unlink(missing_ok=True)
                    except OSError:
                        pass
            if task.media_path:
                info_path = Path(task.media_path).with_suffix(".info.json")
                try:
                    info_path.unlink(missing_ok=True)
                except OSError:
                    pass
        self.db.delete_download_task(
            task_id, source_url=task.url, media_path=task.media_path, delete_media=delete_files
        )
        self.task_deleted.emit(task_id)

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
            worker.cancel()
            return
        self.tasks.pop(task_id, None)
        self.queue = deque(item for item in self.queue if item != task_id)
        self.db.delete_download_task(task_id)
        self.task_deleted.emit(task_id)

    def set_format_selector(self, task_id: str, selector: str) -> None:
        task = self.tasks.get(task_id)
        worker = self.workers.get(task_id)
        if not task or not worker:
            return
        task.format_selector = selector
        task.status = "downloading" if selector else "canceled"
        self._persist(task)
        worker.set_format_selector(selector)

    def _start_next(self) -> None:
        if len(self.workers) >= self.max_concurrent or not self.queue:
            return
        while self.queue and len(self.workers) < self.max_concurrent:
            task_id = self.queue.popleft()
            task = self.tasks[task_id]
            if task.status != "queued":
                continue
            task.status = "downloading"
            self._persist(task)
            self.task_updated.emit(task)
            thread = QThread()
            worker = DownloadWorker(task.url, task.output_dir, self.db, task.proxy, "", task.quality,
                                    task.filename_template, task.ffmpeg_path, task.format_selector,
                                    task.download_album, task.playlist_mode)
            self.threads[task_id] = thread
            self.workers[task_id] = worker
            if self.active_task_id is None:
                self.active_task_id = task_id
                self.thread = thread
                self.worker = worker
            worker.moveToThread(thread)
            thread.started.connect(worker.run)
            worker.progress.connect(lambda data, tid=task_id: self._on_progress(tid, data))
            worker.formats_ready.connect(lambda payload, tid=task_id: self._on_formats_ready(tid, payload))
            worker.playlist_info.connect(lambda payload, tid=task_id: self._on_playlist_info(tid, payload))
            worker.completed.connect(lambda item, tid=task_id: self._on_media_completed(tid, item))
            worker.failed.connect(lambda error, tid=task_id: self._on_failed(tid, error))
            worker.finished.connect(thread.quit)
            worker.finished.connect(worker.deleteLater)
            thread.finished.connect(lambda tid=task_id: self._thread_finished(tid))
            thread.finished.connect(thread.deleteLater)
            thread.start()

    def _on_formats_ready(self, task_id: str, payload: dict) -> None:
        task = self.tasks.get(task_id)
        if task:
            if payload.get("title"):
                task.title = payload["title"]
            if payload.get("thumbnail_path"):
                task.thumbnail_path = payload["thumbnail_path"]
            task.status = "waiting_selection"
            self._persist(task)
            self.task_updated.emit(task)
            self.formats_ready.emit(task_id, payload)

    def _on_playlist_info(self, task_id: str, payload: dict) -> None:
        task = self.tasks.get(task_id)
        if not task:
            return
        if payload.get("is_playlist"):
            count = int(payload.get("count") or 0)
            base = payload.get("title") or task.title or "播放列表"
            task.title = f"{base}（共 {count} 个视频）" if count else f"{base}（播放列表）"
            self._persist(task)
            self.task_updated.emit(task)
        self.playlist_info.emit(task_id, payload)

    def _on_media_completed(self, task_id: str, media: MediaItem) -> None:
        task = self.tasks.get(task_id)
        if task:
            task.title = media.title or task.title
            task.media_path = media.video_path
            task.thumbnail_path = media.thumbnail_path
            self._persist(task)
        self.task_media_completed.emit(task_id, media)

    def _on_progress(self, task_id: str, data: dict) -> None:
        task = self.tasks.get(task_id)
        if not task:
            return
        total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
        done = data.get("downloaded_bytes") or 0
        task.downloaded_bytes = int(done or 0)
        task.total_bytes = int(total or 0)
        if total:
            task.progress = min(100.0, done * 100.0 / total)
        info = data.get("info_dict") or {}
        if info.get("title"):
            task.title = info["title"]
        raw_speed = data.get("speed")
        if raw_speed:
            task.speed_samples.append(float(raw_speed))
            task.speed_bps = sum(task.speed_samples) / len(task.speed_samples)
        task.speed = format_speed(task.speed_bps) or data.get("_speed_str") or ""
        remaining = task.total_bytes - task.downloaded_bytes
        task.eta = format_eta(remaining / task.speed_bps) if task.speed_bps > 0 and remaining > 0 else data.get("_eta_str") or ""
        task.size = data.get("_total_bytes_str") or data.get("_total_bytes_estimate_str") or ""
        if data.get("thumbnail_path"):
            task.thumbnail_path = data["thumbnail_path"]
        self._persist(task)
        self.task_progress.emit(task_id, data)

    def _on_failed(self, task_id: str, error: str) -> None:
        task = self.tasks[task_id]
        task.error = error
        self._persist(task)
        self.task_updated.emit(task)
        self._persist(task)

    def _thread_finished(self, task_id: str) -> None:
        task = self.tasks.get(task_id)
        if not task:
            self.threads.pop(task_id, None)
            self.workers.pop(task_id, None)
            self._start_next()
            return
        if task_id in self._pending_deletes:
            delete_files = self._pending_deletes.pop(task_id)
            self._remove_task_record(task_id, delete_files)
            self.threads.pop(task_id, None)
            self.workers.pop(task_id, None)
            if self.active_task_id == task_id:
                self.active_task_id = next(iter(self.workers), None)
                self.thread = self.threads.get(self.active_task_id) if self.active_task_id else None
                self.worker = self.workers.get(self.active_task_id) if self.active_task_id else None
            self._start_next()
            return
        if task_id in self._discard_tasks:
            self._discard_tasks.discard(task_id)
            self.db.delete_download_task(task_id)
            self.tasks.pop(task_id, None)
            self.task_deleted.emit(task_id)
            self.threads.pop(task_id, None)
            self.workers.pop(task_id, None)
            if self.active_task_id == task_id:
                self.active_task_id = next(iter(self.workers), None)
                self.thread = self.threads.get(self.active_task_id) if self.active_task_id else None
                self.worker = self.workers.get(self.active_task_id) if self.active_task_id else None
            self._start_next()
            return
        if task.pause_requested:
            task.status = "paused"
        elif task.cancel_requested:
            task.status = "canceled"
        elif task.error:
            task.status = "failed"
        else:
            task.status = "completed"
            task.progress = 100.0
        self._persist(task)
        self.task_updated.emit(task)
        self.task_finished.emit(task_id, task.status, task.error)
        self.threads.pop(task_id, None)
        self.workers.pop(task_id, None)
        if self.active_task_id == task_id:
            self.active_task_id = next(iter(self.workers), None)
            self.thread = self.threads.get(self.active_task_id) if self.active_task_id else None
            self.worker = self.workers.get(self.active_task_id) if self.active_task_id else None
        self._start_next()

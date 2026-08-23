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
    proxy: str = ""
    filename_template: str = "%(title)s [%(id)s].%(ext)s"
    ffmpeg_path: str = ""
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

    def __init__(self, url: str, output_dir: str, db: Database, proxy: str = "", cookie_file: str = "",
                 quality: str = "best", filename_template: str = "%(title)s [%(id)s].%(ext)s",
                 ffmpeg_path: str = ""):
        super().__init__()
        self.url, self.output_dir, self.db, self.proxy, self.cookie_file = url, output_dir, db, proxy, cookie_file
        self.quality, self.filename_template, self.ffmpeg_path = quality, filename_template, ffmpeg_path
        self._cancel = threading.Event()
        self._thumbnail_saved = False

    def cancel(self) -> None:
        self._cancel.set()

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
            "noplaylist": False,
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
                    thumb = next((p for p in base.parent.glob(base.stem + ".*") if p.suffix.lower() not in {".mp4", ".webm", ".mkv"}), None)
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

    def _save_thumbnail(self, url: str, video_id: str) -> str:
        """Fetch the cover as soon as yt-dlp exposes it, so the task card can show it."""
        safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", str(video_id))[:80] or "video"
        path = Path(self.output_dir) / f"{safe_id}.thumb.jpg"
        try:
            response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            response.raise_for_status()
            path.write_bytes(response.content)
            self._thumbnail_saved = True
            return str(path)
        except Exception:
            return ""


class DownloadService(QObject):
    task_added = Signal(object)
    task_updated = Signal(object)
    task_progress = Signal(str, dict)
    task_media_completed = Signal(str, object)
    task_finished = Signal(str, str, str)
    failed = Signal(str)

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self.tasks: dict[str, DownloadTask] = {}
        self.queue: deque[str] = deque()
        self.active_task_id: str | None = None
        self.thread: QThread | None = None
        self.worker: DownloadWorker | None = None

    def enqueue(self, url: str, output_dir: str, proxy: str = "", cookie_file: str = "",
                quality: str = "best", filename_template: str = "%(title)s [%(id)s].%(ext)s",
                ffmpeg_path: str = "") -> str:
        task = DownloadTask(
            id=uuid4().hex[:10], url=url.strip(), output_dir=output_dir, proxy=proxy,
            quality=quality, filename_template=filename_template, ffmpeg_path=ffmpeg_path,
        )
        self.tasks[task.id] = task
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
        if task.status == "queued":
            task.status = "canceled"
            self.task_updated.emit(task)
            self.task_finished.emit(task.id, task.status, "")
            return
        if task_id == self.active_task_id and self.worker:
            task.pause_requested = False
            task.cancel_requested = True
            task.status = "canceling"
            self.task_updated.emit(task)
            self.worker.cancel()

    def pause(self, task_id: str | None = None) -> None:
        task_id = task_id or self.active_task_id
        if not task_id or task_id not in self.tasks:
            return
        task = self.tasks[task_id]
        if task_id == self.active_task_id and task.status == "downloading" and self.worker:
            task.pause_requested = True
            task.status = "暂停中"
            self.task_updated.emit(task)
            self.worker.cancel()

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
        self.queue.appendleft(task.id)
        self.task_updated.emit(task)
        self._start_next()

    def retry(self, task_id: str) -> None:
        task = self.tasks.get(task_id)
        if not task or task.status not in {"failed", "canceled"}:
            return
        task.status, task.error, task.progress, task.cancel_requested, task.pause_requested = "queued", "", 0.0, False, False
        task.speed_samples.clear()
        task.speed_bps = 0.0
        task.speed = ""
        self.queue.append(task.id)
        self.task_updated.emit(task)
        self._start_next()

    def start_task(self, task_id: str) -> None:
        task = self.tasks.get(task_id)
        if not task:
            return
        if task.status == "queued":
            self._start_next()
        elif task.status == "paused":
            self.resume(task_id)
        elif task.status in {"failed", "canceled"}:
            self.retry(task_id)
        elif task.status == "completed":
            self.redownload(task_id)

    def redownload(self, task_id: str) -> str | None:
        task = self.tasks.get(task_id)
        if not task:
            return None
        return self.enqueue(task.url, task.output_dir, task.proxy, quality=task.quality,
                            filename_template=task.filename_template, ffmpeg_path=task.ffmpeg_path)

    def _start_next(self) -> None:
        if self.active_task_id or not self.queue:
            return
        while self.queue:
            task_id = self.queue.popleft()
            task = self.tasks[task_id]
            if task.status != "queued":
                continue
            self.active_task_id = task_id
            task.status = "downloading"
            self.task_updated.emit(task)
            self.thread = QThread()
            self.worker = DownloadWorker(task.url, task.output_dir, self.db, task.proxy, "", task.quality, task.filename_template, task.ffmpeg_path)
            self.worker.moveToThread(self.thread)
            self.thread.started.connect(self.worker.run)
            self.worker.progress.connect(lambda data, tid=task_id: self._on_progress(tid, data))
            self.worker.completed.connect(lambda item, tid=task_id: self.task_media_completed.emit(tid, item))
            self.worker.failed.connect(lambda error, tid=task_id: self._on_failed(tid, error))
            self.worker.finished.connect(self.thread.quit)
            self.worker.finished.connect(self.worker.deleteLater)
            self.thread.finished.connect(lambda tid=task_id: self._thread_finished(tid))
            self.thread.finished.connect(self.thread.deleteLater)
            self.thread.start()
            return

    def _on_progress(self, task_id: str, data: dict) -> None:
        task = self.tasks[task_id]
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
        self.task_progress.emit(task_id, data)

    def _on_failed(self, task_id: str, error: str) -> None:
        task = self.tasks[task_id]
        task.error = error
        self.task_updated.emit(task)

    def _thread_finished(self, task_id: str) -> None:
        task = self.tasks[task_id]
        if task.pause_requested:
            task.status = "paused"
        elif task.cancel_requested:
            task.status = "canceled"
        elif task.error:
            task.status = "failed"
        else:
            task.status = "completed"
            task.progress = 100.0
        self.task_updated.emit(task)
        self.task_finished.emit(task_id, task.status, task.error)
        self.active_task_id = None
        self.thread = None
        self.worker = None
        self._start_next()

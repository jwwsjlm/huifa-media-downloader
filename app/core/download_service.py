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

    def __init__(self, url: str, output_dir: str, db: Database, proxy: str = "", cookie_file: str = "",
                 quality: str = "best", filename_template: str = "%(title)s [%(id)s].%(ext)s",
                 ffmpeg_path: str = "", format_selector: str = ""):
        super().__init__()
        self.url, self.output_dir, self.db, self.proxy, self.cookie_file = url, output_dir, db, proxy, cookie_file
        self.quality, self.filename_template, self.ffmpeg_path = quality, filename_template, ffmpeg_path
        self.format_selector = format_selector
        self._cancel = threading.Event()
        self._thumbnail_saved = False
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
            if self.quality == "custom" and not self.format_selector:
                probe_opts = {k: v for k, v in ydl_opts.items() if k not in {"format", "progress_hooks", "writethumbnail", "writeinfojson"}}
                with yt_dlp.YoutubeDL(probe_opts) as probe:
                    preview = probe.extract_info(self.url, download=False)
                choices = self._build_format_choices(preview)
                self.formats_ready.emit({"title": preview.get("title", ""), "choices": choices})
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
            choices.append({"label": label, "selector": selector})
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
    failed = Signal(str)

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self.tasks: dict[str, DownloadTask] = {}
        self.queue: deque[str] = deque()
        self.active_task_id: str | None = None
        self.thread: QThread | None = None
        self.worker: DownloadWorker | None = None

    def restore_tasks(self) -> list[DownloadTask]:
        restored: list[DownloadTask] = []
        for row in self.db.list_download_tasks():
            status = row["status"]
            if status in {"downloading", "canceling", "暂停中"}:
                status = "paused"
            task = DownloadTask(
                id=row["id"], url=row["url"], output_dir=row["output_dir"], quality=row["quality"] or "best",
                proxy=row["proxy"] or "", filename_template=row["filename_template"] or "%(title)s [%(id)s].%(ext)s",
                ffmpeg_path=row["ffmpeg_path"] or "", format_selector=row["format_selector"] or "",
                title=row["title"] or "等待获取视频信息", status=status,
                progress=float(row["progress"] or 0), speed=row["speed"] or "", speed_bps=float(row["speed_bps"] or 0),
                downloaded_bytes=int(row["downloaded_bytes"] or 0), total_bytes=int(row["total_bytes"] or 0),
                eta=row["eta"] or "", size=row["size"] or "", error=row["error"] or "",
                media_path=row["media_path"] or "", thumbnail_path=row["thumbnail_path"] or "",
                created_at=row["created_at"] or datetime.now().isoformat(timespec="seconds"),
            )
            self.tasks[task.id] = task
            restored.append(task)
        return restored

    def _persist(self, task: DownloadTask) -> None:
        self.db.upsert_download_task(task)

    def enqueue(self, url: str, output_dir: str, proxy: str = "", cookie_file: str = "",
                quality: str = "best", filename_template: str = "%(title)s [%(id)s].%(ext)s",
                ffmpeg_path: str = "", format_selector: str = "") -> str:
        task = DownloadTask(
            id=uuid4().hex[:10], url=url.strip(), output_dir=output_dir, proxy=proxy,
            quality=quality, filename_template=filename_template, ffmpeg_path=ffmpeg_path, format_selector=format_selector,
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
        if task.status == "queued":
            task.status = "canceled"
            self._persist(task)
            self.task_updated.emit(task)
            self.task_finished.emit(task.id, task.status, "")
            return
        if task_id == self.active_task_id and self.worker:
            task.pause_requested = False
            task.cancel_requested = True
            task.status = "canceling"
            self._persist(task)
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
            self._persist(task)
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
        self._persist(task)
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
        self._persist(task)
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

    def redownload(self, task_id: str, quality_override: str | None = None) -> str | None:
        task = self.tasks.get(task_id)
        if not task:
            return None
        return self.enqueue(task.url, task.output_dir, task.proxy, quality=quality_override or task.quality,
                            filename_template=task.filename_template, ffmpeg_path=task.ffmpeg_path,
                            format_selector="" if quality_override else task.format_selector)

    def delete_task(self, task_id: str) -> bool:
        task = self.tasks.get(task_id)
        if not task or task_id == self.active_task_id or task.status in {"downloading", "canceling", "暂停中"}:
            return False
        self.queue = deque(queued_id for queued_id in self.queue if queued_id != task_id)
        self.tasks.pop(task_id, None)
        self.db.delete_download_task(task_id)
        self.task_deleted.emit(task_id)
        return True

    def set_format_selector(self, task_id: str, selector: str) -> None:
        task = self.tasks.get(task_id)
        if not task or not self.worker or task_id != self.active_task_id:
            return
        task.format_selector = selector
        task.status = "downloading" if selector else "canceled"
        self._persist(task)
        self.worker.set_format_selector(selector)

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
            self._persist(task)
            self.task_updated.emit(task)
            self.thread = QThread()
            self.worker = DownloadWorker(task.url, task.output_dir, self.db, task.proxy, "", task.quality, task.filename_template, task.ffmpeg_path, task.format_selector)
            self.worker.moveToThread(self.thread)
            self.thread.started.connect(self.worker.run)
            self.worker.progress.connect(lambda data, tid=task_id: self._on_progress(tid, data))
            self.worker.formats_ready.connect(lambda payload, tid=task_id: self._on_formats_ready(tid, payload))
            self.worker.completed.connect(lambda item, tid=task_id: self.task_media_completed.emit(tid, item))
            self.worker.failed.connect(lambda error, tid=task_id: self._on_failed(tid, error))
            self.worker.finished.connect(self.thread.quit)
            self.worker.finished.connect(self.worker.deleteLater)
            self.thread.finished.connect(lambda tid=task_id: self._thread_finished(tid))
            self.thread.finished.connect(self.thread.deleteLater)
            self.thread.start()
            return

    def _on_formats_ready(self, task_id: str, payload: dict) -> None:
        task = self.tasks.get(task_id)
        if task:
            if payload.get("title"):
                task.title = payload["title"]
            task.status = "waiting_selection"
            self._persist(task)
            self.task_updated.emit(task)
            self.formats_ready.emit(task_id, payload)

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
        self._persist(task)
        self.task_progress.emit(task_id, data)

    def _on_failed(self, task_id: str, error: str) -> None:
        task = self.tasks[task_id]
        task.error = error
        self._persist(task)
        self.task_updated.emit(task)
        self._persist(task)

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

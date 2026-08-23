from __future__ import annotations

import hashlib
import json
import struct
import threading
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal, Slot

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
        # Prefer the bundled FFmpeg shipped with the application. yt-dlp accepts
        # either the executable path or its containing directory.
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
                    thumb = next(iter(base.parent.glob(base.stem + ".*")), None)
                    info_path = str(base.with_suffix(".info.json"))
                    if not Path(info_path).exists():
                        info_path = str(base.with_suffix(".json"))
                    digest = hashlib.sha256(Path(video_path).read_bytes()).hexdigest() if Path(video_path).exists() else ""
                    item = MediaItem(source_url=entry.get("webpage_url") or self.url, title=entry.get("title") or "",
                                     description=entry.get("description") or "", tags=entry.get("tags") or [],
                                     uploader=entry.get("uploader") or "", thumbnail_path=str(thumb or ""),
                                     video_path=video_path, metadata_json_path=info_path,
                                     source_ip=detect_public_ip(self.proxy), proxy_profile=self.proxy, sha256=digest)
                    item.id = self.db.add_media(item)
                    self.completed.emit(item)
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()


class DownloadService(QObject):
    progress = Signal(dict)
    completed = Signal(object)
    failed = Signal(str)
    started = Signal()

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self.thread = None
        self.worker = None

    def start(self, url: str, output_dir: str, proxy: str = "", cookie_file: str = "",
              quality: str = "best", filename_template: str = "%(title)s [%(id)s].%(ext)s",
              ffmpeg_path: str = "") -> None:
        from PySide6.QtCore import QThread
        if self.thread and self.thread.isRunning():
            self.failed.emit("当前已有下载任务，请等待完成")
            return
        self.thread = QThread()
        self.worker = DownloadWorker(url, output_dir, self.db, proxy, cookie_file, quality, filename_template, ffmpeg_path)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.progress)
        self.worker.completed.connect(self.completed)
        self.worker.failed.connect(self.failed)
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.start()
        self.started.emit()

    def cancel(self) -> None:
        if self.worker:
            self.worker.cancel()

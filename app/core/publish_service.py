from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal, Slot

from app.adapters.sau_adapter import SauAdapter
from app.adapters.toutiao_adapter import ToutiaoAdapter
from app.storage.database import Database
from app.storage.models import MediaItem, PublishTask


class PublishWorker(QObject):
    result = Signal(int, bool, str)
    finished = Signal()

    def __init__(self, task_id: int, row, media: MediaItem):
        super().__init__()
        self.task_id = task_id
        self.row = row
        self.media = media

    @Slot()
    def run(self) -> None:
        try:
            metadata = {
                "title": self.row["title"],
                "description": self.row["description"],
                "tags": json.loads(self.row["topics"] or "[]"),
            }
            settings = json.loads(self.row["settings"] or "{}")
            adapter = ToutiaoAdapter() if self.row["platform"] == "toutiao" else SauAdapter(self.row["platform"])
            payload = adapter.build_payload(self.media.__dict__, metadata, settings)
            ok, result = adapter.publish(payload)
            self.result.emit(self.task_id, ok, result)
        except Exception as exc:
            self.result.emit(self.task_id, False, str(exc))
        finally:
            self.finished.emit()


class PublishService(QObject):
    status = Signal(int, str, str)

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self.last_created_count = 0
        self.last_existing_count = 0
        self.threads: dict[int, QThread] = {}
        self.workers: dict[int, PublishWorker] = {}

    def create_tasks(self, media: MediaItem, platforms: list[str], metadata: dict, settings: dict) -> list[int]:
        ids = []
        self.last_created_count = 0
        self.last_existing_count = 0
        for platform in platforms:
            key = hashlib.sha256(f"{media.sha256}:{platform}:{metadata.get('title','')}".encode()).hexdigest()
            existing = self.db.get_publish_task_by_key(key)
            if existing:
                ids.append(int(existing["id"]))
                self.last_existing_count += 1
                continue
            ids.append(self.db.add_publish_task(PublishTask(media_id=media.id or 0, platform=platform,
                          title=metadata.get("title", media.title), description=metadata.get("description", media.description),
                          topics=metadata.get("tags", media.tags), settings=settings.get(platform, {}), idempotency_key=key)))
            self.last_created_count += 1
        return ids

    def run_task(self, task_id: int) -> None:
        if task_id in self.workers:
            return
        row = next((r for r in self.db.list_publish_tasks() if r["id"] == task_id), None)
        if not row:
            return
        media = self.db.get_media(row["media_id"])
        if not media:
            self.status.emit(task_id, "failed", "媒体记录不存在")
            return
        self.db.update_publish_status(task_id, "uploading")
        self.status.emit(task_id, "uploading", "")
        thread = QThread()
        worker = PublishWorker(task_id, row, media)
        self.threads[task_id] = thread
        self.workers[task_id] = worker
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.result.connect(self._on_result)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(lambda tid=task_id: self._thread_finished(tid))
        thread.finished.connect(thread.deleteLater)
        thread.start()

    def _on_result(self, task_id: int, ok: bool, result: str) -> None:
        state = "success" if ok else "failed"
        self.db.update_publish_status(task_id, state, result)
        self.status.emit(task_id, state, result)

    def _thread_finished(self, task_id: int) -> None:
        self.threads.pop(task_id, None)
        self.workers.pop(task_id, None)

    def retry_task(self, task_id: int) -> None:
        row = next((r for r in self.db.list_publish_tasks() if r["id"] == task_id), None)
        if row and row["status"] == "failed":
            self.run_task(task_id)

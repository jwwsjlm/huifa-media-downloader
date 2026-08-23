from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot

from app.adapters.sau_adapter import SauAdapter
from app.adapters.toutiao_adapter import ToutiaoAdapter
from app.storage.database import Database
from app.storage.models import MediaItem, PublishTask


class PublishService(QObject):
    status = Signal(int, str, str)

    def __init__(self, db: Database):
        super().__init__()
        self.db = db

    def create_tasks(self, media: MediaItem, platforms: list[str], metadata: dict, settings: dict) -> list[int]:
        ids = []
        for platform in platforms:
            key = hashlib.sha256(f"{media.sha256}:{platform}:{metadata.get('title','')}".encode()).hexdigest()
            ids.append(self.db.add_publish_task(PublishTask(media_id=media.id or 0, platform=platform,
                          title=metadata.get("title", media.title), description=metadata.get("description", media.description),
                          topics=metadata.get("tags", media.tags), settings=settings.get(platform, {}), idempotency_key=key)))
        return ids

    def run_task(self, task_id: int) -> None:
        row = next((r for r in self.db.list_publish_tasks() if r["id"] == task_id), None)
        if not row:
            return
        media = self.db.get_media(row["media_id"])
        if not media:
            self.status.emit(task_id, "failed", "媒体记录不存在")
            return
        metadata = {"title": row["title"], "description": row["description"], "tags": json.loads(row["topics"] or "[]")}
        settings = json.loads(row["settings"] or "{}")
        adapter = ToutiaoAdapter() if row["platform"] == "toutiao" else SauAdapter(row["platform"])
        payload = adapter.build_payload(media.__dict__, metadata, settings)
        self.db.update_publish_status(task_id, "uploading")
        self.status.emit(task_id, "uploading", "")
        ok, result = adapter.publish(payload)
        state = "success" if ok else "failed"
        self.db.update_publish_status(task_id, state, result)
        self.status.emit(task_id, state, result)


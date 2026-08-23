from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable

from app.storage.models import MediaItem, PublishTask


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS media_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT, source_url TEXT NOT NULL,
                source_platform TEXT, title TEXT, description TEXT, tags TEXT,
                uploader TEXT, thumbnail_path TEXT, video_path TEXT,
                metadata_json_path TEXT, source_ip TEXT, proxy_profile TEXT,
                downloaded_at TEXT, sha256 TEXT
            );
            CREATE TABLE IF NOT EXISTS publish_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT, media_id INTEGER NOT NULL,
                platform TEXT NOT NULL, account TEXT, status TEXT, title TEXT,
                description TEXT, topics TEXT, settings TEXT, idempotency_key TEXT UNIQUE,
                result TEXT, created_at TEXT, FOREIGN KEY(media_id) REFERENCES media_items(id)
            );
            CREATE TABLE IF NOT EXISTS download_tasks (
                id TEXT PRIMARY KEY, url TEXT NOT NULL, output_dir TEXT NOT NULL,
                quality TEXT, proxy TEXT, filename_template TEXT, ffmpeg_path TEXT, format_selector TEXT,
                title TEXT, status TEXT, progress REAL DEFAULT 0,
                speed TEXT, speed_bps REAL DEFAULT 0, downloaded_bytes INTEGER DEFAULT 0,
                total_bytes INTEGER DEFAULT 0, eta TEXT, size TEXT, error TEXT,
                media_path TEXT, thumbnail_path TEXT, created_at TEXT,
                updated_at TEXT
            );
            """
        )
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(download_tasks)").fetchall()}
        if "format_selector" not in columns:
            self.conn.execute("ALTER TABLE download_tasks ADD COLUMN format_selector TEXT DEFAULT ''")
        self.conn.commit()

    def add_media(self, item: MediaItem) -> int:
        cur = self.conn.execute(
            """INSERT INTO media_items(source_url,source_platform,title,description,tags,uploader,
            thumbnail_path,video_path,metadata_json_path,source_ip,proxy_profile,downloaded_at,sha256)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (item.source_url, item.source_platform, item.title, item.description,
             json.dumps(item.tags, ensure_ascii=False), item.uploader, item.thumbnail_path,
             item.video_path, item.metadata_json_path, item.source_ip, item.proxy_profile,
             item.downloaded_at, item.sha256),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list_media(self) -> list[MediaItem]:
        rows = self.conn.execute("SELECT * FROM media_items ORDER BY id DESC").fetchall()
        return [MediaItem(id=r["id"], source_url=r["source_url"], source_platform=r["source_platform"],
                          title=r["title"] or "", description=r["description"] or "",
                          tags=json.loads(r["tags"] or "[]"), uploader=r["uploader"] or "",
                          thumbnail_path=r["thumbnail_path"] or "", video_path=r["video_path"] or "",
                          metadata_json_path=r["metadata_json_path"] or "", source_ip=r["source_ip"] or "",
                          proxy_profile=r["proxy_profile"] or "", downloaded_at=r["downloaded_at"] or "",
                          sha256=r["sha256"] or "") for r in rows]

    def get_media(self, media_id: int) -> MediaItem | None:
        return next((m for m in self.list_media() if m.id == media_id), None)

    def add_publish_task(self, task: PublishTask) -> int:
        cur = self.conn.execute(
            """INSERT OR REPLACE INTO publish_tasks(media_id,platform,account,status,title,description,
            topics,settings,idempotency_key,result,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (task.media_id, task.platform, task.account, task.status, task.title, task.description,
             json.dumps(task.topics, ensure_ascii=False), json.dumps(task.settings, ensure_ascii=False),
             task.idempotency_key, task.result, task.created_at),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list_publish_tasks(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM publish_tasks ORDER BY id DESC").fetchall()

    def update_publish_status(self, task_id: int, status: str, result: str = "") -> None:
        self.conn.execute("UPDATE publish_tasks SET status=?, result=? WHERE id=?", (status, result, task_id))
        self.conn.commit()

    def upsert_download_task(self, task) -> None:
        self.conn.execute(
            """INSERT INTO download_tasks
            (id,url,output_dir,quality,proxy,filename_template,ffmpeg_path,format_selector,title,status,progress,
             speed,speed_bps,downloaded_bytes,total_bytes,eta,size,error,media_path,thumbnail_path,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
            ON CONFLICT(id) DO UPDATE SET
             url=excluded.url, output_dir=excluded.output_dir, quality=excluded.quality,
             proxy=excluded.proxy, filename_template=excluded.filename_template,
             ffmpeg_path=excluded.ffmpeg_path, format_selector=excluded.format_selector,
             title=excluded.title, status=excluded.status,
             progress=excluded.progress, speed=excluded.speed, speed_bps=excluded.speed_bps,
             downloaded_bytes=excluded.downloaded_bytes, total_bytes=excluded.total_bytes,
             eta=excluded.eta, size=excluded.size, error=excluded.error,
             media_path=excluded.media_path, thumbnail_path=excluded.thumbnail_path,
             created_at=excluded.created_at, updated_at=datetime('now')""",
            (task.id, task.url, task.output_dir, task.quality, task.proxy, task.filename_template,
             task.ffmpeg_path, task.format_selector, task.title, task.status, task.progress, task.speed, task.speed_bps,
             task.downloaded_bytes, task.total_bytes, task.eta, task.size, task.error,
             task.media_path, task.thumbnail_path, task.created_at),
        )
        self.conn.commit()

    def list_download_tasks(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM download_tasks ORDER BY created_at ASC").fetchall()

    def delete_download_task(self, task_id: str) -> None:
        self.conn.execute("DELETE FROM download_tasks WHERE id=?", (task_id,))
        self.conn.commit()

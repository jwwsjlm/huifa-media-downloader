from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

from app.storage.database_recovery import (
    DEFAULT_BACKUP_INTERVAL_SECONDS,
    DatabaseRecoveryReport,
    create_rotating_database_backup,
    database_backup_due,
    recover_database_file,
)
from app.storage.models import MediaItem, PublishTask


DATABASE_SCHEMA_VERSION = 5
_MEDIA_INSERT_SQL = """INSERT INTO media_items(
    source_url,source_platform,title,description,tags,uploader,
    thumbnail_path,video_path,metadata_json_path,source_ip,proxy_profile,downloaded_at
) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"""


@dataclass(frozen=True, slots=True)
class _PreparedMediaInsert:
    item: MediaItem
    values: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class _TaskFileRecord:
    path: str
    kind: str
    managed: int


def _decode_media_tags(value: object) -> list[str]:
    try:
        document = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(document, list):
        return []
    tags: list[str] = []
    for item in document:
        try:
            text = str(item).strip()
        except Exception:
            continue
        if text:
            tags.append(text)
    return tags


def _media_item_from_row(row: sqlite3.Row) -> MediaItem:
    return MediaItem(
        id=row["id"],
        source_url=row["source_url"],
        source_platform=row["source_platform"],
        title=row["title"] or "",
        description=row["description"] or "",
        tags=_decode_media_tags(row["tags"]),
        uploader=row["uploader"] or "",
        thumbnail_path=row["thumbnail_path"] or "",
        video_path=row["video_path"] or "",
        metadata_json_path=row["metadata_json_path"] or "",
        source_ip=row["source_ip"] or "",
        proxy_profile=row["proxy_profile"] or "",
        downloaded_at=row["downloaded_at"] or "",
    )


_CURRENT_TABLE_COLUMNS = {
    "media_items": (
        "id", "source_url", "source_platform", "title", "description", "tags",
        "uploader", "thumbnail_path", "video_path", "metadata_json_path", "source_ip",
        "proxy_profile", "downloaded_at",
    ),
    "publish_tasks": (
        "id", "media_id", "platform", "account", "status", "title", "description",
        "topics", "settings", "idempotency_key", "result", "created_at",
    ),
    "download_tasks": (
        "id", "task_kind", "parent_task_id", "root_task_id", "source_key", "collection_index",
        "options_json", "url", "output_dir", "quality", "download_album", "playlist_mode", "proxy",
        "cookie_file", "filename_template", "ffmpeg_path", "format_selector", "cookie_source",
        "cookie_browser", "cookie_profile", "cookie_keyring", "cookie_container",
        "transcode_codec", "transcode_device", "transcode_encoder", "subtitle_language", "title",
        "status", "progress", "speed", "speed_bps", "downloaded_bytes", "total_bytes", "eta",
        "size", "error", "media_path", "thumbnail_path", "created_at", "updated_at",
    ),
    "download_task_files": (
        "task_id", "path", "kind", "managed",
    ),
    "collection_probe_entries": (
        "parent_task_id", "collection_index", "source_key", "url", "title",
        "uploader", "duration", "upload_date", "thumbnail", "live_status",
        "availability", "entry_kind", "downloadable", "disabled_reason",
        "selected", "completed", "estimated_bytes",
    ),
}

_DOWNLOAD_TASK_COLUMNS = _CURRENT_TABLE_COLUMNS["download_tasks"]
_DOWNLOAD_TASK_VALUE_COLUMNS = _DOWNLOAD_TASK_COLUMNS[:-1]
_DOWNLOAD_TASK_INSERT_SQL = (
    "INSERT INTO download_tasks(" + ",".join(_DOWNLOAD_TASK_COLUMNS) + ") VALUES("
    + ",".join("?" for _column in _DOWNLOAD_TASK_VALUE_COLUMNS)
    + ",datetime('now'))"
)
_DOWNLOAD_TASK_UPSERT_SQL = (
    _DOWNLOAD_TASK_INSERT_SQL
    + " ON CONFLICT(id) DO UPDATE SET "
    + ",".join(
        f"{column}=excluded.{column}"
        for column in _DOWNLOAD_TASK_VALUE_COLUMNS
        if column != "id"
    )
    + ",updated_at=datetime('now')"
)
_DOWNLOAD_TASK_UPDATE_COLUMNS = tuple(
    column
    for column in _DOWNLOAD_TASK_VALUE_COLUMNS
    if column != "id"
)
_DOWNLOAD_TASK_UPDATE_SQL = (
    "UPDATE download_tasks SET "
    + ",".join(f"{column}=?" for column in _DOWNLOAD_TASK_UPDATE_COLUMNS)
    + ",updated_at=datetime('now') WHERE id=?"
)
_DOWNLOAD_TASK_SUBTREE_CTE = """WITH RECURSIVE task_tree(id) AS (
    SELECT id FROM download_tasks WHERE id=?
    UNION
    SELECT child.id
    FROM download_tasks AS child
    JOIN task_tree AS parent ON child.parent_task_id=parent.id
)"""

_SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS media_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source_url TEXT NOT NULL,
        source_platform TEXT, title TEXT, description TEXT, tags TEXT,
        uploader TEXT, thumbnail_path TEXT, video_path TEXT,
        metadata_json_path TEXT, source_ip TEXT, proxy_profile TEXT,
        downloaded_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS publish_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT, media_id INTEGER NOT NULL,
        platform TEXT NOT NULL, account TEXT, status TEXT, title TEXT,
        description TEXT, topics TEXT, settings TEXT, idempotency_key TEXT UNIQUE,
        result TEXT, created_at TEXT, FOREIGN KEY(media_id) REFERENCES media_items(id)
    )""",
    """CREATE TABLE IF NOT EXISTS download_tasks (
        id TEXT PRIMARY KEY,
        task_kind TEXT NOT NULL DEFAULT 'video',
        parent_task_id TEXT DEFAULT '', root_task_id TEXT DEFAULT '',
        source_key TEXT DEFAULT '', collection_index INTEGER DEFAULT 0,
        options_json TEXT DEFAULT '{}',
        url TEXT NOT NULL, output_dir TEXT NOT NULL,
        quality TEXT, download_album INTEGER DEFAULT 0,
        playlist_mode TEXT DEFAULT 'auto', proxy TEXT,
        cookie_file TEXT DEFAULT '', filename_template TEXT, ffmpeg_path TEXT,
        format_selector TEXT, cookie_source TEXT DEFAULT 'none',
        cookie_browser TEXT DEFAULT 'chrome', cookie_profile TEXT DEFAULT '',
        cookie_keyring TEXT DEFAULT '', cookie_container TEXT DEFAULT '',
        transcode_codec TEXT DEFAULT 'original', transcode_device TEXT DEFAULT 'auto',
        transcode_encoder TEXT DEFAULT 'original', subtitle_language TEXT DEFAULT 'none',
        title TEXT, status TEXT, progress REAL DEFAULT 0,
        speed TEXT, speed_bps REAL DEFAULT 0, downloaded_bytes INTEGER DEFAULT 0,
        total_bytes INTEGER DEFAULT 0, eta TEXT, size TEXT, error TEXT,
        media_path TEXT, thumbnail_path TEXT, created_at TEXT, updated_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS download_task_files (
        task_id TEXT NOT NULL,
        path TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'sidecar',
        managed INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY(task_id, path),
        FOREIGN KEY(task_id) REFERENCES download_tasks(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE IF NOT EXISTS collection_probe_entries (
        parent_task_id TEXT NOT NULL,
        collection_index INTEGER NOT NULL,
        source_key TEXT DEFAULT '', url TEXT DEFAULT '', title TEXT DEFAULT '',
        uploader TEXT DEFAULT '', duration REAL DEFAULT 0,
        upload_date TEXT DEFAULT '', thumbnail TEXT DEFAULT '',
        live_status TEXT DEFAULT '', availability TEXT DEFAULT '',
        entry_kind TEXT NOT NULL DEFAULT 'video',
        downloadable INTEGER NOT NULL DEFAULT 1,
        disabled_reason TEXT DEFAULT '', selected INTEGER NOT NULL DEFAULT 1,
        completed INTEGER NOT NULL DEFAULT 0, estimated_bytes INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(parent_task_id, collection_index),
        FOREIGN KEY(parent_task_id) REFERENCES download_tasks(id) ON DELETE CASCADE
    )""",
    "CREATE INDEX IF NOT EXISTS idx_download_tasks_created_at ON download_tasks(created_at)",
    """CREATE INDEX IF NOT EXISTS idx_download_tasks_parent
        ON download_tasks(parent_task_id, collection_index)""",
    """CREATE INDEX IF NOT EXISTS idx_download_tasks_root
        ON download_tasks(root_task_id, collection_index)""",
    """CREATE INDEX IF NOT EXISTS idx_download_tasks_source_key
        ON download_tasks(source_key, status)""",
    """CREATE INDEX IF NOT EXISTS idx_download_task_files_task
        ON download_task_files(task_id, managed)""",
    """CREATE INDEX IF NOT EXISTS idx_collection_probe_parent_selected
        ON collection_probe_entries(parent_task_id, selected, collection_index)""",
    """CREATE INDEX IF NOT EXISTS idx_collection_probe_downloadable_completed
        ON collection_probe_entries(parent_task_id, downloadable, completed, collection_index)""",
    """CREATE INDEX IF NOT EXISTS idx_collection_probe_upload_date
        ON collection_probe_entries(parent_task_id, upload_date, collection_index)""",
    """CREATE INDEX IF NOT EXISTS idx_collection_probe_duration
        ON collection_probe_entries(parent_task_id, duration, collection_index)""",
    """CREATE INDEX IF NOT EXISTS idx_media_items_source_url_id
        ON media_items(source_url, id DESC)""",
    """CREATE INDEX IF NOT EXISTS idx_publish_tasks_media_id_id
        ON publish_tasks(media_id, id)""",
    """CREATE INDEX IF NOT EXISTS idx_publish_tasks_media_platform_id
        ON publish_tasks(media_id, platform, id DESC)""",
    "CREATE INDEX IF NOT EXISTS idx_publish_tasks_status ON publish_tasks(status)",
)


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.recovery_report: DatabaseRecoveryReport = recover_database_file(self.path)
        self.last_backup_path = ""
        self.last_backup_error = ""
        self._lock = threading.RLock()
        self.conn: sqlite3.Connection | None = None
        try:
            self.conn = self._open_connection()
            if self._has_user_schema() and not self._schema_matches_current():
                self.conn.close()
                self.conn = None
                self._remove_database_files()
                self.recovery_report = DatabaseRecoveryReport(
                    status="schema_reset",
                    detail="数据库结构不是当前开发版本，已按最新结构重新创建",
                )
                self.conn = self._open_connection()

            # Publish rows intentionally survive a missing/deleted media record so
            # the queue can display a retryable diagnostic instead of losing the
            # failed publication record during cleanup.
            self._initialize_schema()
            if not self._schema_matches_current():
                raise sqlite3.DatabaseError("数据库结构初始化后仍与当前版本不一致")

            # A new/reset/restored database must supersede stale snapshots
            # immediately. Healthy databases are backed up at most once per day at
            # startup, then once more on a clean close if their WAL changed.
            self.create_backup_if_due(
                force=self.recovery_report.status in {
                    "new", "restored", "reset", "schema_reset"
                }
            )
        except BaseException:
            if self.conn is not None:
                self.conn.close()
                self.conn = None
            raise

    def _open_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        # WAL lets readers continue while progress is persisted. NORMAL keeps
        # desktop writes responsive, and busy_timeout absorbs short lock races.
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.execute("PRAGMA cache_size=-32768")
        return connection

    def _has_user_schema(self) -> bool:
        assert self.conn is not None
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
        ).fetchone()
        return row is not None

    def _schema_matches_current(self) -> bool:
        assert self.conn is not None
        version = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
        if version != DATABASE_SCHEMA_VERSION:
            return False
        for table, expected_columns in _CURRENT_TABLE_COLUMNS.items():
            rows = self.conn.execute(f'PRAGMA table_info("{table}")').fetchall()
            if tuple(str(row[1]) for row in rows) != expected_columns:
                return False
        return True

    def _remove_database_files(self) -> None:
        for suffix in ("", "-wal", "-shm", "-journal"):
            self.path.with_name(self.path.name + suffix).unlink(missing_ok=True)

    def _initialize_schema(self) -> None:
        assert self.conn is not None
        with self._immediate_transaction() as connection:
            for statement in _SCHEMA_STATEMENTS:
                connection.execute(statement)
            connection.execute(f"PRAGMA user_version={DATABASE_SCHEMA_VERSION}")

    def close(self) -> None:
        with self._lock:
            if self.conn is None:
                return
            self.create_backup_if_due(interval_seconds=0)
            self.conn.close()
            self.conn = None

    def create_backup_if_due(
        self,
        *,
        force: bool = False,
        interval_seconds: int = DEFAULT_BACKUP_INTERVAL_SECONDS,
    ) -> Path | None:
        """Create a consistent rotating backup without making startup fatal."""

        with self._lock:
            if not force and not database_backup_due(
                self.path,
                interval_seconds=interval_seconds,
            ):
                return None
            try:
                backup = create_rotating_database_backup(self.conn, self.path)
            except (OSError, sqlite3.DatabaseError) as exc:
                self.last_backup_error = str(exc)[:1000]
                return None
            self.last_backup_path = str(backup)
            self.last_backup_error = ""
            return backup

    @staticmethod
    def _prepare_media_insert(item: MediaItem) -> _PreparedMediaInsert:
        return _PreparedMediaInsert(
            item=item,
            values=(
                item.source_url,
                item.source_platform,
                item.title,
                item.description,
                json.dumps(item.tags, ensure_ascii=False),
                item.uploader,
                item.thumbnail_path,
                item.video_path,
                item.metadata_json_path,
                item.source_ip,
                item.proxy_profile,
                item.downloaded_at,
            ),
        )

    @staticmethod
    def _normalize_task_files(
        task_files: Iterable[tuple[str, str, bool]],
    ) -> list[_TaskFileRecord]:
        priorities = {
            "media": 100,
            "thumbnail": 90,
            "subtitle": 80,
            "info_json": 70,
            "metadata": 60,
            "description": 50,
            "sidecar": 0,
        }
        records: dict[str, _TaskFileRecord] = {}
        for path, kind, managed in task_files:
            path_text = str(path or "").strip()
            if not path_text:
                continue
            kind_text = str(kind or "sidecar").strip() or "sidecar"
            key = os.path.normcase(os.path.normpath(path_text))
            incoming = _TaskFileRecord(path_text, kind_text, int(bool(managed)))
            existing = records.get(key)
            if existing is None:
                records[key] = incoming
                continue
            selected_kind = (
                incoming.kind
                if priorities.get(incoming.kind, 10) > priorities.get(existing.kind, 10)
                else existing.kind
            )
            # If any source says a path is user-owned, never make it deletable
            # merely because another discovery path classified it as managed.
            records[key] = _TaskFileRecord(
                existing.path,
                selected_kind,
                min(existing.managed, incoming.managed),
            )
        return list(records.values())

    @contextmanager
    def _immediate_transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            assert self.conn is not None
            if self.conn.in_transaction:
                raise sqlite3.OperationalError("数据库连接已有未完成事务")
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                yield self.conn
                self.conn.commit()
            except BaseException:
                if self.conn.in_transaction:
                    self.conn.rollback()
                raise

    def add_media(self, item: MediaItem) -> int:
        prepared = self._prepare_media_insert(item)
        with self._immediate_transaction() as connection:
            cur = connection.execute(_MEDIA_INSERT_SQL, prepared.values)
            return int(cur.lastrowid)

    def complete_download_task(self, task, item: MediaItem) -> int:
        """Atomically persist one downloaded media item and finish its task."""
        return self.complete_download_task_batch(task, (item,))[0]

    def complete_download_task_batch(
        self,
        task,
        items: Iterable[MediaItem],
        task_files: Iterable[tuple[str, str, bool]] = (),
    ) -> list[int]:
        """Atomically persist every media item and mark an existing task complete.

        A playlist is one download task but can produce several media rows.  The
        rows and the final task state must therefore share one transaction: a
        partial playlist must never appear in the completed catalog, and a
        media row must never survive when its download task cannot be updated.
        """
        media_items = list(items)
        if not media_items:
            raise ValueError("完成下载任务时至少需要一个媒体条目")
        prepared_media = [self._prepare_media_insert(item) for item in media_items]
        managed_files = self._normalize_task_files(task_files)

        task_id = str(getattr(task, "id", "") or "").strip()
        if not task_id:
            raise ValueError("完成下载任务时缺少任务 ID")

        # For a playlist the service normally leaves these fields pointing at
        # the last completed entry.  Falling back to the last supplied item
        # also keeps this database API safe for callers that have not mutated
        # the in-memory task yet.
        last_item = media_items[-1]
        media_path = str(getattr(task, "media_path", "") or last_item.video_path or "")
        thumbnail_path = str(getattr(task, "thumbnail_path", "") or last_item.thumbnail_path or "")

        media_ids: list[int] = []
        with self._immediate_transaction() as connection:
            exists = connection.execute(
                "SELECT 1 FROM download_tasks WHERE id=?",
                (task_id,),
            ).fetchone()
            if exists is None:
                raise LookupError(f"下载任务不存在：{task_id}")

            for prepared in prepared_media:
                cursor = connection.execute(_MEDIA_INSERT_SQL, prepared.values)
                media_ids.append(int(cursor.lastrowid))

            cursor = connection.execute(
                """UPDATE download_tasks
                SET status='completed', progress=100, error='', media_path=?, thumbnail_path=?,
                    updated_at=datetime('now')
                WHERE id=?""",
                (media_path, thumbnail_path, task_id),
            )
            if cursor.rowcount != 1:
                raise LookupError(f"下载任务不存在：{task_id}")
            connection.execute("DELETE FROM download_task_files WHERE task_id=?", (task_id,))
            connection.executemany(
                """INSERT INTO download_task_files(task_id,path,kind,managed)
                VALUES(?,?,?,?)""",
                (
                    (task_id, record.path, record.kind, record.managed)
                    for record in managed_files
                ),
            )

        # Expose generated IDs only after the transaction is durable.  A
        # failed transaction leaves both the database and caller-owned models
        # untouched.
        for item, media_id in zip(media_items, media_ids):
            item.id = media_id
        return media_ids

    def replace_completed_media_path(
        self,
        task_id: str,
        old_path: str,
        new_path: str,
        *,
        transcode_codec: str,
        transcode_device: str,
        transcode_encoder: str,
    ) -> None:
        """Atomically point a completed task and catalog row at a converted file."""
        normalized_task_id = str(task_id)
        normalized_old_path = str(old_path)
        normalized_new_path = str(new_path)
        with self._immediate_transaction() as connection:
            task_row = connection.execute(
                "SELECT url,status,media_path FROM download_tasks WHERE id=?",
                (normalized_task_id,),
            ).fetchone()
            if task_row is None:
                raise LookupError(f"下载任务不存在：{normalized_task_id}")
            if (
                str(task_row["status"] or "") not in {"completed", "processing"}
                or str(task_row["media_path"] or "") != normalized_old_path
            ):
                raise LookupError("下载任务的完成文件已经变化，已停止覆盖较新的结果")

            media_row = connection.execute(
                """SELECT id FROM media_items
                WHERE video_path=? AND source_url=?
                ORDER BY id DESC LIMIT 1""",
                (normalized_old_path, str(task_row["url"] or "")),
            ).fetchone()
            if media_row is None:
                raise LookupError("已完成任务对应的媒体库记录不存在或归属不匹配")

            connection.execute(
                """UPDATE download_tasks
                SET status='completed', progress=100, error='', media_path=?,
                    transcode_codec=?, transcode_device=?, transcode_encoder=?,
                    updated_at=datetime('now')
                WHERE id=?""",
                (
                    normalized_new_path,
                    str(transcode_codec),
                    str(transcode_device),
                    str(transcode_encoder),
                    normalized_task_id,
                ),
            )
            connection.execute(
                "UPDATE media_items SET video_path=? WHERE id=?",
                (normalized_new_path, int(media_row["id"])),
            )
            if normalized_old_path != normalized_new_path:
                connection.execute(
                    "DELETE FROM download_task_files WHERE task_id=? AND path=?",
                    (normalized_task_id, normalized_old_path),
                )
            connection.execute(
                """INSERT INTO download_task_files(task_id,path,kind,managed)
                VALUES(?,?,'media',1)
                ON CONFLICT(task_id,path) DO UPDATE SET
                    kind='media', managed=MIN(download_task_files.managed, excluded.managed)""",
                (normalized_task_id, normalized_new_path),
            )

    def list_media(self, limit: int | None = None, offset: int = 0) -> list[MediaItem]:
        """Return newest media, optionally as a bounded catalog page.

        The completed-media page renders cards incrementally, so eagerly
        decoding every historical row (including JSON tags) wastes startup
        time and memory for large libraries.  Keeping ``limit=None`` preserves
        the original API for exports/tests while the UI can request pages.
        """
        normalized_offset = max(0, int(offset or 0))
        query = "SELECT * FROM media_items ORDER BY id DESC"
        parameters: tuple[int, ...] = ()
        if limit is not None:
            normalized_limit = max(0, int(limit))
            if normalized_limit == 0:
                return []
            query += " LIMIT ? OFFSET ?"
            parameters = (normalized_limit, normalized_offset)
        elif normalized_offset:
            query += " LIMIT -1 OFFSET ?"
            parameters = (normalized_offset,)
        with self._lock:
            rows = self.conn.execute(query, parameters).fetchall()
        return [_media_item_from_row(row) for row in rows]

    def count_media(self) -> int:
        """Return catalog size without materializing media rows."""
        with self._lock:
            row = self.conn.execute("SELECT COUNT(*) FROM media_items").fetchone()
        return max(0, int(row[0] if row else 0))

    def get_media(self, media_id: int) -> MediaItem | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM media_items WHERE id=?", (media_id,)).fetchone()
        if row is None:
            return None
        return _media_item_from_row(row)

    def get_latest_media_for_url(self, source_url: str) -> MediaItem | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM media_items WHERE source_url=? ORDER BY id DESC LIMIT 1", (source_url,)).fetchone()
        if not row:
            return None
        return _media_item_from_row(row)

    def latest_media_by_source_urls(self, source_urls: Iterable[str]) -> dict[str, MediaItem]:
        """Load the newest media rows for many task URLs without N+1 queries."""
        urls = list(dict.fromkeys(str(url or "") for url in source_urls if str(url or "")))
        if not urls:
            return {}
        rows: list[sqlite3.Row] = []
        with self._lock:
            # Stay comfortably below SQLite's variable limit used by older
            # Windows builds while keeping each restore query reasonably large.
            for offset in range(0, len(urls), 400):
                chunk = urls[offset:offset + 400]
                placeholders = ",".join("?" for _ in chunk)
                rows.extend(
                    self.conn.execute(
                        f"""SELECT media.* FROM media_items AS media
                        INNER JOIN (
                            SELECT source_url, MAX(id) AS latest_id
                            FROM media_items
                            WHERE source_url IN ({placeholders})
                            GROUP BY source_url
                        ) AS latest ON latest.latest_id = media.id""",
                        chunk,
                    ).fetchall()
                )
        return {
            str(row["source_url"]): _media_item_from_row(row)
            for row in rows
        }

    @staticmethod
    def _publish_task_values(
        task: PublishTask,
        *,
        idempotency_key: str | None,
    ) -> tuple[Any, ...]:
        return (
            task.media_id,
            task.platform,
            task.account,
            task.status,
            task.title,
            task.description,
            json.dumps(task.topics, ensure_ascii=False),
            json.dumps(task.settings, ensure_ascii=False),
            idempotency_key,
            task.result,
            task.created_at,
        )

    def get_or_add_publish_task(self, task: PublishTask) -> tuple[int, bool]:
        """Atomically create a publication target or return its existing id."""

        idempotency_key = str(task.idempotency_key or "").strip()
        values = self._publish_task_values(
            task,
            idempotency_key=idempotency_key or None,
        )
        with self._immediate_transaction() as connection:
            if not idempotency_key:
                cursor = connection.execute(
                    """INSERT INTO publish_tasks(media_id,platform,account,status,title,description,
                    topics,settings,idempotency_key,result,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    values,
                )
                return int(cursor.lastrowid), True
            cursor = connection.execute(
                """INSERT INTO publish_tasks(media_id,platform,account,status,title,description,
                topics,settings,idempotency_key,result,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(idempotency_key) DO NOTHING""",
                values,
            )
            if cursor.rowcount == 1:
                return int(cursor.lastrowid), True
            existing = connection.execute(
                "SELECT id FROM publish_tasks WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing is None:
                raise sqlite3.IntegrityError("发布任务幂等写入未返回现有记录")
            return int(existing["id"]), False

    def add_publish_task(self, task: PublishTask) -> int:
        """Create a publication task without replacing a prior result."""

        task_id, _created = self.get_or_add_publish_task(task)
        return task_id

    def list_publish_tasks(
        self,
        limit: int | None = None,
        offset: int = 0,
        media_id: int | None = None,
    ) -> list[sqlite3.Row]:
        """Return publication tasks as a bounded newest-first page."""
        clauses: list[str] = []
        parameters: list[int] = []
        if media_id is not None and int(media_id or 0) > 0:
            clauses.append("media_id=?")
            parameters.append(int(media_id))
        query = "SELECT * FROM publish_tasks"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id DESC"
        normalized_offset = max(0, int(offset or 0))
        if limit is not None:
            normalized_limit = max(0, int(limit))
            if normalized_limit == 0:
                return []
            query += " LIMIT ? OFFSET ?"
            parameters.extend((normalized_limit, normalized_offset))
        elif normalized_offset:
            query += " LIMIT -1 OFFSET ?"
            parameters.append(normalized_offset)
        with self._lock:
            return self.conn.execute(query, tuple(parameters)).fetchall()

    def count_publish_tasks(self, media_id: int | None = None) -> int:
        query = "SELECT COUNT(*) FROM publish_tasks"
        parameters: tuple[int, ...] = ()
        if media_id is not None and int(media_id or 0) > 0:
            query += " WHERE media_id=?"
            parameters = (int(media_id),)
        with self._lock:
            row = self.conn.execute(query, parameters).fetchone()
        return max(0, int(row[0] if row else 0))

    def get_publish_task(self, task_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute("SELECT * FROM publish_tasks WHERE id=?", (task_id,)).fetchone()

    def publish_statuses_by_media(self, media_id: int | None = None) -> dict[int, dict[str, str]]:
        """Return one distribution state per media/platform.

        A media item may have more than one task for the same platform when
        different accounts are used.  Once any account succeeds the platform
        is considered distributed; otherwise the newest task describes the
        actionable state shown in the completed-media catalog. ``media_id``
        limits the same reduction query to one card for live UI updates.
        """
        selected_media_id = int(media_id or 0)
        media_filter = " AND media_id=?" if selected_media_id > 0 else ""
        parameters = (selected_media_id,) if selected_media_id > 0 else ()
        with self._lock:
            rows = self.conn.execute(
                f"""WITH ranked AS (
                    SELECT media_id, platform, status,
                           ROW_NUMBER() OVER (
                               PARTITION BY media_id, platform
                               ORDER BY CASE WHEN status='success' THEN 0 ELSE 1 END, id DESC
                           ) AS rank_order
                    FROM publish_tasks
                    WHERE media_id > 0 AND TRIM(platform) <> ''{media_filter}
                )
                SELECT media_id, platform, status
                FROM ranked
                WHERE rank_order = 1""",
                parameters,
            ).fetchall()
        statuses: dict[int, dict[str, str]] = {}
        for row in rows:
            media_id = int(row["media_id"] or 0)
            platform = str(row["platform"] or "").strip()
            state = str(row["status"] or "pending")
            statuses.setdefault(media_id, {})[platform] = state
        return statuses

    def publish_statuses_for_media(self, media_id: int) -> dict[str, str]:
        """Return the reduced platform states for one completed-media card."""
        selected_media_id = int(media_id or 0)
        if selected_media_id <= 0:
            return {}
        return self.publish_statuses_by_media(selected_media_id).get(selected_media_id, {})

    def publish_statuses_for_media_ids(self, media_ids: Iterable[int]) -> dict[int, dict[str, str]]:
        """Load reduced publish states only for the visible catalog pages."""
        ids = list(dict.fromkeys(int(value or 0) for value in media_ids if int(value or 0) > 0))
        if not ids:
            return {}
        statuses: dict[int, dict[str, str]] = {}
        with self._lock:
            for offset in range(0, len(ids), 400):
                chunk = ids[offset:offset + 400]
                placeholders = ",".join("?" for _ in chunk)
                rows = self.conn.execute(
                    f"""WITH ranked AS (
                        SELECT media_id, platform, status,
                               ROW_NUMBER() OVER (
                                   PARTITION BY media_id, platform
                                   ORDER BY CASE WHEN status='success' THEN 0 ELSE 1 END, id DESC
                               ) AS rank_order
                        FROM publish_tasks
                        WHERE media_id IN ({placeholders}) AND TRIM(platform) <> ''
                    )
                    SELECT media_id, platform, status
                    FROM ranked
                    WHERE rank_order = 1""",
                    chunk,
                ).fetchall()
                for row in rows:
                    media_id = int(row["media_id"] or 0)
                    platform = str(row["platform"] or "").strip()
                    statuses.setdefault(media_id, {})[platform] = str(row["status"] or "pending")
        return statuses

    def media_distribution_counts(self, target_platforms: Iterable[str]) -> dict[str, int]:
        """Aggregate completed-page metrics in SQLite without loading the catalog."""
        targets = tuple(dict.fromkeys(str(value or "").strip() for value in target_platforms if str(value or "").strip()))
        target_placeholders = ",".join("?" for _ in targets)
        successful_targets = (
            f"SUM(CASE WHEN reduced.platform IN ({target_placeholders}) "
            "AND reduced.status='success' THEN 1 ELSE 0 END)"
            if targets
            else "0"
        )
        parameters: tuple[str, ...] = targets
        with self._lock:
            row = self.conn.execute(
                f"""WITH ranked AS (
                    SELECT media_id, platform, status,
                           ROW_NUMBER() OVER (
                               PARTITION BY media_id, platform
                               ORDER BY CASE WHEN status='success' THEN 0 ELSE 1 END, id DESC
                           ) AS rank_order
                    FROM publish_tasks
                    WHERE media_id > 0 AND TRIM(platform) <> ''
                ), reduced AS (
                    SELECT media_id, platform, status FROM ranked WHERE rank_order = 1
                ), flags AS (
                    SELECT media.id AS media_id,
                           MAX(CASE WHEN reduced.status='success' THEN 1 ELSE 0 END) AS has_success,
                           MAX(CASE WHEN reduced.status IN ('pending','uploading') THEN 1 ELSE 0 END) AS has_active,
                           MAX(CASE WHEN reduced.status='failed' THEN 1 ELSE 0 END) AS has_failed,
                           CASE WHEN {successful_targets} = {len(targets)} AND {len(targets)} > 0
                                THEN 1 ELSE 0 END AS is_complete
                    FROM media_items AS media
                    LEFT JOIN reduced ON reduced.media_id = media.id
                    GROUP BY media.id
                )
                SELECT COUNT(*) AS total,
                       COALESCE(SUM(has_success), 0) AS published,
                       COALESCE(SUM(has_active), 0) AS active,
                       COALESCE(SUM(has_failed), 0) AS failed,
                       COALESCE(SUM(is_complete), 0) AS complete
                FROM flags""",
                parameters,
            ).fetchone()
        total = max(0, int(row["total"] if row else 0))
        complete = max(0, int(row["complete"] if row else 0))
        return {
            "all": total,
            "needs_distribution": max(0, total - complete),
            "published": max(0, int(row["published"] if row else 0)),
            "queued": max(0, int(row["active"] if row else 0)),
            "retry_needed": max(0, int(row["failed"] if row else 0)),
            "complete": complete,
        }

    def update_publish_status(self, task_id: int, status: str, result: str = "") -> None:
        with self._immediate_transaction() as connection:
            connection.execute(
                "UPDATE publish_tasks SET status=?, result=? WHERE id=?",
                (status, result, task_id),
            )

    def recover_interrupted_publish_tasks(self, result: str) -> int:
        """Fail every interrupted upload in one set-based transaction."""
        with self._immediate_transaction() as connection:
            cursor = connection.execute(
                "UPDATE publish_tasks SET status='failed', result=? WHERE status='uploading'",
                (result,),
            )
            return max(0, int(cursor.rowcount))

    def upsert_download_task(self, task) -> None:
        self.upsert_download_tasks((task,))

    def insert_download_task(self, task) -> None:
        """Insert a brand-new task without replacing an existing task id."""

        values = self._download_task_upsert_values(task)
        with self._immediate_transaction() as connection:
            connection.execute(_DOWNLOAD_TASK_INSERT_SQL, values)

    @staticmethod
    def _download_task_update_values(task) -> tuple[Any, ...]:
        values = Database._download_task_upsert_values(task)
        return (*values[1:], values[0])

    def update_download_task(self, task) -> None:
        """Update an existing task and refuse to recreate a deleted record."""

        values = self._download_task_update_values(task)
        with self._immediate_transaction() as connection:
            cursor = connection.execute(_DOWNLOAD_TASK_UPDATE_SQL, values)
            if cursor.rowcount != 1:
                raise LookupError(f"下载任务记录不存在：{task.id}")

    def update_download_tasks(self, tasks: Iterable) -> None:
        """Atomically update existing tasks without UPSERT resurrection."""

        prepared = [
            (task, self._download_task_update_values(task))
            for task in tasks
        ]
        if not prepared:
            return
        with self._immediate_transaction() as connection:
            for task, values in prepared:
                cursor = connection.execute(_DOWNLOAD_TASK_UPDATE_SQL, values)
                if cursor.rowcount != 1:
                    raise LookupError(f"下载任务记录不存在：{task.id}")

    def materialize_download_tasks(self, parent, children: Iterable) -> None:
        """Atomically update a collection parent and insert only new children."""

        parent_values = self._download_task_update_values(parent)
        child_rows = [self._download_task_upsert_values(child) for child in children]
        with self._immediate_transaction() as connection:
            cursor = connection.execute(_DOWNLOAD_TASK_UPDATE_SQL, parent_values)
            if cursor.rowcount != 1:
                raise LookupError(f"下载任务记录不存在：{parent.id}")
            if child_rows:
                connection.executemany(_DOWNLOAD_TASK_INSERT_SQL, child_rows)

    @staticmethod
    def _download_task_upsert_values(task) -> tuple[Any, ...]:
        """Serialize one task before acquiring SQLite's write lock."""

        return (
            task.id,
            getattr(task, "task_kind", "video"),
            getattr(task, "parent_task_id", ""),
            getattr(task, "root_task_id", ""),
            getattr(task, "source_key", ""),
            int(getattr(task, "collection_index", 0) or 0),
            json.dumps(
                getattr(task, "options_json", {}) or {},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            task.url,
            task.output_dir,
            task.quality,
            int(task.download_album),
            task.playlist_mode,
            task.proxy,
            task.cookie_file,
            task.filename_template,
            task.ffmpeg_path,
            task.format_selector,
            getattr(task, "cookie_source", "none"),
            getattr(task, "cookie_browser", "chrome"),
            getattr(task, "cookie_profile", ""),
            getattr(task, "cookie_keyring", ""),
            getattr(task, "cookie_container", ""),
            getattr(task, "transcode_codec", "original"),
            getattr(task, "transcode_device", "auto"),
            getattr(task, "transcode_encoder", ""),
            getattr(task, "subtitle_language", "none"),
            task.title,
            task.status,
            task.progress,
            task.speed,
            task.speed_bps,
            task.downloaded_bytes,
            task.total_bytes,
            task.eta,
            task.size,
            task.error,
            task.media_path,
            task.thumbnail_path,
            task.created_at,
        )

    def upsert_download_tasks(self, tasks: Iterable) -> None:
        rows = [self._download_task_upsert_values(task) for task in tasks]
        if not rows:
            return
        with self._immediate_transaction() as connection:
            connection.executemany(_DOWNLOAD_TASK_UPSERT_SQL, rows)

    def list_download_tasks(self) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM download_tasks ORDER BY created_at ASC, collection_index ASC"
            ).fetchall()

    def existing_download_task_ids(self, task_ids: Iterable[str]) -> set[str]:
        """Return existing IDs in bounded queries for stale-batch isolation."""

        normalized: list[str] = []
        seen: set[str] = set()
        for task_id in task_ids:
            value = str(task_id or "")
            if value and value not in seen:
                seen.add(value)
                normalized.append(value)
        existing: set[str] = set()
        with self._lock:
            for offset in range(0, len(normalized), 400):
                chunk = normalized[offset:offset + 400]
                placeholders = ",".join("?" for _value in chunk)
                rows = self.conn.execute(
                    f"SELECT id FROM download_tasks WHERE id IN ({placeholders})",
                    chunk,
                ).fetchall()
                existing.update(str(row["id"]) for row in rows)
        return existing

    def list_download_task_tree(self, root_task_id: str) -> list[sqlite3.Row]:
        """Return one task and every descendant reachable through parent ids."""

        with self._lock:
            return self.conn.execute(
                _DOWNLOAD_TASK_SUBTREE_CTE
                + """
                SELECT task.*
                FROM download_tasks AS task
                JOIN task_tree ON task_tree.id=task.id
                ORDER BY task.collection_index DESC, task.id""",
                (str(root_task_id),),
            ).fetchall()

    def list_download_task_files(self, task_id: str, *, include_tree: bool = False) -> list[sqlite3.Row]:
        with self._lock:
            if include_tree:
                return self.conn.execute(
                    _DOWNLOAD_TASK_SUBTREE_CTE
                    + """
                    SELECT file.*
                    FROM download_task_files AS file
                    JOIN task_tree ON task_tree.id=file.task_id
                    ORDER BY file.task_id, file.path""",
                    (str(task_id),),
                ).fetchall()
            return self.conn.execute(
                "SELECT * FROM download_task_files WHERE task_id=? ORDER BY path",
                (str(task_id),),
            ).fetchall()

    def upsert_collection_probe_entries(
        self,
        parent_task_id: str,
        entries: Iterable[dict],
    ) -> int:
        rows = [
            (
                str(parent_task_id), int(entry.get("index") or 0),
                str(entry.get("source_key") or ""), str(entry.get("url") or ""),
                str(entry.get("title") or ""), str(entry.get("uploader") or ""),
                float(entry.get("duration") or 0), str(entry.get("upload_date") or ""),
                str(entry.get("thumbnail") or ""), str(entry.get("live_status") or ""),
                str(entry.get("availability") or ""), str(entry.get("entry_kind") or "video"),
                int(bool(entry.get("downloadable"))), str(entry.get("disabled_reason") or ""),
                int(bool(entry.get("selected"))), int(bool(entry.get("completed"))),
                max(0, int(entry.get("estimated_bytes") or 0)),
            )
            for entry in entries
            if int(entry.get("index") or 0) > 0
        ]
        if not rows:
            return 0
        with self._immediate_transaction() as connection:
            connection.executemany(
                """INSERT INTO collection_probe_entries
                (parent_task_id,collection_index,source_key,url,title,uploader,duration,
                 upload_date,thumbnail,live_status,availability,entry_kind,downloadable,
                 disabled_reason,selected,completed,estimated_bytes)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(parent_task_id,collection_index) DO UPDATE SET
                 source_key=excluded.source_key,url=excluded.url,title=excluded.title,
                 uploader=excluded.uploader,duration=excluded.duration,
                 upload_date=excluded.upload_date,thumbnail=excluded.thumbnail,
                 live_status=excluded.live_status,availability=excluded.availability,
                 entry_kind=excluded.entry_kind,downloadable=excluded.downloadable,
                 disabled_reason=excluded.disabled_reason,selected=excluded.selected,
                 completed=excluded.completed,estimated_bytes=excluded.estimated_bytes""",
                rows,
            )
        return len(rows)

    @staticmethod
    def _collection_entry_dict(row: sqlite3.Row) -> dict:
        return {
            "index": int(row["collection_index"] or 0),
            "source_key": str(row["source_key"] or ""),
            "url": str(row["url"] or ""),
            "title": str(row["title"] or ""),
            "uploader": str(row["uploader"] or ""),
            "duration": float(row["duration"] or 0),
            "upload_date": str(row["upload_date"] or ""),
            "thumbnail": str(row["thumbnail"] or ""),
            "live_status": str(row["live_status"] or ""),
            "availability": str(row["availability"] or ""),
            "entry_kind": str(row["entry_kind"] or "video"),
            "downloadable": bool(row["downloadable"]),
            "disabled_reason": str(row["disabled_reason"] or ""),
            "selected": bool(row["selected"]),
            "completed": bool(row["completed"]),
            "estimated_bytes": max(0, int(row["estimated_bytes"] or 0)),
        }

    @staticmethod
    def _collection_probe_view_clause(
        parent_task_id: str,
        *,
        query: str = "",
        state: str = "all",
        date_after: str = "",
        date_before: str = "",
        duration_min: int = 0,
        duration_max: int = 0,
        entry_kind: str = "",
    ) -> tuple[str, list[object]]:
        clauses = ["parent_task_id=?"]
        parameters: list[object] = [str(parent_task_id)]
        query_text = str(query or "").strip().casefold()
        if query_text:
            clauses.append("(LOWER(title) LIKE ? OR LOWER(uploader) LIKE ? OR LOWER(url) LIKE ?)")
            pattern = f"%{query_text}%"
            parameters.extend((pattern, pattern, pattern))
        normalized_state = str(state or "all").strip().casefold()
        if normalized_state == "available":
            clauses.append("downloadable=1")
        elif normalized_state == "completed":
            clauses.append("completed=1")
        elif normalized_state == "unavailable":
            clauses.append("downloadable=0")
        elif normalized_state == "live":
            clauses.append("live_status<>''")
        after = str(date_after or "").replace("-", "")[:8]
        before = str(date_before or "").replace("-", "")[:8]
        if after:
            clauses.append("(upload_date='' OR REPLACE(upload_date,'-','')>=?)")
            parameters.append(after)
        if before:
            clauses.append("(upload_date='' OR REPLACE(upload_date,'-','')<=?)")
            parameters.append(before)
        minimum = max(0, int(duration_min or 0))
        maximum = max(0, int(duration_max or 0))
        if minimum:
            clauses.append("(duration<=0 OR duration>=?)")
            parameters.append(minimum)
        if maximum:
            clauses.append("(duration<=0 OR duration<=?)")
            parameters.append(maximum)
        normalized_kind = str(entry_kind or "").strip().casefold()
        if normalized_kind in {"video", "collection"}:
            clauses.append("entry_kind=?")
            parameters.append(normalized_kind)
        return " AND ".join(clauses), parameters

    def collection_probe_entry_count(
        self,
        parent_task_id: str,
        *,
        query: str = "",
        state: str = "all",
        date_after: str = "",
        date_before: str = "",
        duration_min: int = 0,
        duration_max: int = 0,
        entry_kind: str = "",
    ) -> int:
        where, parameters = self._collection_probe_view_clause(
            parent_task_id,
            query=query,
            state=state,
            date_after=date_after,
            date_before=date_before,
            duration_min=duration_min,
            duration_max=duration_max,
            entry_kind=entry_kind,
        )
        with self._lock:
            row = self.conn.execute(
                f"SELECT COUNT(*) FROM collection_probe_entries WHERE {where}",
                parameters,
            ).fetchone()
        return max(0, int(row[0] if row else 0))

    def list_collection_probe_entries(
        self,
        parent_task_id: str,
        *,
        offset: int = 0,
        limit: int = 200,
        selected_only: bool = False,
        query: str = "",
        state: str = "all",
        date_after: str = "",
        date_before: str = "",
        duration_min: int = 0,
        duration_max: int = 0,
        sort_column: str = "collection_index",
        sort_descending: bool = False,
        entry_kind: str = "",
        random_seed: int = 0,
    ) -> list[dict]:
        where, parameters = self._collection_probe_view_clause(
            parent_task_id,
            query=query,
            state=state,
            date_after=date_after,
            date_before=date_before,
            duration_min=duration_min,
            duration_max=duration_max,
            entry_kind=entry_kind,
        )
        if selected_only:
            where += " AND selected=1 AND downloadable=1"
        order_columns = {
            "collection_index": "collection_index",
            "title": "title COLLATE NOCASE",
            "uploader": "uploader COLLATE NOCASE",
            "duration": "duration",
            "upload_date": "upload_date",
            "live_status": "live_status COLLATE NOCASE",
            "availability": "completed DESC, availability COLLATE NOCASE",
        }
        order_parameters: tuple[object, ...] = ()
        if str(sort_column or "") == "random":
            order_by = "((collection_index * ?) % 2147483647)"
            order_parameters = (max(1, int(random_seed or 1)),)
        else:
            order_by = order_columns.get(str(sort_column or ""), "collection_index")
        direction = "DESC" if sort_descending else "ASC"
        with self._lock:
            rows = self.conn.execute(
                f"""SELECT * FROM collection_probe_entries WHERE {where}
                ORDER BY {order_by} {direction}, collection_index ASC LIMIT ? OFFSET ?""",
                (*parameters, *order_parameters, max(1, int(limit)), max(0, int(offset))),
            ).fetchall()
        return [self._collection_entry_dict(row) for row in rows]

    def count_selected_collection_probe_entries(self, parent_task_id: str, entry_kind: str = "") -> int:
        kind = str(entry_kind or "").strip().casefold()
        kind_clause = " AND entry_kind=?" if kind in {"video", "collection"} else ""
        parameters: tuple[object, ...] = (
            (str(parent_task_id), kind) if kind_clause else (str(parent_task_id),)
        )
        with self._lock:
            row = self.conn.execute(
                f"""SELECT COUNT(*) FROM collection_probe_entries
                WHERE parent_task_id=? AND selected=1 AND downloadable=1{kind_clause}""",
                parameters,
            ).fetchone()
        return max(0, int(row[0] if row else 0))

    def collection_probe_storage_summary(self, parent_task_id: str) -> dict[str, int]:
        with self._lock:
            row = self.conn.execute(
                """SELECT COUNT(*) AS selected_count,
                   COALESCE(SUM(CASE WHEN estimated_bytes>0 THEN 1 ELSE 0 END),0) AS known_count,
                   COALESCE(SUM(estimated_bytes),0) AS estimated_bytes
                FROM collection_probe_entries
                WHERE parent_task_id=? AND selected=1 AND downloadable=1""",
                (str(parent_task_id),),
            ).fetchone()
        return {
            "selected_count": max(0, int(row["selected_count"] if row else 0)),
            "known_count": max(0, int(row["known_count"] if row else 0)),
            "estimated_bytes": max(0, int(row["estimated_bytes"] if row else 0)),
        }

    def set_collection_probe_entry_selected(
        self,
        parent_task_id: str,
        collection_index: int,
        selected: bool,
    ) -> None:
        with self._immediate_transaction() as connection:
            connection.execute(
                """UPDATE collection_probe_entries SET selected=?
                WHERE parent_task_id=? AND collection_index=? AND downloadable=1""",
                (int(bool(selected)), str(parent_task_id), int(collection_index)),
            )

    def set_collection_probe_selection(self, parent_task_id: str, mode: str) -> None:
        with self._immediate_transaction() as connection:
            if mode == "all":
                connection.execute(
                    """UPDATE collection_probe_entries SET selected=1
                    WHERE parent_task_id=? AND downloadable=1""",
                    (str(parent_task_id),),
                )
            elif mode == "none":
                connection.execute(
                    "UPDATE collection_probe_entries SET selected=0 WHERE parent_task_id=?",
                    (str(parent_task_id),),
                )
            else:
                connection.execute(
                    """UPDATE collection_probe_entries SET selected=CASE selected WHEN 1 THEN 0 ELSE 1 END
                    WHERE parent_task_id=? AND downloadable=1""",
                    (str(parent_task_id),),
                )

    def replace_download_task_files(
        self,
        task_id: str,
        files: Iterable[tuple[str, str, bool]],
    ) -> None:
        normalized_task_id = str(task_id)
        records = self._normalize_task_files(files)
        with self._immediate_transaction() as connection:
            exists = connection.execute(
                "SELECT 1 FROM download_tasks WHERE id=?",
                (normalized_task_id,),
            ).fetchone()
            if exists is None:
                raise LookupError(f"下载任务不存在：{normalized_task_id}")
            connection.execute(
                "DELETE FROM download_task_files WHERE task_id=?",
                (normalized_task_id,),
            )
            connection.executemany(
                "INSERT INTO download_task_files(task_id,path,kind,managed) VALUES(?,?,?,?)",
                (
                    (normalized_task_id, record.path, record.kind, record.managed)
                    for record in records
                ),
            )

    def completed_media_identities(self) -> tuple[set[str], set[str], set[str]]:
        with self._lock:
            task_rows = self.conn.execute(
                "SELECT source_key, url, title FROM download_tasks WHERE status='completed'"
            ).fetchall()
            media_rows = self.conn.execute(
                "SELECT source_url, title FROM media_items"
            ).fetchall()
        source_keys: set[str] = set()
        urls: set[str] = set()
        titles: set[str] = set()
        for source_key, url, title in task_rows:
            if source_key:
                source_keys.add(str(source_key))
            if url:
                urls.add(str(url))
            if title:
                titles.add(str(title))
        for url, title in media_rows:
            if url:
                urls.add(str(url))
            if title:
                titles.add(str(title))
        return source_keys, urls, titles

    def delete_download_task_tree(
        self,
        root_task_id: str,
        *,
        delete_media: bool = False,
    ) -> list[sqlite3.Row]:
        """Delete a collection subtree, returning its rows for file cleanup."""

        root_id = str(root_task_id)
        with self._immediate_transaction() as connection:
            rows = connection.execute(
                _DOWNLOAD_TASK_SUBTREE_CTE
                + """
                SELECT task.*
                FROM download_tasks AS task
                JOIN task_tree ON task_tree.id=task.id
                ORDER BY task.collection_index DESC, task.id""",
                (root_id,),
            ).fetchall()
            if delete_media:
                # Match the historical per-row behavior: prefer an exact
                # completed path, falling back to source URL only when the task
                # has no recorded media path.
                connection.execute(
                    _DOWNLOAD_TASK_SUBTREE_CTE
                    + """
                    DELETE FROM media_items
                    WHERE video_path IN (
                        SELECT task.media_path
                        FROM download_tasks AS task
                        JOIN task_tree ON task_tree.id=task.id
                        WHERE COALESCE(task.media_path, '')<>''
                    ) OR source_url IN (
                        SELECT task.url
                        FROM download_tasks AS task
                        JOIN task_tree ON task_tree.id=task.id
                        WHERE COALESCE(task.media_path, '')=''
                          AND COALESCE(task.url, '')<>''
                    )""",
                    (root_id,),
                )
            connection.execute(
                _DOWNLOAD_TASK_SUBTREE_CTE
                + """
                DELETE FROM download_task_files
                WHERE task_id IN (SELECT id FROM task_tree)""",
                (root_id,),
            )
            connection.execute(
                _DOWNLOAD_TASK_SUBTREE_CTE
                + """
                DELETE FROM collection_probe_entries
                WHERE parent_task_id IN (SELECT id FROM task_tree)""",
                (root_id,),
            )
            connection.execute(
                _DOWNLOAD_TASK_SUBTREE_CTE
                + """
                DELETE FROM download_tasks
                WHERE id IN (SELECT id FROM task_tree)""",
                (root_id,),
            )
        return rows

    def delete_download_task(self, task_id: str, source_url: str = "", media_path: str = "", delete_media: bool = False) -> None:
        with self._immediate_transaction() as connection:
            connection.execute("DELETE FROM download_task_files WHERE task_id=?", (task_id,))
            connection.execute("DELETE FROM collection_probe_entries WHERE parent_task_id=?", (task_id,))
            connection.execute("DELETE FROM download_tasks WHERE id=?", (task_id,))
            # The completed-media catalog is independent from the download queue.
            # Keep it when the user only removes a task record; remove it only
            # when the associated files are explicitly deleted as well.
            if delete_media:
                if media_path:
                    connection.execute("DELETE FROM media_items WHERE video_path=?", (media_path,))
                elif source_url:
                    connection.execute("DELETE FROM media_items WHERE source_url=?", (source_url,))

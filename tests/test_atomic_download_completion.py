from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.storage.database import Database
from app.storage.models import MediaItem, PublishTask


def make_task(task_id: str = "task-1", **overrides):
    values = {
        "id": task_id,
        "url": "https://example.com/watch/demo",
        "output_dir": "C:/downloads",
        "quality": "best",
        "download_album": False,
        "playlist_mode": "single",
        "proxy": "",
        "cookie_file": "",
        "filename_template": "%(title)s.%(ext)s",
        "ffmpeg_path": "",
        "format_selector": "",
        "title": "下载中",
        "status": "downloading",
        "progress": 42.0,
        "speed": "1 MiB/s",
        "speed_bps": 1024.0,
        "downloaded_bytes": 1024,
        "total_bytes": 2048,
        "eta": "1s",
        "size": "2 KiB",
        "error": "旧错误",
        "media_path": "",
        "thumbnail_path": "",
        "created_at": "2026-08-24T10:00:00",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class AtomicDownloadCompletionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "app.db")

    def tearDown(self) -> None:
        self.db.close()
        self.temp_dir.cleanup()

    def _persist_task(self, task=None):
        task = task or make_task()
        self.db.upsert_download_task(task)
        with self.db._lock:
            self.db.conn.execute(
                "UPDATE download_tasks SET updated_at='2000-01-01 00:00:00' WHERE id=?",
                (task.id,),
            )
            self.db.conn.commit()
        return task

    def _media_count(self) -> int:
        with self.db._lock:
            return int(self.db.conn.execute("SELECT COUNT(*) FROM media_items").fetchone()[0])

    def _task_row(self, task_id: str = "task-1"):
        with self.db._lock:
            return self.db.conn.execute(
                "SELECT * FROM download_tasks WHERE id=?",
                (task_id,),
            ).fetchone()

    def test_batch_completion_commits_all_media_and_task_once(self) -> None:
        task = self._persist_task(
            make_task(media_path="C:/downloads/playlist-last.mp4", thumbnail_path="C:/downloads/last.jpg")
        )
        items = [
            MediaItem(source_url="https://example.com/1", title="一", video_path="C:/downloads/1.mp4"),
            MediaItem(
                source_url="https://example.com/2",
                title="二",
                thumbnail_path="C:/downloads/2.jpg",
                video_path="C:/downloads/2.mp4",
            ),
        ]
        statements: list[str] = []
        self.db.conn.set_trace_callback(statements.append)
        try:
            media_ids = self.db.complete_download_task_batch(task, items)
        finally:
            self.db.conn.set_trace_callback(None)

        self.assertEqual(len(media_ids), 2)
        self.assertEqual([item.id for item in items], media_ids)
        self.assertEqual(self._media_count(), 2)
        row = self._task_row()
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["progress"], 100.0)
        self.assertEqual(row["error"], "")
        self.assertEqual(row["media_path"], task.media_path)
        self.assertEqual(row["thumbnail_path"], task.thumbnail_path)
        self.assertNotEqual(row["updated_at"], "2000-01-01 00:00:00")
        normalized = [statement.strip().upper() for statement in statements]
        self.assertEqual(sum(statement.startswith("BEGIN IMMEDIATE") for statement in normalized), 1)
        self.assertEqual(sum(statement == "COMMIT" for statement in normalized), 1)

    def test_completion_update_preserves_download_options_and_collection_identity(self) -> None:
        task = self._persist_task()
        with self.db._lock:
            self.db.conn.execute(
                """UPDATE download_tasks
                SET quality='4k', parent_task_id='parent-1', root_task_id='root-1',
                    source_key='youtube:video-1', collection_index=7,
                    options_json='{"container":"mkv","write_thumbnail":false}'
                WHERE id=?""",
                (task.id,),
            )
            self.db.conn.commit()
        item = MediaItem(
            source_url=task.url,
            video_path="C:/downloads/completed.mp4",
        )

        self.db.complete_download_task_batch(task, [item])

        row = self._task_row()
        self.assertEqual(row["quality"], "4k")
        self.assertEqual(row["parent_task_id"], "parent-1")
        self.assertEqual(row["root_task_id"], "root-1")
        self.assertEqual(row["source_key"], "youtube:video-1")
        self.assertEqual(row["collection_index"], 7)
        self.assertEqual(
            json.loads(row["options_json"]),
            {"container": "mkv", "write_thumbnail": False},
        )

    def test_single_item_wrapper_returns_id_and_uses_media_paths_as_fallback(self) -> None:
        task = self._persist_task()
        item = MediaItem(
            source_url="https://example.com/single",
            thumbnail_path="C:/downloads/single.jpg",
            video_path="C:/downloads/single.mp4",
        )

        media_id = self.db.complete_download_task(task, item)

        self.assertEqual(item.id, media_id)
        self.assertEqual(self.db.get_media(media_id).video_path, item.video_path)
        row = self._task_row()
        self.assertEqual(row["media_path"], item.video_path)
        self.assertEqual(row["thumbnail_path"], item.thumbnail_path)

    def test_missing_task_rolls_back_without_orphan_media(self) -> None:
        item = MediaItem(source_url="https://example.com/missing", video_path="C:/downloads/missing.mp4")

        with self.assertRaises(LookupError):
            self.db.complete_download_task(make_task("missing"), item)

        self.assertEqual(self._media_count(), 0)
        self.assertIsNone(item.id)
        self.assertFalse(self.db.conn.in_transaction)

    def test_empty_playlist_is_rejected_without_changing_task(self) -> None:
        task = self._persist_task()

        with self.assertRaisesRegex(ValueError, "至少需要一个媒体条目"):
            self.db.complete_download_task_batch(task, [])

        self.assertEqual(self._media_count(), 0)
        row = self._task_row()
        self.assertEqual(row["status"], "downloading")
        self.assertEqual(row["progress"], 42.0)
        self.assertEqual(row["error"], "旧错误")
        self.assertFalse(self.db.conn.in_transaction)

    def test_second_media_constraint_failure_rolls_back_first_insert_and_task_update(self) -> None:
        task = self._persist_task()
        first = MediaItem(source_url="https://example.com/first", video_path="C:/downloads/first.mp4")
        invalid = MediaItem(source_url=None, video_path="C:/downloads/invalid.mp4")  # type: ignore[arg-type]

        with self.assertRaises(sqlite3.IntegrityError):
            self.db.complete_download_task_batch(task, [first, invalid])

        self.assertEqual(self._media_count(), 0)
        self.assertIsNone(first.id)
        self.assertIsNone(invalid.id)
        row = self._task_row()
        self.assertEqual(row["status"], "downloading")
        self.assertEqual(row["progress"], 42.0)
        self.assertEqual(row["error"], "旧错误")
        self.assertFalse(self.db.conn.in_transaction)

    def test_interruption_during_playlist_serialization_rolls_back(self) -> None:
        task = self._persist_task()
        items = [
            MediaItem(source_url="https://example.com/first", tags=["ok"]),
            MediaItem(source_url="https://example.com/second", tags=["interrupt"]),
        ]
        real_dumps = json.dumps
        calls = 0

        def interrupt_on_second_tags(value, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt("simulated process interruption")
            return real_dumps(value, **kwargs)

        statements: list[str] = []
        self.db.conn.set_trace_callback(statements.append)
        with patch("app.storage.database.json.dumps", side_effect=interrupt_on_second_tags):
            try:
                with self.assertRaises(KeyboardInterrupt):
                    self.db.complete_download_task_batch(task, items)
            finally:
                self.db.conn.set_trace_callback(None)

        self.assertEqual(self._media_count(), 0)
        self.assertEqual([item.id for item in items], [None, None])
        self.assertEqual(self._task_row()["status"], "downloading")
        self.assertFalse(self.db.conn.in_transaction)
        self.assertFalse(any(
            statement.strip().upper().startswith("BEGIN") for statement in statements
        ))

    def test_duplicate_task_files_are_collapsed_with_safe_ownership(self) -> None:
        task = self._persist_task()
        item = MediaItem(
            source_url="https://example.com/duplicate-file",
            video_path="C:/downloads/shared.mp4",
        )

        self.db.complete_download_task_batch(task, [item], [
            (item.video_path, "sidecar", True),
            (item.video_path, "media", False),
            (item.video_path, "thumbnail", True),
        ])

        rows = self.db.list_download_task_files(task.id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["path"], item.video_path)
        self.assertEqual(rows[0]["kind"], "media")
        self.assertEqual(rows[0]["managed"], 0)

    def test_replacing_file_manifest_rolls_back_to_previous_rows_on_failure(self) -> None:
        task = self._persist_task()
        original = "C:/downloads/original.mp4"
        self.db.replace_download_task_files(task.id, [(original, "media", True)])
        with self.db._lock:
            self.db.conn.execute(
                """CREATE TRIGGER reject_bad_task_file
                BEFORE INSERT ON download_task_files
                WHEN NEW.kind='bad'
                BEGIN
                    SELECT RAISE(ABORT, 'simulated manifest failure');
                END"""
            )
            self.db.conn.commit()

        with self.assertRaises(sqlite3.IntegrityError):
            self.db.replace_download_task_files(task.id, [
                ("C:/downloads/new.mp4", "media", True),
                ("C:/downloads/bad.txt", "bad", True),
            ])

        rows = self.db.list_download_task_files(task.id)
        self.assertEqual([(row["path"], row["kind"]) for row in rows], [
            (original, "media"),
        ])
        self.assertFalse(self.db.conn.in_transaction)

    def test_replacing_file_manifest_rejects_orphan_task_id(self) -> None:
        with self.assertRaises(LookupError):
            self.db.replace_download_task_files(
                "missing-task",
                [("C:/downloads/orphan.mp4", "media", True)],
            )

        self.assertFalse(self.db.conn.in_transaction)

    def test_task_update_failure_rolls_back_inserted_media(self) -> None:
        task = self._persist_task()
        with self.db._lock:
            self.db.conn.execute(
                """CREATE TRIGGER reject_download_completion
                BEFORE UPDATE OF status ON download_tasks
                WHEN NEW.status='completed'
                BEGIN
                    SELECT RAISE(ABORT, 'simulated completion failure');
                END"""
            )
            self.db.conn.commit()

        with self.assertRaises(sqlite3.IntegrityError):
            self.db.complete_download_task(
                task,
                MediaItem(source_url="https://example.com/rejected", video_path="C:/downloads/rejected.mp4"),
            )

        self.assertEqual(self._media_count(), 0)
        row = self._task_row()
        self.assertEqual(row["status"], "downloading")
        self.assertEqual(row["progress"], 42.0)
        self.assertEqual(row["error"], "旧错误")
        self.assertFalse(self.db.conn.in_transaction)

    def test_single_media_insert_failure_releases_transaction_for_next_write(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.add_media(MediaItem(source_url=None))  # type: ignore[arg-type]

        self.assertEqual(self._media_count(), 0)
        self.assertFalse(self.db.conn.in_transaction)

        media_id = self.db.add_media(MediaItem(source_url="https://example.com/recovery"))
        self.assertGreater(media_id, 0)
        self.assertEqual(self._media_count(), 1)

    def test_publish_status_failure_rolls_back_and_releases_transaction(self) -> None:
        task_id = self.db.add_publish_task(PublishTask(
            media_id=1,
            platform="douyin",
            idempotency_key="atomic-publish-status",
        ))
        with self.db._lock:
            self.db.conn.execute(
                """CREATE TRIGGER reject_failed_publish_status
                BEFORE UPDATE OF status ON publish_tasks
                WHEN NEW.status='failed'
                BEGIN
                    SELECT RAISE(ABORT, 'simulated publish status failure');
                END"""
            )
            self.db.conn.commit()

        with self.assertRaisesRegex(sqlite3.IntegrityError, 'simulated publish status failure'):
            self.db.update_publish_status(task_id, "failed", "failure")

        row = self.db.get_publish_task(task_id)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["result"], "")
        self.assertFalse(self.db.conn.in_transaction)

        self.db.update_publish_status(task_id, "uploading")
        self.assertEqual(self.db.get_publish_task(task_id)["status"], "uploading")

    def test_tree_delete_failure_restores_tasks_manifests_and_probe_entries(self) -> None:
        root = make_task(
            "tree-root",
            task_kind="collection",
            root_task_id="tree-root",
        )
        child = make_task(
            "tree-child",
            url="https://example.com/watch/tree-child",
            parent_task_id=root.id,
            root_task_id=root.id,
            collection_index=1,
        )
        self.db.upsert_download_tasks([root, child])
        self.db.replace_download_task_files(
            child.id,
            [("C:/downloads/tree-child.mp4", "media", True)],
        )
        self.db.upsert_collection_probe_entries(root.id, [{
            "index": 1,
            "url": child.url,
        }])
        with self.db._lock:
            self.db.conn.execute(
                """CREATE TRIGGER reject_tree_root_delete
                BEFORE DELETE ON download_tasks
                WHEN OLD.id='tree-root'
                BEGIN
                    SELECT RAISE(ABORT, 'simulated tree delete failure');
                END"""
            )
            self.db.conn.commit()

        with self.assertRaisesRegex(sqlite3.IntegrityError, 'simulated tree delete failure'):
            self.db.delete_download_task_tree(root.id)

        self.assertEqual(
            {row["id"] for row in self.db.list_download_tasks()},
            {root.id, child.id},
        )
        self.assertEqual(len(self.db.list_download_task_files(child.id)), 1)
        self.assertEqual(len(self.db.list_collection_probe_entries(root.id)), 1)
        self.assertFalse(self.db.conn.in_transaction)

        with self.db._lock:
            self.db.conn.execute("DROP TRIGGER reject_tree_root_delete")
            self.db.conn.commit()
        deleted = self.db.delete_download_task_tree(root.id)
        self.assertEqual({row["id"] for row in deleted}, {root.id, child.id})
        self.assertEqual(self.db.list_download_tasks(), [])

    def test_nested_collection_delete_removes_only_its_recursive_subtree(self) -> None:
        root = make_task(
            "nested-root",
            task_kind="collection",
            root_task_id="nested-root",
        )
        nested = make_task(
            "nested-collection",
            task_kind="collection",
            parent_task_id=root.id,
            root_task_id=root.id,
            collection_index=1,
        )
        grandchild = make_task(
            "nested-video",
            url="https://example.com/watch/nested-video",
            parent_task_id=nested.id,
            root_task_id=root.id,
            collection_index=1,
            media_path="C:/downloads/nested-video.mp4",
        )
        sibling = make_task(
            "root-sibling",
            url="https://example.com/watch/root-sibling",
            parent_task_id=root.id,
            root_task_id=root.id,
            collection_index=2,
        )
        self.db.upsert_download_tasks((root, nested, grandchild, sibling))
        self.db.replace_download_task_files(
            grandchild.id,
            [(grandchild.media_path, "media", True)],
        )
        self.db.upsert_collection_probe_entries(nested.id, [{
            "index": 1,
            "url": grandchild.url,
        }])
        media = MediaItem(
            source_url=grandchild.url,
            video_path=grandchild.media_path,
        )
        self.db.add_media(media)

        listed = self.db.list_download_task_tree(nested.id)
        listed_files = self.db.list_download_task_files(nested.id, include_tree=True)
        deleted = self.db.delete_download_task_tree(nested.id, delete_media=True)

        self.assertEqual({row["id"] for row in listed}, {nested.id, grandchild.id})
        self.assertEqual(
            {(row["task_id"], row["path"]) for row in listed_files},
            {(grandchild.id, grandchild.media_path)},
        )
        self.assertEqual({row["id"] for row in deleted}, {nested.id, grandchild.id})
        self.assertEqual(
            {row["id"] for row in self.db.list_download_tasks()},
            {root.id, sibling.id},
        )
        self.assertEqual(self.db.list_download_task_files(grandchild.id), [])
        self.assertEqual(self.db.list_collection_probe_entries(nested.id), [])
        self.assertEqual(self._media_count(), 0)


if __name__ == "__main__":
    unittest.main()

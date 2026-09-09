from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from app.core.download_service import DownloadService, DownloadTask
from app.storage.database import Database
from app.ui.task_context_menu import task_menu_capabilities


class LocalHistoryImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        existing = QCoreApplication.instance()
        if existing is not None and not isinstance(existing, QApplication):
            raise unittest.SkipTest("a non-GUI QCoreApplication already exists")
        cls.app = existing or QApplication([])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = Database(self.root / "app.db")
        self.addCleanup(self.database.close)
        self.service = DownloadService(self.database)

    def test_imports_only_unrecorded_direct_nonempty_media_without_owning_files(self) -> None:
        video = self.root / "video.mp4"
        audio = self.root / "audio.m4a"
        known = self.root / "known.webm"
        video.write_bytes(b"video")
        audio.write_bytes(b"audio")
        known.write_bytes(b"known")
        (self.root / "empty.mp4").write_bytes(b"")
        (self.root / "unfinished.mp4.part").write_bytes(b"partial")
        nested = self.root / "nested"
        nested.mkdir()
        (nested / "nested.mp4").write_bytes(b"nested")

        known_task = DownloadTask(
            "known-task",
            "https://example.test/known",
            str(self.root),
            source_key="generic:known",
            title="Known remote",
            status="completed",
            media_path=str(known),
        )
        self.database.insert_download_task(known_task)

        self.assertEqual(
            {path.name for path in self.service.scan_local_history(self.root)},
            {"video.mp4", "audio.m4a"},
        )

        imported = self.service.import_local_history(
            self.service.scan_local_history(self.root),
            self.root,
        )

        self.assertEqual({task.title for task in imported}, {"video", "audio"})
        self.assertTrue(all(task.status == "completed" for task in imported))
        self.assertTrue(all(task.options_json.get("_local_history") for task in imported))
        self.assertEqual(
            next(task for task in imported if task.title == "audio").options_json[
                "content_mode"
            ],
            "audio",
        )
        self.assertEqual(self.service.scan_local_history(self.root), [])
        self.assertTrue(all(
            int(self.database.list_download_task_files(task.id)[0]["managed"]) == 0
            for task in imported
        ))
        self.assertTrue(all(
            not task_menu_capabilities(task).can_retry
            and not task_menu_capabilities(task).can_custom_redownload
            and not task_menu_capabilities(task).can_convert
            for task in imported
        ))
        self.assertIsNone(self.service.redownload(imported[0].id))
        self.assertFalse(
            self.service.convert_completed_task(imported[0].id, "original")
        )

        source_keys, urls, titles = self.database.completed_media_identities()
        self.assertEqual(source_keys, {"generic:known"})
        self.assertEqual(urls, {"https://example.test/known"})
        self.assertEqual(titles, {"Known remote"})

        self.database.close()
        restored_database = Database(self.root / "app.db")
        self.addCleanup(restored_database.close)
        restored_service = DownloadService(restored_database)
        restored_service.restore_tasks()
        restored_local = [
            task for task in restored_service.tasks.values()
            if task.options_json.get("_local_history")
        ]
        self.assertEqual({task.title for task in restored_local}, {"video", "audio"})

        deleted = next(task for task in restored_local if task.title == "video")
        self.assertTrue(restored_service.delete_task(deleted.id, delete_files=True))
        self.assertTrue(video.exists())


if __name__ == "__main__":
    unittest.main()

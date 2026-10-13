from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication

from app.presentation.database_lifecycle import QmlDatabaseLifecycle


class _Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    def close(self) -> None:
        pass


class _DownloadService:
    active_thread_count = 0

    def __init__(self, database: _Database) -> None:
        self.db = database

    def reset_task_cache(self) -> None:
        raise RuntimeError("cache clear failed")


class _PublishService:
    active_thread_count = 0

    def __init__(self, database: _Database) -> None:
        self.db = database


class _Runtime:
    def __init__(self, database: _Database) -> None:
        self.db = database
        self.download_service = _DownloadService(database)
        self.publish_service = _PublishService(database)

    def replace_database(self, database: _Database) -> None:
        self.db = database
        self.download_service.db = database
        self.publish_service.db = database


class QmlDatabaseLifecycleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def test_failed_cache_clear_keeps_replacement_database_unsafe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.db"
            current = _Database(path)
            runtime = _Runtime(current)
            replacement = _Database(path)

            def create_replacement(_path: Path) -> _Database:
                path.touch()
                return replacement

            lifecycle = QmlDatabaseLifecycle(runtime, lambda _database: None)
            with patch("app.presentation.database_lifecycle.Database", create_replacement):
                lifecycle.checkFile()

            self.assertFalse(lifecycle.persistenceAvailable)


if __name__ == "__main__":
    unittest.main()

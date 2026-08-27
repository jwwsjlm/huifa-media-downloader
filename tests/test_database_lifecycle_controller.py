from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from app.ui.database_lifecycle_controller import (
    DatabaseLifecycleController,
    database_recovery_notice,
)


class _Database:
    def __init__(self, path: Path, report=None, events: list[str] | None = None) -> None:
        self.path = path
        self.recovery_report = report or SimpleNamespace(
            requires_notice=False,
            status="new",
            detail="",
            quarantine_dir="",
        )
        self.closed = False
        self.events = events

    def close(self) -> None:
        self.closed = True
        if self.events is not None:
            self.events.append("close-old")


class _DownloadService:
    def __init__(self, database: _Database) -> None:
        self.db = database
        self.active_thread_count = 0
        self.reset_calls = 0
        self.reset_error: Exception | None = None

    def reset_task_cache(self) -> None:
        self.reset_calls += 1
        if self.reset_error is not None:
            raise self.reset_error


class _PublishService:
    def __init__(self, database: _Database) -> None:
        self.db = database
        self.active_thread_count = 0


class _Dashboard:
    def __init__(self) -> None:
        self.status = QLabel()
        self.clear_calls = 0
        self.clear_error: Exception | None = None

    def clear_tasks(self) -> None:
        self.clear_calls += 1
        if self.clear_error is not None:
            raise self.clear_error


class _Page:
    def __init__(self) -> None:
        self.refresh_calls = 0

    def refresh(self) -> None:
        self.refresh_calls += 1


class DatabaseLifecycleControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        existing = QCoreApplication.instance()
        if existing is not None and not isinstance(existing, QApplication):
            raise unittest.SkipTest("a non-GUI QCoreApplication already exists")
        cls.app = existing or QApplication([])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.path = self.root / "app.db"
        self.path.write_bytes(b"database")
        self.parent = QWidget()
        self.current = _Database(self.path)
        self.download_service = _DownloadService(self.current)
        self.publish_service = _PublishService(self.current)
        self.dashboard = _Dashboard()
        self.completed = _Page()
        self.publish_queue = _Page()
        self.statuses: list[tuple[str, int]] = []
        self.factory_calls: list[Path] = []

        def factory(path: Path) -> _Database:
            self.factory_calls.append(path)
            path.write_bytes(b"replacement")
            return _Database(path)

        self.database_patcher = patch(
            "app.ui.database_lifecycle_controller.Database",
            side_effect=factory,
        )
        self.database_constructor = self.database_patcher.start()
        self.addCleanup(self.database_patcher.stop)

        self.parent.db = self.current
        self.parent.download_service = self.download_service
        self.parent.publish_service = self.publish_service
        self.parent.dashboard = self.dashboard
        self.parent.completed = self.completed
        self.parent.publish_queue = self.publish_queue
        self.parent.shutdown_controller = SimpleNamespace(started=False)
        self.parent.statusBar = lambda: SimpleNamespace(
            showMessage=lambda message, timeout: self.statuses.append((message, timeout))
        )
        self.controller = DatabaseLifecycleController(self.parent)

    def tearDown(self) -> None:
        self.controller.stop()
        self.parent.close()
        self.parent.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()
        self.temporary.cleanup()

    def test_start_and_stop_own_the_watch_timer(self) -> None:
        self.assertFalse(self.controller.watching)
        self.controller.start()
        self.assertTrue(self.controller.watching)
        self.controller.stop()
        self.assertFalse(self.controller.watching)

    def test_recovery_presentation_keeps_schema_reset_distinct(self) -> None:
        report = SimpleNamespace(
            requires_notice=True,
            status="schema_reset",
            detail="arbitrary diagnostic text",
            quarantine_dir="",
        )
        title, summary, detail = database_recovery_notice(
            _Database(self.path, report),
        )
        self.assertEqual(title, "数据库结构已更新")
        self.assertIn("最新结构", summary)
        self.assertIn("不执行旧版本迁移", detail)

    def test_notice_is_suppressed_during_shutdown(self) -> None:
        self.current.recovery_report = SimpleNamespace(
            requires_notice=True,
            status="restored",
            detail="",
            quarantine_dir=str(self.root / "recovery"),
        )
        self.parent.shutdown_controller.started = True
        with patch(
            "app.ui.database_lifecycle_controller.QMessageBox.warning",
        ) as warning:
            self.controller.show_recovery_notice()
        warning.assert_not_called()
        self.assertEqual(self.statuses, [])

    def test_missing_database_waits_for_download_and_publish_threads(self) -> None:
        self.path.unlink()
        self.download_service.active_thread_count = 1
        self.controller.check_file()
        self.assertEqual(self.factory_calls, [])
        self.assertFalse(self.controller.persistence_available)

        self.download_service.active_thread_count = 0
        self.publish_service.active_thread_count = 1
        self.controller.check_file()
        self.assertEqual(self.factory_calls, [])

        self.publish_service.active_thread_count = 0
        self.controller.check_file()
        self.assertEqual(self.factory_calls, [self.path])
        self.assertTrue(self.controller.persistence_available)

    def test_replacement_is_constructed_before_old_database_is_closed(self) -> None:
        events: list[str] = []
        old = _Database(self.path, events=events)
        self.current = old
        self.parent.db = old
        self.download_service.db = old
        self.publish_service.db = old
        self.path.unlink()

        def factory(path: Path) -> _Database:
            self.assertFalse(old.closed)
            events.append("create-new")
            path.write_bytes(b"replacement")
            return _Database(path, events=events)

        self.database_constructor.side_effect = factory
        self.controller.check_file()

        self.assertEqual(events[:2], ["create-new", "close-old"])
        self.assertTrue(old.closed)
        self.assertIs(self.download_service.db, self.parent.db)
        self.assertIs(self.publish_service.db, self.parent.db)
        self.assertEqual(self.download_service.reset_calls, 1)
        self.assertEqual(self.dashboard.clear_calls, 1)
        self.assertEqual(self.completed.refresh_calls, 1)
        self.assertEqual(self.publish_queue.refresh_calls, 1)
        self.assertEqual(self.dashboard.status.text(), "任务数据库已清空")
        self.assertTrue(self.controller.persistence_available)

    def test_replacement_creation_failure_keeps_old_connection_open(self) -> None:
        self.path.unlink()
        self.database_constructor.side_effect = lambda _path: (_ for _ in ()).throw(
            RuntimeError("schema creation failed"),
        )

        self.controller.check_file()

        self.assertFalse(self.current.closed)
        self.assertIs(self.download_service.db, self.current)
        self.assertFalse(self.controller.persistence_available)
        self.assertIn("schema creation failed", self.statuses[-1][0])

    def test_failed_cache_clear_is_retried_before_persistence_is_safe(self) -> None:
        self.path.unlink()
        self.download_service.reset_error = RuntimeError("cache busy")
        self.controller.check_file()
        self.assertFalse(self.controller.persistence_available)
        self.assertEqual(self.download_service.reset_calls, 1)

        self.download_service.reset_error = None
        self.controller.check_file()
        self.assertEqual(self.download_service.reset_calls, 2)
        self.assertEqual(self.dashboard.clear_calls, 2)
        self.assertTrue(self.controller.persistence_available)


if __name__ == "__main__":
    unittest.main()

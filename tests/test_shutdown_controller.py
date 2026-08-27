from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QWidget

from app.ui.shutdown_controller import ShutdownController


class _Service:
    def __init__(self, *, stopped: bool = True) -> None:
        self.request_calls = 0
        self.shutdown_calls = 0
        self.request_error: Exception | None = None
        self.shutdown_error: Exception | None = None
        self.stopped = stopped
        self.active_thread_count = 0 if stopped else 1
        self.busy = not stopped

    def request_shutdown(self) -> None:
        self.request_calls += 1
        if self.request_error is not None:
            raise self.request_error

    def shutdown(self, *, timeout_ms: int) -> bool:
        self.shutdown_calls += 1
        if self.shutdown_error is not None:
            raise self.shutdown_error
        return self.stopped


class _RequestOwner:
    def __init__(self) -> None:
        self.request_calls = 0
        self.error: Exception | None = None

    def request_shutdown(self) -> None:
        self.request_calls += 1
        if self.error is not None:
            raise self.error


class _Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.closed = False
        self.upserts: list[list[object]] = []

    def upsert_download_tasks(self, tasks) -> None:
        self.upserts.append(list(tasks))

    def close(self) -> None:
        self.closed = True


class _DownloadService(_Service):
    def __init__(self, database: _Database) -> None:
        super().__init__()
        self.db = database
        self.tasks: dict[str, object] = {}
        self.reset_calls = 0

    def reset_task_cache(self) -> None:
        self.reset_calls += 1
        self.tasks.clear()


class _Dashboard(_RequestOwner):
    def __init__(self) -> None:
        super().__init__()
        self.collection_probe_running = False
        self.live_ids: set[str] | None = set()
        self.clear_calls = 0

    def _database_task_ids(self) -> set[str] | None:
        return self.live_ids

    def clear_tasks(self) -> None:
        self.clear_calls += 1


class _Settings(_RequestOwner):
    def __init__(self) -> None:
        super().__init__()
        self.local_core_version_check_running = False


class _DatabaseLifecycle:
    def __init__(self) -> None:
        self.stop_calls = 0
        self.persistence_available = True

    def stop(self) -> None:
        self.stop_calls += 1


class _Tabs:
    def __init__(self) -> None:
        self.enabled = True

    def setEnabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)


class _CoverService:
    def __init__(self) -> None:
        self.close_calls = 0
        self.error: Exception | None = None

    def close(self) -> None:
        self.close_calls += 1
        if self.error is not None:
            raise self.error


class _StatusSummary:
    def __init__(self) -> None:
        self.stop_calls = 0

    def stop(self) -> None:
        self.stop_calls += 1


class _CloseEvent:
    def __init__(self) -> None:
        self.accepted = False
        self.ignored = False

    def accept(self) -> None:
        self.accepted = True

    def ignore(self) -> None:
        self.ignored = True


class ShutdownControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        existing = QCoreApplication.instance()
        if existing is not None and not isinstance(existing, QApplication):
            raise unittest.SkipTest("a non-GUI QCoreApplication already exists")
        cls.app = existing or QApplication([])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database_path = self.root / "app.db"
        self.database_path.write_bytes(b"db")
        self.parent = QWidget()
        self.tabs = _Tabs()
        self.database = _Database(self.database_path)
        self.database_lifecycle = _DatabaseLifecycle()
        self.update_service = _Service()
        self.application_update_service = _Service()
        self.download_service = _DownloadService(self.database)
        self.publish_service = _Service()
        self.runtime_update_dialog = _RequestOwner()
        self.application_update_controller = _RequestOwner()
        self.desktop_notifications = _RequestOwner()
        self.dashboard = _Dashboard()
        self.settings = _Settings()
        self.task_status_summary = _StatusSummary()
        self.cover_service = _CoverService()
        self.statuses: list[tuple[str, int]] = []
        self.close_calls = 0
        self.force_exit_codes: list[int] = []
        self.now = 0.0
        self.parent.tabs = self.tabs
        self.parent.db = self.database
        self.parent.database_lifecycle_controller = self.database_lifecycle
        self.parent.update_service = self.update_service
        self.parent.runtime_update_dialog_controller = self.runtime_update_dialog
        self.parent.application_update_controller = self.application_update_controller
        self.parent.desktop_notification_controller = self.desktop_notifications
        self.parent.application_update_service = self.application_update_service
        self.parent.dashboard = self.dashboard
        self.parent.download_service = self.download_service
        self.parent.publish_service = self.publish_service
        self.parent.settings = self.settings
        self.parent.task_status_summary = self.task_status_summary
        self.parent.cover_service = self.cover_service
        self.parent.statusBar = lambda: SimpleNamespace(
            showMessage=lambda message, timeout: self.statuses.append((message, timeout))
        )
        self.parent.close = self._close_window
        self.controller = ShutdownController(
            self.parent,
            clock=lambda: self.now,
            force_exit=self.force_exit_codes.append,
        )

    def tearDown(self) -> None:
        self.controller._poll_timer.stop()
        self.controller._initial_poll.stop()
        self.controller._close_timer.stop()
        self.parent.close()
        self.parent.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()
        self.temporary.cleanup()

    def _close_window(self) -> None:
        self.close_calls += 1

    def test_begin_is_idempotent_and_one_request_failure_does_not_skip_others(self) -> None:
        self.update_service.request_error = RuntimeError("update stop failed")

        self.controller.begin()
        self.controller.begin()

        self.assertTrue(self.controller.started)
        self.assertTrue(self.controller.polling)
        self.assertFalse(self.tabs.enabled)
        self.assertEqual(self.database_lifecycle.stop_calls, 1)
        self.assertEqual(self.task_status_summary.stop_calls, 1)
        for owner in (
            self.runtime_update_dialog,
            self.application_update_controller,
            self.desktop_notifications,
            self.update_service,
            self.application_update_service,
            self.dashboard,
            self.download_service,
            self.publish_service,
            self.settings,
        ):
            self.assertEqual(owner.request_calls, 1)

    def test_unreadable_database_task_ids_do_not_clear_live_tasks(self) -> None:
        task = SimpleNamespace(status="downloading")
        self.download_service.tasks["active"] = task
        self.dashboard.live_ids = None

        self.controller.begin()
        self.controller.finish()

        self.assertEqual(self.download_service.reset_calls, 0)
        self.assertEqual(self.dashboard.clear_calls, 0)
        self.assertEqual(task.status, "paused")
        self.assertEqual(self.database.upserts, [[task]])

    def test_confirmed_empty_database_discards_stale_cache_without_rewrite(self) -> None:
        self.download_service.tasks["stale"] = SimpleNamespace(status="downloading")
        self.dashboard.live_ids = set()

        self.controller.begin()
        self.controller.finish()

        self.assertEqual(self.download_service.reset_calls, 1)
        self.assertEqual(self.dashboard.clear_calls, 1)
        self.assertEqual(self.database.upserts, [])

    def test_poll_exception_isolated_and_zero_activity_can_still_finish(self) -> None:
        self.update_service.shutdown_error = RuntimeError("poll failed")
        self.update_service.active_thread_count = 0

        self.controller.begin()
        self.controller.poll()

        self.assertTrue(self.controller.complete)
        self.assertEqual(self.application_update_service.shutdown_calls, 1)
        self.assertEqual(self.download_service.shutdown_calls, 1)
        self.assertEqual(self.publish_service.shutdown_calls, 1)
        self.assertTrue(self.database.closed)

    def test_cover_close_failure_does_not_prevent_database_close_or_completion(self) -> None:
        self.cover_service.error = RuntimeError("cover session failed")
        self.controller.begin()

        self.controller.finish()

        self.assertTrue(self.database.closed)
        self.assertTrue(self.controller.complete)
        crash_log = self.root / "logs" / "app-crash.log"
        self.assertTrue(crash_log.is_file())
        self.assertIn("关闭封面服务失败", crash_log.read_text(encoding="utf-8"))

    def test_status_feedback_failure_does_not_block_shutdown(self) -> None:
        self.controller.show_status = lambda _message, _timeout: (
            _ for _ in ()
        ).throw(RuntimeError("status bar deleted"))

        self.controller.begin()
        self.controller.poll()

        self.assertTrue(self.controller.complete)
        self.assertTrue(self.database.closed)
        crash_log = self.root / "logs" / "app-crash.log"
        self.assertIn(
            "更新退出状态提示失败",
            crash_log.read_text(encoding="utf-8"),
        )

    def test_waiting_status_reports_aggregate_activity_after_delay(self) -> None:
        self.update_service.stopped = False
        self.update_service.active_thread_count = 2
        self.download_service.stopped = False
        self.download_service.active_thread_count = 1
        self.controller.begin()
        self.now = 10.0

        self.controller.poll()

        message, timeout = self.statuses[-1]
        self.assertIn("3", message)
        self.assertIn("10", message)
        self.assertEqual(timeout, 0)
        self.assertFalse(self.controller.complete)

    def test_shutdown_forces_process_exit_after_bounded_wait(self) -> None:
        self.update_service.stopped = False
        self.update_service.active_thread_count = 1
        self.controller.begin()
        self.now = float(self.controller.FORCE_EXIT_AFTER_SECONDS)

        self.controller.poll()
        self.controller.poll()

        self.assertEqual(self.force_exit_codes, [0])
        self.assertFalse(self.controller.polling)
        self.assertIn("20", self.statuses[-1][0])

    def test_close_event_ignores_first_request_and_accepts_after_completion(self) -> None:
        first = _CloseEvent()
        self.controller.handle_close_event(first)
        self.assertTrue(first.ignored)
        self.assertFalse(first.accepted)
        self.assertTrue(self.controller.started)

        self.controller.finish()
        second = _CloseEvent()
        self.controller.handle_close_event(second)
        self.assertTrue(second.accepted)
        self.assertFalse(second.ignored)


if __name__ == "__main__":
    unittest.main()

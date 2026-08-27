from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QWidget

from app.storage.models import MediaItem
from app.ui.navigation import SidebarNavigation
from app.ui.publish_ui_controller import PublishUiController


class _View(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.dirty_calls = 0
        self.dirty = False
        self.dirty_error: Exception | None = None

    def mark_dirty(self) -> None:
        self.dirty_calls += 1
        if self.dirty_error is not None:
            raise self.dirty_error


class _Queue(_View):
    def __init__(self) -> None:
        super().__init__()
        self.refreshes: list[tuple[int, object]] = []
        self.focused: list[int] = []

    def refresh_task(self, task_id: int, row) -> None:
        self.refreshes.append((task_id, row))

    def focus_media(self, media: MediaItem) -> None:
        self.focused.append(int(media.id or 0))


class _Completed(_View):
    def __init__(self) -> None:
        super().__init__()
        self.refreshed_media: list[int] = []

    def refresh_media_distribution(self, media_id: int) -> None:
        self.refreshed_media.append(media_id)


class _Notifications:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def publish_finished(self, *args) -> None:
        self.calls.append(args)


class _Database:
    def __init__(self) -> None:
        self.row = {
            "id": 7,
            "media_id": 11,
            "platform": "youtube",
            "status": "success",
        }
        self.error: Exception | None = None
        self.calls = 0

    def get_publish_task(self, _task_id: int):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.row


class PublishUiControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        existing = QCoreApplication.instance()
        if existing is not None and not isinstance(existing, QApplication):
            raise unittest.SkipTest("a non-GUI QCoreApplication already exists")
        cls.app = existing or QApplication([])

    def setUp(self) -> None:
        self.parent = QWidget()
        self.tabs = SidebarNavigation(self.parent)
        self.home = QWidget()
        self.queue = _Queue()
        self.completed = _Completed()
        self.tabs.addTab(self.home, "Home")
        self.tabs.addTab(self.queue, "Queue")
        self.database = _Database()
        self.notifications = _Notifications()
        self.statuses: list[tuple[str, int]] = []
        self.created_pages: list[QWidget] = []

        def editor_factory(_window, media, _platforms):
            page = QWidget()
            page.setProperty("media_id", int(media.id or 0))
            self.created_pages.append(page)
            return page

        self.editor_patcher = patch(
            "app.ui.publish_ui_controller.PublishPage",
            side_effect=editor_factory,
        )
        self.editor_patcher.start()
        self.addCleanup(self.editor_patcher.stop)

        self.parent.db = self.database
        self.parent.tabs = self.tabs
        self.parent.publish_queue = self.queue
        self.parent.completed = self.completed
        self.parent.desktop_notification_controller = self.notifications
        self.parent.statusBar = lambda: SimpleNamespace(
            showMessage=lambda message, timeout: self.statuses.append((message, timeout))
        )
        self.controller = PublishUiController(self.parent)

    def tearDown(self) -> None:
        self.parent.close()
        self.parent.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()

    def test_same_media_reuses_existing_editor_navigation_item(self) -> None:
        media = MediaItem(id=11, title="demo")

        first = self.controller.open_editor(media, ("youtube",))
        second = self.controller.open_editor(media, ("bilibili",))

        self.assertIs(first, second)
        self.assertEqual(len(self.created_pages), 1)
        self.assertEqual(self.tabs.count(), 3)
        self.assertIs(self.tabs.currentWidget(), first)

    def test_completed_editor_is_removed_and_queue_receives_focus(self) -> None:
        page = self.controller.open_editor(MediaItem(id=11, title="demo"))
        self.assertEqual(self.tabs.count(), 3)

        self.controller.complete_editor(page)
        self.app.processEvents()

        self.assertEqual(self.tabs.count(), 2)
        self.assertIs(self.tabs.currentWidget(), self.queue)
        self.assertEqual(self.queue.dirty_calls, 1)
        self.assertEqual(self.completed.dirty_calls, 1)
        self.assertNotIn(11, self.controller._editors)

    def test_missing_publish_row_marks_views_dirty_without_second_query(self) -> None:
        self.database.row = None

        self.controller.status_changed(7, "failed", "removed")

        self.assertEqual(self.database.calls, 1)
        self.assertEqual(self.queue.refreshes, [])
        self.assertEqual(self.queue.dirty_calls, 1)
        self.assertEqual(self.completed.dirty_calls, 1)
        self.assertEqual(self.notifications.calls, [])

    def test_database_read_failure_is_contained_and_reported(self) -> None:
        self.database.error = RuntimeError("database is locked")

        self.controller.status_changed(7, "success", "done")

        self.assertEqual(self.queue.dirty_calls, 1)
        self.assertEqual(self.completed.dirty_calls, 1)
        self.assertIn("database is locked", self.statuses[-1][0])
        self.assertEqual(self.notifications.calls, [])

    def test_dirty_refresh_failure_does_not_escape_database_failure_handler(self) -> None:
        self.database.error = RuntimeError("database is locked")
        self.queue.dirty_error = RuntimeError("queue refresh repeated failure")
        self.completed.dirty_error = RuntimeError("media refresh repeated failure")

        self.controller.status_changed(7, "success", "done")

        self.assertTrue(self.queue.dirty)
        self.assertTrue(self.completed.dirty)
        self.assertIn("database is locked", self.statuses[-1][0])

    def test_terminal_status_refreshes_both_views_and_sends_notification(self) -> None:
        row = self.database.row

        self.controller.status_changed(7, "success", "published")

        self.assertEqual(self.queue.refreshes, [(7, row)])
        self.assertEqual(self.completed.refreshed_media, [11])
        self.assertEqual(len(self.notifications.calls), 1)
        self.assertEqual(self.notifications.calls[0][0:3], (7, "success", "published"))

    def test_remove_tab_keeps_navigation_button_indexes_aligned(self) -> None:
        first = self.controller.open_editor(MediaItem(id=11, title="first"))
        second = self.controller.open_editor(MediaItem(id=12, title="second"))
        first_index = self.tabs.indexOf(first)
        removed_button = self.tabs.navigationButton(first_index)

        removed = self.tabs.removeTab(first_index)

        self.assertIs(removed, first)
        self.assertTrue(removed_button.isHidden())
        self.assertIsNone(removed_button.parent())
        self.assertEqual(self.tabs.indexOf(second), 2)
        for index in range(self.tabs.count()):
            self.assertEqual(
                self.tabs.navigationButton(index).objectName(),
                f"navigationButton{index}",
            )

    def test_destroyed_editor_is_evicted_before_same_media_reopens(self) -> None:
        first = self.controller.open_editor(MediaItem(id=11, title="first"))
        self.tabs.removeTab(self.tabs.indexOf(first))
        first.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()

        second = self.controller.open_editor(MediaItem(id=11, title="second"))

        self.assertIsNot(first, second)
        self.assertIs(self.controller._editors[11], second)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QWidget

from app.core.download_service import DownloadTask
from app.ui.desktop_notification_controller import (
    DesktopNotificationController,
    desktop_notification_should_show,
)


class _Settings:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled

    def get_bool(self, key: str, default: bool = False) -> bool:
        if key == "desktop_notifications":
            return self.enabled
        return default


class _FakeTray:
    def __init__(self) -> None:
        self.visible = False
        self.messages: list[tuple[str, str, object, int]] = []
        self.error: Exception | None = None

    def isVisible(self) -> bool:
        return self.visible

    def show(self) -> None:
        self.visible = True

    def hide(self) -> None:
        self.visible = False

    def setVisible(self, visible: bool) -> None:
        self.visible = bool(visible)

    def showMessage(self, title: str, message: str, icon, timeout: int) -> None:
        if self.error is not None:
            raise self.error
        self.messages.append((title, message, icon, timeout))


class _FakeWindow(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.active = False
        self.minimized = False
        self.activations = 0

    def isActiveWindow(self) -> bool:
        return self.active

    def isMinimized(self) -> bool:
        return self.minimized

    def showNormal(self) -> None:
        self.minimized = False

    def show(self) -> None:
        pass

    def raise_(self) -> None:
        pass

    def activateWindow(self) -> None:
        self.activations += 1


class DesktopNotificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        existing = QCoreApplication.instance()
        if existing is not None and not isinstance(existing, QApplication):
            raise unittest.SkipTest("a non-GUI QCoreApplication already exists")
        cls.app = existing or QApplication([])

    def setUp(self) -> None:
        self.window = _FakeWindow()
        self.settings = _Settings(True)
        self.focused: list[int] = []
        self.selected_pages: list[object] = []
        self.queue = SimpleNamespace(
            focus_task=lambda task_id: self.focused.append(int(task_id)),
        )
        self.dashboard = SimpleNamespace()
        self.download_service = SimpleNamespace(tasks={})
        self.window.app_settings = self.settings
        self.window.settings = SimpleNamespace(
            desktop_notifications=SimpleNamespace(
                setEnabled=lambda _enabled: None,
            ),
        )
        self.window.tabs = SimpleNamespace(setCurrentWidget=self.selected_pages.append)
        self.window.dashboard = self.dashboard
        self.window.publish_queue = self.queue
        self.window.download_service = self.download_service
        self.window.desktop_notifications_available = True
        self.window.shutdown_controller = SimpleNamespace(started=False)
        self.controller = DesktopNotificationController(self.window)

    def tearDown(self) -> None:
        self.controller.request_shutdown()
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()

    def test_policy_only_notifies_when_enabled_available_and_in_background(self) -> None:
        self.assertTrue(desktop_notification_should_show(
            enabled=True,
            available=True,
            window_active=False,
            window_minimized=False,
            shutting_down=False,
        ))
        self.assertTrue(desktop_notification_should_show(
            enabled=True,
            available=True,
            window_active=True,
            window_minimized=True,
            shutting_down=False,
        ))
        self.assertFalse(desktop_notification_should_show(
            enabled=True,
            available=True,
            window_active=True,
            window_minimized=False,
            shutting_down=False,
        ))
        self.assertFalse(desktop_notification_should_show(
            enabled=False,
            available=True,
            window_active=False,
            window_minimized=False,
            shutting_down=False,
        ))
        self.assertFalse(desktop_notification_should_show(
            enabled=True,
            available=True,
            window_active=False,
            window_minimized=False,
            shutting_down=True,
        ))

    def test_show_notification_bounds_plain_text_and_records_click_target(self) -> None:
        tray = _FakeTray()
        self.controller.tray_icon = tray  # type: ignore[assignment]

        shown = self.controller.show(
            "  下载\n完成  ",
            "标题\n" + "x" * 500,
            QSystemTrayIcon.MessageIcon.Information,
            ("download", "task-1"),
        )

        self.assertTrue(shown)
        self.assertTrue(tray.visible)
        self.assertEqual(
            self.controller.notification_target,
            ("download", "task-1"),
        )
        self.assertEqual(tray.messages[0][0], "下载 完成")
        self.assertNotIn("\n", tray.messages[0][1])
        self.assertLessEqual(len(tray.messages[0][1]), 320)
        self.assertEqual(tray.messages[0][3], 10_000)

    def test_show_failure_clears_ghost_click_target(self) -> None:
        tray = _FakeTray()
        tray.error = RuntimeError("tray unavailable")
        self.controller.tray_icon = tray  # type: ignore[assignment]

        shown = self.controller.show(
            "下载完成",
            "message",
            QSystemTrayIcon.MessageIcon.Information,
            ("download", "task-1"),
        )

        self.assertFalse(shown)
        self.assertIsNone(self.controller.notification_target)

    def test_setup_failure_disables_notifications_without_external_state_sync(self) -> None:
        enabled: list[bool] = []
        self.controller.settings_page.desktop_notifications.setEnabled = (
            enabled.append
        )

        with patch(
            "app.ui.desktop_notification_controller.QSystemTrayIcon",
            side_effect=RuntimeError("tray unavailable"),
        ):
            self.controller.setup()

        self.assertFalse(self.controller.available)
        self.assertEqual(enabled, [False])

    def test_finished_download_notification_uses_title_and_saved_filename(self) -> None:
        task = DownloadTask(
            "task-1",
            "https://example.com/video",
            "D:/downloads",
            title="演示视频",
            status="completed",
            media_path="D:/downloads/demo.mp4",
        )
        self.download_service.tasks[task.id] = task
        calls: list[tuple] = []
        self.controller.show = lambda *args: calls.append(args) or True  # type: ignore[method-assign]

        self.controller.download_finished(task.id, "completed", "")

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "下载完成")
        self.assertIn("演示视频", calls[0][1])
        self.assertIn("demo.mp4", calls[0][1])
        self.assertEqual(calls[0][3], ("download", task.id))

    def test_foreground_window_does_not_emit_duplicate_balloon(self) -> None:
        tray = _FakeTray()
        self.controller.tray_icon = tray  # type: ignore[assignment]
        self.window.active = True

        shown = self.controller.show(
            "下载完成",
            "窗口内已经可见",
            QSystemTrayIcon.MessageIcon.Information,
            ("download", "task-2"),
        )

        self.assertFalse(shown)
        self.assertFalse(tray.messages)

    def test_notification_click_consumes_target_and_focuses_publish_task(self) -> None:
        self.controller.notification_target = ("publish", 42)

        self.controller.notification_clicked()

        self.assertEqual(self.selected_pages, [self.queue])
        self.assertEqual(self.focused, [42])
        self.assertIsNone(self.controller.notification_target)
        self.controller.notification_clicked()
        self.assertEqual(self.focused, [42])


if __name__ == "__main__":
    unittest.main()

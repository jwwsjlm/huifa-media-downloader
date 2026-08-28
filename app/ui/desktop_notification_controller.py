from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from PySide6.QtGui import QAction, QIcon
from PySide6.QtCore import QObject
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from app.core.paths import application_dir
from app.ui.i18n import application_name_text
from app.ui.i18n import format_text as ui_format
from app.ui.i18n import runtime_text, text as ui_text


def desktop_notification_should_show(
    *,
    enabled: bool,
    available: bool,
    window_active: bool,
    window_minimized: bool,
    shutting_down: bool,
) -> bool:
    """Keep background notifications useful without duplicating foreground UI."""

    return bool(
        enabled
        and available
        and not shutting_down
        and (not window_active or window_minimized)
    )


class DesktopNotificationController(QObject):
    """Own the native tray bridge, notification target and click routing."""

    def __init__(self, window: Any) -> None:
        super().__init__(window)
        self.parent = window
        self.app_settings = window.app_settings
        self.settings_page = window.settings
        self.tabs = window.tabs
        self.dashboard = window.dashboard
        self.publish_queue = window.publish_queue
        self.download_service = window.download_service
        self.available_flag = bool(window.desktop_notifications_available)
        self.shutdown_started = lambda: bool(
            getattr(getattr(window, "shutdown_controller", None), "started", False)
        )
        self.close_application = window.close
        self._available = self.available_flag
        self.tray_icon: QSystemTrayIcon | None = None
        self.tray_menu: QMenu | None = None
        self.notification_target: tuple[str, int | str] | None = None

    @property
    def available(self) -> bool:
        return self._available

    def setup(self) -> None:
        if not self.available or self.tray_icon is not None:
            return
        app = QApplication.instance()
        icon = app.windowIcon() if app is not None else QIcon()
        if icon.isNull():
            runtime_root = Path(getattr(sys, "_MEIPASS", application_dir()))
            candidates = (
                runtime_root / "assets" / "huifa.ico",
                application_dir() / "assets" / "huifa.ico",
            )
            icon_path = next((path for path in candidates if path.is_file()), None)
            if icon_path is not None:
                icon = QIcon(str(icon_path))
        try:
            tray = QSystemTrayIcon(icon, self.parent)
            tray.setToolTip(application_name_text())
            menu = QMenu(self.parent)
            show_action = QAction(ui_text('Show Main Window'), menu)
            show_action.triggered.connect(self.activate_window)
            quit_action = QAction(ui_text('Exit Application'), menu)
            quit_action.triggered.connect(self.close_application)
            menu.addAction(show_action)
            menu.addSeparator()
            menu.addAction(quit_action)
            tray.setContextMenu(menu)
            tray.activated.connect(self.tray_activated)
            tray.messageClicked.connect(self.notification_clicked)
            self.tray_icon = tray
            self.tray_menu = menu
            self.sync_visibility()
        except RuntimeError:
            self._available = False
            self.tray_icon = None
            self.tray_menu = None
            self.settings_page.desktop_notifications.setEnabled(False)

    def sync_visibility(self) -> None:
        tray = self.tray_icon
        if tray is None:
            return
        enabled = (
            self.app_settings.get_bool("desktop_notifications", True)
            and not self.shutdown_started()
        )
        tray.setVisible(enabled)
        if not enabled:
            self.notification_target = None

    def request_shutdown(self) -> None:
        self.notification_target = None
        tray = self.tray_icon
        if tray is not None:
            try:
                tray.hide()
            except RuntimeError:
                pass

    def activate_window(self) -> None:
        parent = self.parent
        if parent.isMinimized():
            parent.showNormal()
        else:
            parent.show()
        parent.raise_()
        parent.activateWindow()

    def tray_activated(self, reason) -> None:
        if reason in {
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        }:
            self.activate_window()

    def show(
        self,
        title: str,
        message: str,
        icon: QSystemTrayIcon.MessageIcon,
        target: tuple[str, int | str],
    ) -> bool:
        tray = self.tray_icon
        available = bool(self.available and tray is not None)
        parent = self.parent
        if not desktop_notification_should_show(
            enabled=self.app_settings.get_bool(
                "desktop_notifications",
                True,
            ),
            available=available,
            window_active=parent.isActiveWindow(),
            window_minimized=parent.isMinimized(),
            shutting_down=self.shutdown_started(),
        ):
            return False
        plain_title = " ".join(str(title or application_name_text()).split())[:80]
        plain_message = " ".join(
            str(message or ui_text('Task status updated')).split()
        )[:320]
        try:
            if not tray.isVisible():
                tray.show()
            self.notification_target = target
            tray.showMessage(plain_title, plain_message, icon, 10_000)
        except RuntimeError:
            self.notification_target = None
            return False
        return True

    def notification_clicked(self) -> None:
        target = self.notification_target
        # QSystemTrayIcon does not identify which balloon emitted the click.
        # Consume the target once so duplicate/late signals cannot reopen an
        # older task after the user has already handled the notification.
        self.notification_target = None
        self.activate_window()
        if target is None:
            return
        kind, identifier = target
        if kind == "download":
            task_id = str(identifier)
            dashboard = self.dashboard
            self.tabs.setCurrentWidget(dashboard)
            all_index = dashboard.filter_box.findData("全部")
            if all_index >= 0:
                dashboard.filter_box.setCurrentIndex(all_index)
            dashboard.search_box.setText(task_id)
            dashboard.apply_filter()
            item = dashboard.items.get(task_id)
            if item is not None:
                dashboard.task_list.setCurrentItem(item)
                dashboard.task_list.scrollToItem(item)
        elif kind == "publish":
            task_id = int(identifier)
            queue = self.publish_queue
            self.tabs.setCurrentWidget(queue)
            queue.focus_task(task_id)

    def download_finished(self, task_id: str, status: str, error: str) -> None:
        task = self.download_service.tasks.get(task_id)
        if task is None:
            return
        subject = task.title or task.url or ui_format('Task {id}', id=task_id)
        if status == "completed":
            detail = subject
            if task.media_path:
                detail += ui_format(
                    ' · Saved as {filename}',
                    filename=Path(task.media_path).name,
                )
            self.show(
                ui_text('Download Complete'),
                detail,
                QSystemTrayIcon.MessageIcon.Information,
                ("download", task_id),
            )
        elif status == "failed":
            detail = ui_format(
                '{subject} · {reason}',
                subject=subject,
                reason=runtime_text(error or task.error) or ui_text(
                    'Open the task log to see the reason',
                ),
            )
            self.show(
                ui_text('Download Failed'),
                detail,
                QSystemTrayIcon.MessageIcon.Critical,
                ("download", task_id),
            )

    def publish_finished(
        self,
        task_id: int,
        status: str,
        result: str,
        row: Any,
        platform_name: str,
    ) -> None:
        if status not in {"success", "failed"}:
            return
        media_title = str(row["title"] or ui_format('Publish Task {id}', id=task_id))
        if status == "success":
            self.show(
                ui_format('{platform} Publish Complete', platform=platform_name),
                media_title,
                QSystemTrayIcon.MessageIcon.Information,
                ("publish", int(task_id)),
            )
        else:
            self.show(
                ui_format('{platform} Publish Failed', platform=platform_name),
                ui_format(
                    '{title} · {reason}',
                    title=media_title,
                    reason=runtime_text(result or row["result"]) or ui_text(
                        'Open the publish queue to see the reason',
                    ),
                ),
                QSystemTrayIcon.MessageIcon.Critical,
                ("publish", int(task_id)),
            )

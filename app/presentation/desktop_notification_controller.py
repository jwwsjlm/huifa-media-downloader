from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from app.core.version import APP_NAME


class QmlDesktopNotificationController(QObject):
    """Native tray notifications that route back into the QML shell."""

    activateRequested = Signal()
    downloadRequested = Signal(str)
    publishRequested = Signal(int)

    def __init__(
        self,
        runtime: Any,
        request_exit: Callable[[], None],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._runtime = runtime
        self._request_exit = request_exit
        self._tray: QSystemTrayIcon | None = None
        self._menu: QMenu | None = None
        self._target: tuple[str, int | str] | None = None
        self._available = False

    @Slot()
    def setup(self) -> None:
        if self._tray is not None:
            self.syncVisibility()
            return
        try:
            self._available = bool(
                QSystemTrayIcon.isSystemTrayAvailable()
                and QSystemTrayIcon.supportsMessages()
            )
        except RuntimeError:
            self._available = False
        if not self._available:
            return
        application = QGuiApplication.instance()
        try:
            icon = application.windowIcon() if application is not None else QIcon()
            tray = QSystemTrayIcon(icon, self)
            tray.setToolTip(APP_NAME)
            menu = QMenu()
            show_action = menu.addAction("显示主窗口")
            show_action.triggered.connect(self.activateRequested.emit)
            menu.addSeparator()
            exit_action = menu.addAction("退出")
            exit_action.triggered.connect(self._request_exit)
            tray.setContextMenu(menu)
            tray.activated.connect(self._tray_activated)
            tray.messageClicked.connect(self._notification_clicked)
        except RuntimeError:
            self._available = False
            return
        self._tray = tray
        self._menu = menu
        self.syncVisibility()

    @Slot()
    def syncVisibility(self) -> None:
        tray = self._tray
        if tray is None:
            return
        enabled = self._runtime.app_settings.get_bool("desktop_notifications", True)
        try:
            tray.setVisible(bool(enabled))
        except RuntimeError:
            return
        if not enabled:
            self._target = None

    @Slot()
    def requestShutdown(self) -> None:
        self._target = None
        tray = self._tray
        if tray is not None:
            try:
                tray.hide()
            except RuntimeError:
                pass

    @Slot(str, str, str)
    def downloadFinished(self, task_id: str, status: str, error: str) -> None:
        if status not in {"completed", "failed"}:
            return
        task = self._runtime.download_service.tasks.get(str(task_id or ""))
        if task is None:
            return
        subject = str(task.title or task.url or task_id)
        if status == "completed":
            self._show(
                "下载完成",
                subject,
                QSystemTrayIcon.MessageIcon.Information,
                ("download", str(task_id)),
            )
            return
        self._show(
            "下载失败",
            f"{subject} · {error or task.error or '请查看任务详情'}",
            QSystemTrayIcon.MessageIcon.Critical,
            ("download", str(task_id)),
        )

    @Slot(int, str, str)
    def publishFinished(self, task_id: int, status: str, result: str) -> None:
        if status not in {"success", "failed"}:
            return
        row = self._runtime.db.get_publish_task(int(task_id or 0))
        if row is None:
            return
        subject = str(row["title"] or f"发布任务 {task_id}")
        platform = str(row["platform"] or "发布")
        if status == "success":
            self._show(
                f"{platform} 发布完成",
                subject,
                QSystemTrayIcon.MessageIcon.Information,
                ("publish", int(task_id)),
            )
            return
        self._show(
            f"{platform} 发布失败",
            f"{subject} · {result or row['result'] or '请查看发布队列'}",
            QSystemTrayIcon.MessageIcon.Critical,
            ("publish", int(task_id)),
        )

    def _show(
        self,
        title: str,
        message: str,
        icon: QSystemTrayIcon.MessageIcon,
        target: tuple[str, int | str],
    ) -> None:
        tray = self._tray
        if (
            tray is None
            or not self._available
            or not self._runtime.app_settings.get_bool("desktop_notifications", True)
            or QGuiApplication.applicationState() == Qt.ApplicationState.ApplicationActive
        ):
            return
        try:
            if not tray.isVisible():
                tray.show()
            self._target = target
            tray.showMessage(
                " ".join(str(title or APP_NAME).split())[:80],
                " ".join(str(message or "").split())[:320],
                icon,
                10_000,
            )
        except RuntimeError:
            self._target = None

    @Slot(QSystemTrayIcon.ActivationReason)
    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in {
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        }:
            self.activateRequested.emit()

    @Slot()
    def _notification_clicked(self) -> None:
        target = self._target
        self._target = None
        self.activateRequested.emit()
        if target is None:
            return
        kind, identifier = target
        if kind == "download":
            self.downloadRequested.emit(str(identifier))
        elif kind == "publish":
            self.publishRequested.emit(int(identifier))

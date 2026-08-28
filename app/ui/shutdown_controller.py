from __future__ import annotations

import os
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QTimer

from app.ui.i18n import format_text as ui_format
from app.ui.i18n import text as ui_text


class ShutdownController(QObject):
    """Coordinate non-blocking application shutdown across owned services."""

    POLL_INTERVAL_MS = 100
    WAIT_STATUS_DELAY_SECONDS = 10
    FORCE_EXIT_AFTER_SECONDS = 20

    def __init__(
        self,
        window: Any,
        *,
        clock: Callable[[], float] = time.monotonic,
        force_exit: Callable[[int], object] = os._exit,
    ) -> None:
        super().__init__(window)
        self.window = window
        self.parent = window
        self.tabs = window.tabs
        self.database_lifecycle = window.database_lifecycle_controller
        self.update_service = window.update_service
        self.runtime_update_dialog = window.runtime_update_dialog_controller
        self.application_update_controller = window.application_update_controller
        self.desktop_notifications = window.desktop_notification_controller
        self.application_update_service = window.application_update_service
        self.dashboard = window.dashboard
        self.download_service = window.download_service
        self.publish_service = window.publish_service
        self.settings_page = window.settings
        self.task_status_summary = window.task_status_summary
        self.cover_service = window.cover_service
        self.show_status = window.statusBar().showMessage
        self.close_window = window.close
        self.clock = clock
        self.force_exit = force_exit
        self.started = False
        self.complete = False
        self._force_exit_requested = False
        self._database_available = False
        self._started_at = 0.0
        self._last_wait_seconds = -1
        self._error_keys: set[str] = set()
        self._errors: list[str] = []

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(self.POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self.poll)
        self._initial_poll = QTimer(self)
        self._initial_poll.setSingleShot(True)
        self._initial_poll.timeout.connect(self.poll)
        self._close_timer = QTimer(self)
        self._close_timer.setSingleShot(True)
        self._close_timer.timeout.connect(self._close_if_complete)

    def database(self) -> Any:
        return self.window.db

    @property
    def polling(self) -> bool:
        return self._poll_timer.isActive()

    def begin(self) -> None:
        if self.started or self.complete:
            return
        self.started = True
        self._started_at = self.clock()
        self._last_wait_seconds = -1

        self._run_once(
            "database-watch-stop",
            "停止数据库文件监控失败",
            self.database_lifecycle.stop,
        )
        self._run_once(
            "disable-tabs",
            "禁用主界面失败",
            lambda: self.tabs.setEnabled(False),
        )
        self._show_status(
            "shutdown-start-status",
            ui_text("Safely stopping background tasks…"),
        )
        self._prepare_database_state()

        for key, label, request in self._shutdown_requests():
            self._run_once(key, label, request)

        self._poll_timer.start()
        self._initial_poll.start(0)

    def _shutdown_requests(self) -> tuple[tuple[str, str, Callable[[], None]], ...]:
        return (
            (
                "task-status-summary-stop",
                "停止任务统计刷新失败",
                self.task_status_summary.stop,
            ),
            (
                "runtime-update-dialog-request",
                "停止运行时更新弹窗失败",
                self.runtime_update_dialog.request_shutdown,
            ),
            (
                "application-update-controller-request",
                "停止程序更新控制器失败",
                self.application_update_controller.request_shutdown,
            ),
            (
                "desktop-notifications-request",
                "停止桌面通知失败",
                self.desktop_notifications.request_shutdown,
            ),
            (
                "update-service-request",
                "停止第三方组件更新服务失败",
                self.update_service.request_shutdown,
            ),
            (
                "application-update-service-request",
                "停止程序更新服务失败",
                self.application_update_service.request_shutdown,
            ),
            (
                "dashboard-request",
                "停止聚合解析任务失败",
                self.dashboard.request_shutdown,
            ),
            (
                "download-service-request",
                "停止下载服务失败",
                self.download_service.request_shutdown,
            ),
            (
                "publish-service-request",
                "停止发布服务失败",
                self.publish_service.request_shutdown,
            ),
            (
                "settings-request",
                "停止本地组件检测失败",
                self.settings_page.request_shutdown,
            ),
        )

    def _prepare_database_state(self) -> None:
        try:
            database_available = bool(
                self.database_lifecycle.persistence_available
            )
        except Exception:
            self._record_current_exception(
                "database-availability",
                "读取数据库可用状态失败",
            )
            database_available = False

        tasks = getattr(self.download_service, "tasks", {})
        if database_available and tasks:
            try:
                live_ids = self.dashboard._database_task_ids()
            except Exception:
                self._record_current_exception(
                    "database-task-ids",
                    "退出时读取数据库任务列表失败",
                )
                live_ids = None

            # ``None`` explicitly means the read failed. Only a successful,
            # genuinely empty result proves that in-memory rows are stale.
            if live_ids == set():
                database_available = False
                self._run_once(
                    "clear-stale-download-cache",
                    "清理已删除数据库的任务缓存失败",
                    self.download_service.reset_task_cache,
                )
                self._run_once(
                    "clear-stale-dashboard",
                    "清理已删除数据库的任务界面失败",
                    self.dashboard.clear_tasks,
                )
        self._database_available = database_available

    def poll(self) -> None:
        if not self.started or self.complete:
            return

        states = (
            self._poll_service("update-service", self.update_service),
            self._poll_service(
                "application-update-service",
                self.application_update_service,
                busy_attribute="busy",
            ),
            self._poll_service("download-service", self.download_service),
            self._poll_service("publish-service", self.publish_service),
            self._idle_property(
                "collection-probe",
                lambda: self.dashboard.collection_probe_running,
            ),
            self._idle_property(
                "local-core-version-check",
                lambda: self.settings_page.local_core_version_check_running,
            ),
        )
        if all(states):
            self.finish()
            return

        elapsed = max(0, int(self.clock() - self._started_at))
        if elapsed >= self.FORCE_EXIT_AFTER_SECONDS:
            self._force_exit_after_timeout(elapsed)
            return
        if (
            elapsed >= self.WAIT_STATUS_DELAY_SECONDS
            and elapsed != self._last_wait_seconds
        ):
            self._last_wait_seconds = elapsed
            self._show_status(
                "shutdown-wait-status",
                ui_format(
                    "Waiting for {active} background tasks to stop safely ({seconds}s elapsed)…",
                    active=self._active_background_count(),
                    seconds=elapsed,
                ),
            )

    def _poll_service(
        self,
        key: str,
        service: Any,
        *,
        busy_attribute: str = "active_thread_count",
    ) -> bool:
        try:
            return bool(service.shutdown(timeout_ms=0))
        except Exception:
            self._record_current_exception(
                f"{key}-poll",
                f"轮询 {key} 退出状态失败",
            )
            # The stop request has already been sent. If the service cannot
            # report its state, use its lightweight ownership indicator and
            # avoid trapping the application forever on another exception.
            try:
                return not bool(getattr(service, busy_attribute))
            except Exception:
                self._record_current_exception(
                    f"{key}-busy",
                    f"读取 {key} 后台状态失败",
                )
                return True

    def _idle_property(self, key: str, running: Callable[[], object]) -> bool:
        try:
            return not bool(running())
        except Exception:
            self._record_current_exception(
                f"{key}-state",
                f"读取 {key} 后台状态失败",
            )
            return True

    def _active_background_count(self) -> int:
        values = (
            self._activity_value("update-service", self.update_service),
            self._activity_value(
                "application-update-service",
                self.application_update_service,
                attribute="busy",
            ),
            self._activity_value("download-service", self.download_service),
            self._activity_value("publish-service", self.publish_service),
            self._running_value(
                "local-core-version-check",
                lambda: self.settings_page.local_core_version_check_running,
            ),
            self._running_value(
                "collection-probe",
                lambda: self.dashboard.collection_probe_running,
            ),
        )
        return sum(values)

    def _force_exit_after_timeout(self, elapsed: int) -> None:
        """Stop waiting for an uninterruptible third-party call."""

        if self._force_exit_requested:
            return
        self._force_exit_requested = True
        self._poll_timer.stop()
        self._initial_poll.stop()
        self._show_status(
            "shutdown-force-exit-status",
            ui_format(
                "Background tasks did not stop within {seconds} seconds; forcing exit…",
                seconds=elapsed,
            ),
        )
        self._errors.append(
            f"后台任务超过 {elapsed} 秒仍未结束，程序已执行强制退出。"
        )
        self._flush_error_log()
        self.force_exit(0)

    def _activity_value(
        self,
        key: str,
        service: Any,
        *,
        attribute: str = "active_thread_count",
    ) -> int:
        try:
            value = getattr(service, attribute)
            if isinstance(value, bool):
                return int(value)
            return max(0, int(value or 0))
        except Exception:
            self._record_current_exception(
                f"{key}-activity",
                f"读取 {key} 活动任务数失败",
            )
            return 0

    def _running_value(self, key: str, running: Callable[[], object]) -> int:
        try:
            return int(bool(running()))
        except Exception:
            self._record_current_exception(
                f"{key}-activity",
                f"读取 {key} 活动状态失败",
            )
            return 0

    def finish(self) -> None:
        if self.complete:
            return
        self._poll_timer.stop()
        self._initial_poll.stop()

        if self._database_available:
            self._persist_interrupted_tasks()

        self._run_once(
            "cover-service-close",
            "关闭封面服务失败",
            self.cover_service.close,
        )
        self._run_once(
            "database-close",
            "关闭数据库失败",
            lambda: self.database().close(),
        )
        self.complete = True
        self._show_status(
            "shutdown-complete-status",
            ui_text("Background tasks stopped; exiting…"),
        )
        self._flush_error_log()
        self._close_timer.start(0)

    def _persist_interrupted_tasks(self) -> None:
        tasks = []
        for task in self.download_service.tasks.values():
            if task.status in {"downloading", "暂停中"}:
                task.status = "paused"
                tasks.append(task)
        if not tasks:
            return
        try:
            self.download_service.db.upsert_download_tasks(tasks)
        except Exception:
            self._record_current_exception(
                "persist-interrupted-tasks",
                "关闭时保存任务状态失败",
            )

    def handle_close_event(self, event: Any) -> None:
        if self.complete:
            event.accept()
            return
        event.ignore()
        self.begin()

    def _close_if_complete(self) -> None:
        if self.complete:
            try:
                self.close_window()
            except Exception:
                self._record_current_exception(
                    "close-window",
                    "关闭主窗口失败",
                )
                self._flush_error_log()

    def _run_once(
        self,
        key: str,
        label: str,
        action: Callable[[], object],
    ) -> bool:
        try:
            action()
            return True
        except Exception:
            self._record_current_exception(key, label)
            return False

    def _show_status(self, key: str, message: str) -> None:
        self._run_once(
            key,
            "更新退出状态提示失败",
            lambda: self.show_status(message, 0),
        )

    def _record_current_exception(self, key: str, label: str) -> None:
        if key in self._error_keys:
            return
        self._error_keys.add(key)
        self._errors.append(f"{label}:\n{traceback.format_exc()}")

    def _flush_error_log(self) -> None:
        if not self._errors:
            return
        try:
            database_path = Path(self.database().path)
            log_path = database_path.parent / "logs" / "app-crash.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as stream:
                stream.write("\n".join(self._errors) + "\n")
        except Exception:
            pass

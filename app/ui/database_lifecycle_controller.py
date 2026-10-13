from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QMessageBox

from app.storage.database import Database
from app.presentation.database_recovery import database_recovery_notice
from app.ui.i18n import format_text as ui_format
from app.ui.i18n import runtime_text, text as ui_text


class DatabaseLifecycleController(QObject):
    """Monitor the portable task database and coordinate a safe live reset."""

    WATCH_INTERVAL_MS = 1000

    def __init__(self, window: Any) -> None:
        super().__init__(window)
        self.window = window
        self.parent = window
        self.download_service = window.download_service
        self.publish_service = window.publish_service
        self.dashboard = window.dashboard
        self.completed_page = window.completed
        self.publish_queue = window.publish_queue
        self.shutdown_started = lambda: bool(
            getattr(getattr(window, "shutdown_controller", None), "started", False)
        )
        self.show_status = window.statusBar().showMessage
        self._view_reset_pending = False
        self._replacement_pending: Database | None = None
        self._persistence_safe = True
        self._watch = QTimer(self)
        self._watch.setInterval(self.WATCH_INTERVAL_MS)
        self._watch.timeout.connect(self.check_file)
        self._notice = QTimer(self)
        self._notice.setSingleShot(True)
        self._notice.timeout.connect(self.show_recovery_notice)

    def database(self) -> Any:
        return self.window.db

    def set_database(self, database: Any) -> None:
        self.window.runtime.replace_database(database)
        self.window.db = self.window.runtime.db

    @property
    def watching(self) -> bool:
        return self._watch.isActive()

    @property
    def persistence_available(self) -> bool:
        database = self.database()
        return self._persistence_safe and Path(database.path).exists()

    def start(self) -> None:
        self._view_reset_pending = False
        self._persistence_safe = True
        self._watch.start()
        if self.database().recovery_report.requires_notice:
            self._notice.start(0)

    def stop(self) -> None:
        self._watch.stop()
        self._notice.stop()
        replacement = self._replacement_pending
        self._replacement_pending = None
        if replacement is not None:
            try:
                replacement.close()
            except Exception:
                pass

    def _background_database_users_active(self) -> bool:
        return bool(
            self.download_service.active_thread_count
            or self.publish_service.active_thread_count
            or getattr(getattr(self.window, "collection_workflow", None), "running", False)
        )

    def show_recovery_notice(self) -> None:
        if self.shutdown_started():
            return
        presentation = database_recovery_notice(self.database())
        if presentation is None:
            return
        title, summary, detail = presentation
        self.show_status(summary, 15000)
        QMessageBox.warning(self.parent, title, detail)

    def check_file(self) -> None:
        if self._view_reset_pending:
            if not self._background_database_users_active():
                self._refresh_after_reset()
            return

        current = self.database()
        database_path = Path(current.path)
        if self._replacement_pending is not None:
            if not self._background_database_users_active():
                self._continue_database_replacement(current)
            return
        if database_path.exists():
            return

        # From this point onward shutdown must not write stale in-memory tasks
        # into a replacement database unless cache/view clearing succeeds.
        self._persistence_safe = False
        if self._background_database_users_active():
            return
        self._replace_missing_database(current, database_path)

    def _replace_missing_database(self, current: Any, database_path: Path) -> None:
        try:
            replacement = Database(database_path)
        except Exception as exc:
            self._show_reset_error(exc)
            return
        self._replacement_pending = replacement
        self._continue_database_replacement(current)

    def _continue_database_replacement(self, current: Any) -> None:
        replacement = self._replacement_pending
        if replacement is None:
            return
        try:
            # The missing database makes every cached task stale. Clear it
            # before redirecting the service to the replacement so a queued
            # lifecycle callback cannot recreate old rows in the new file.
            self.download_service.reset_task_cache()
            self.publish_service.reset_database_state()
        except Exception as exc:
            self._show_reset_error(exc)
            return
        self.set_database(replacement)
        self._replacement_pending = None
        self._view_reset_pending = True
        try:
            current.close()
        except Exception as exc:
            # The replacement is already authoritative. Report the close
            # failure, but continue clearing stale in-memory task state.
            self._show_reset_error(exc)
        self._refresh_after_reset(task_cache_cleared=True)

    def _refresh_after_reset(self, *, task_cache_cleared: bool = False) -> None:
        errors: list[Exception] = []
        dashboard_cleared = False
        if not task_cache_cleared:
            try:
                self.download_service.reset_task_cache()
                task_cache_cleared = True
            except Exception as exc:
                errors.append(exc)
        try:
            self.dashboard.collection_workflow.reset_after_database_change()
        except Exception as exc:
            errors.append(exc)
        try:
            self.dashboard.clear_tasks()
            dashboard_cleared = True
        except Exception as exc:
            errors.append(exc)

        core_state_safe = task_cache_cleared and dashboard_cleared
        self._persistence_safe = core_state_safe
        self._view_reset_pending = not core_state_safe

        for refresh in (
            self.completed_page.refresh,
            self.publish_queue.refresh,
        ):
            try:
                refresh()
            except Exception as exc:
                errors.append(exc)

        if core_state_safe:
            try:
                self.dashboard.status.setText(ui_text("Task database cleared"))
            except Exception as exc:
                errors.append(exc)
        if errors:
            self.show_status(
                ui_format(
                    "Task database was reset, but some views could not refresh: {error}",
                    error=runtime_text(errors[0]),
                ),
                5000,
            )

    def _show_reset_error(self, error: Exception) -> None:
        self.show_status(
            ui_format(
                "Failed to reinitialize the task database: {error}",
                error=runtime_text(error),
            ),
            5000,
        )

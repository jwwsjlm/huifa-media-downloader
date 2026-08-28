from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QMessageBox

from app.storage.database import Database
from app.ui.i18n import format_text as ui_format
from app.ui.i18n import runtime_text, text as ui_text


def database_recovery_notice(database: Any) -> tuple[str, str, str] | None:
    report = database.recovery_report
    if not report.requires_notice:
        return None

    if report.status == "restored":
        title = ui_text("Database Restored Automatically")
        summary = ui_text(
            "The local database was damaged and has been restored from the most recent healthy backup.",
        )
        consequence = ui_text(
            "A small number of task states created after the last backup may need to be checked again.",
        )
        location = report.quarantine_dir or str(database.path.parent / "recovery")
        detail = ui_format(
            "{summary}\n\n{consequence}\n\nThe original files were quarantined at:\n{location}",
            summary=summary,
            consequence=consequence,
            location=location,
        )
    elif report.status == "schema_reset":
        title = ui_text("Database Schema Updated")
        summary = ui_text(
            "The database schema was not the current development version and has been recreated with the latest schema.",
        )
        consequence = ui_text(
            "Old development data was removed; no legacy migration was performed.",
        )
        detail = ui_format(
            "{summary}\n\n{consequence}",
            summary=summary,
            consequence=consequence,
        )
    else:
        title = ui_text("Database Rebuilt Safely")
        summary = ui_text(
            "The local database was damaged and no usable backup was found, so a new empty database was created.",
        )
        consequence = ui_text(
            "The original database was not deleted and can be provided to support for further analysis.",
        )
        location = report.quarantine_dir or str(database.path.parent / "recovery")
        detail = ui_format(
            "{summary}\n\n{consequence}\n\nThe original files were quarantined at:\n{location}",
            summary=summary,
            consequence=consequence,
            location=location,
        )
    return title, summary, detail


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
        self.window.db = database

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

    def _background_database_users_active(self) -> bool:
        return bool(
            self.download_service.active_thread_count
            or self.publish_service.active_thread_count
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

        self.set_database(replacement)
        self.download_service.db = replacement
        self.publish_service.db = replacement
        self._view_reset_pending = True
        try:
            current.close()
        except Exception as exc:
            # The replacement is already authoritative. Report the close
            # failure, but continue clearing stale in-memory task state.
            self._show_reset_error(exc)
        self._refresh_after_reset()

    def _refresh_after_reset(self) -> None:
        errors: list[Exception] = []
        task_cache_cleared = False
        dashboard_cleared = False
        try:
            self.download_service.reset_task_cache()
            task_cache_cleared = True
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

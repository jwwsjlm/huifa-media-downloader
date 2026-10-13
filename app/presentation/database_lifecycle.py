from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from app.presentation.database_recovery import database_recovery_notice
from app.storage.database import Database


class QmlDatabaseLifecycle(QObject):
    """Keep a portable SQLite database recoverable without a Widgets window."""

    message = Signal(str)
    recoveryNotice = Signal(str, str, str)

    def __init__(
        self,
        runtime: Any,
        refresh_views: Callable[[Any], None],
        extra_background_users_active: Callable[[], bool] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._runtime = runtime
        self._refresh_views = refresh_views
        self._extra_background_users_active = extra_background_users_active
        self._persistence_safe = True
        self._view_refresh_pending = False
        self._replacement_pending: Database | None = None
        self._shutting_down = False
        self._watch = QTimer(self)
        self._watch.setInterval(1_000)
        self._watch.timeout.connect(self.checkFile)

    @property
    def persistenceAvailable(self) -> bool:
        try:
            return self._persistence_safe and Path(self._runtime.db.path).exists()
        except Exception:
            return False

    @Slot()
    def start(self) -> None:
        if self._shutting_down:
            return
        self._persistence_safe = True
        self._view_refresh_pending = False
        self._watch.start()
        notice = database_recovery_notice(self._runtime.db)
        if notice is not None:
            title, summary, detail = notice
            self.message.emit(f"{title}：{summary}")
            self.recoveryNotice.emit(title, summary, detail)

    @Slot()
    def requestShutdown(self) -> None:
        self._shutting_down = True
        self._watch.stop()
        replacement = self._replacement_pending
        self._replacement_pending = None
        if replacement is not None:
            try:
                replacement.close()
            except Exception:
                pass

    @Slot()
    def checkFile(self) -> None:
        if self._shutting_down:
            return
        current = self._runtime.db
        try:
            path = Path(current.path)
        except Exception:
            self._persistence_safe = False
            return
        if self._replacement_pending is not None:
            if not self._background_users_active():
                self._continue_database_replacement(current)
            return
        if path.exists():
            if self._view_refresh_pending:
                self._finish_database_replacement(current)
            return
        self._persistence_safe = False
        if self._background_users_active():
            return
        try:
            replacement = Database(path)
        except Exception as exc:
            self.message.emit(f"无法重新初始化任务数据库：{exc}")
            return
        self._replacement_pending = replacement
        self._continue_database_replacement(current)

    def _continue_database_replacement(self, current: Database) -> None:
        replacement = self._replacement_pending
        if replacement is None:
            return
        try:
            # The missing database makes every cached task stale. Clear it
            # before redirecting the service to the replacement so a queued
            # lifecycle callback cannot recreate old rows in the new file.
            self._runtime.download_service.reset_task_cache()
            self._runtime.publish_service.reset_database_state()
        except Exception as exc:
            self.message.emit(f"任务数据库已重建，但旧内存状态清理失败：{exc}")
            return
        self._runtime.replace_database(replacement)
        self._replacement_pending = None
        try:
            current.close()
        except Exception:
            pass
        self._view_refresh_pending = True
        self._finish_database_replacement(replacement)

    def _finish_database_replacement(self, database: Database) -> None:
        try:
            self._refresh_views(database)
        except Exception as exc:
            self.message.emit(f"任务数据库已重建，但界面刷新失败：{exc}")
            return
        self._view_refresh_pending = False
        self._persistence_safe = True
        self.message.emit("任务数据库已重建，旧的内存任务记录已清除")
        self.recoveryNotice.emit(
            "任务数据库已重建",
            "任务数据库文件已丢失，已创建新的空数据库。",
            "旧内存任务记录已清除。已完成的本地媒体可从下载目录扫描导入。",
        )

    def _background_users_active(self) -> bool:
        if (
            self._runtime.download_service.active_thread_count
            or self._runtime.publish_service.active_thread_count
        ):
            return True
        try:
            return bool(
                self._extra_background_users_active
                and self._extra_background_users_active()
            )
        except Exception:
            return True

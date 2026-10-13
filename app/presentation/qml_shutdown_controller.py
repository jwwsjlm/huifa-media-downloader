from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QCoreApplication, QObject, QTimer, Signal, Slot


class QmlShutdownController(QObject):
    """Stop QML-owned services before allowing the main window to close."""

    POLL_INTERVAL_MS = 100
    FORCE_EXIT_AFTER_SECONDS = 20

    stateChanged = Signal()
    finished = Signal()

    def __init__(
        self,
        runtime: Any,
        collection_workflow: Any,
        show_status: Callable[[str], None],
        persistence_available: Callable[[], bool] | None = None,
        cover_studio: Any | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._runtime = runtime
        self._collection_workflow = collection_workflow
        self._show_status = show_status
        self._persistence_available = persistence_available or (lambda: True)
        self._cover_studio = cover_studio
        self._started = False
        self._complete = False
        self._started_at = 0.0
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(self.POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._poll)

    @property
    def started(self) -> bool:
        return self._started

    @property
    def complete(self) -> bool:
        return self._complete

    @Slot()
    def begin(self) -> None:
        if self._started or self._complete:
            return
        self._started = True
        self._started_at = time.monotonic()
        self.stateChanged.emit()
        self._show_status("正在安全停止后台任务…")
        self._collection_workflow.request_shutdown()
        if self._cover_studio is not None:
            self._cover_studio.requestShutdown()
        self._runtime.update_service.request_shutdown()
        self._runtime.application_update_service.request_shutdown()
        self._runtime.download_service.request_shutdown()
        self._runtime.publish_service.request_shutdown()
        self._poll_timer.start()
        QTimer.singleShot(0, self._poll)

    def finish_blocking(self) -> bool:
        """Complete shutdown after the QApplication event loop has ended."""
        self.begin()
        while not self._complete:
            QCoreApplication.processEvents()
            self._poll()
            if not self._complete:
                time.sleep(self.POLL_INTERVAL_MS / 1000)
        return True

    @Slot()
    def _poll(self) -> None:
        if not self._started or self._complete:
            return
        if self._services_stopped():
            self._finish()
            return
        if time.monotonic() - self._started_at >= self.FORCE_EXIT_AFTER_SECONDS:
            self._show_status("后台任务未能及时停止，正在强制退出…")
            os._exit(0)

    def _services_stopped(self) -> bool:
        services = (
            self._runtime.update_service,
            self._runtime.application_update_service,
            self._runtime.download_service,
            self._runtime.publish_service,
        )
        for service in services:
            try:
                if not service.shutdown(timeout_ms=0):
                    return False
            except Exception:
                return False
        return not self._collection_workflow.running and not bool(
            self._cover_studio is not None and self._cover_studio.busy
        )

    def _finish(self) -> None:
        if self._complete:
            return
        self._poll_timer.stop()
        try:
            can_persist_tasks = bool(self._persistence_available())
        except Exception:
            can_persist_tasks = False
        if can_persist_tasks:
            self._persist_interrupted_tasks()
        try:
            self._runtime.cover_service.close()
        except Exception:
            pass
        try:
            self._runtime.db.close()
        except Exception:
            pass
        self._complete = True
        self.stateChanged.emit()
        self._show_status("后台任务已停止，正在退出…")
        self.finished.emit()

    def _persist_interrupted_tasks(self) -> None:
        try:
            self._runtime.download_service.persist_interrupted_tasks()
        except Exception:
            log_path = Path(self._runtime.db.path).parent / "logs" / "app-crash.log"
            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with log_path.open("a", encoding="utf-8") as stream:
                    stream.write("退出时保存中断下载任务失败。\n")
            except Exception:
                pass

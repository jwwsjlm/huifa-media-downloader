from __future__ import annotations

from functools import partial
from typing import Any

from PySide6.QtCore import QObject, QThread, Qt, QTimer, Slot

from app.core.local_core_versions import LocalCoreVersionWorker
from app.core.qt_lifecycle import delete_unstarted_worker


class LocalCoreVersionController(QObject):
    """Own the local runtime detector and its asynchronous Qt lifecycle."""

    def __init__(
        self,
        page: Any,
    ) -> None:
        super().__init__(page)
        self.page = page
        self._runtime: tuple[Any, Any] | None = None
        self._deferred_finishes: set[Any] = set()
        self.pending = False
        self.loaded = False
        self.shutdown_requested = False

    def refresh(self, *, force: bool = False) -> None:
        if self.shutdown_requested:
            return
        if self.loaded and not force:
            self.page._render_runtime_component_statuses()
            return
        if self._runtime is not None:
            if force:
                self.pending = True
            return

        self.pending = False
        self.page._show_local_core_loading()
        thread = None
        worker = None
        try:
            thread = QThread(self.page)
            worker = LocalCoreVersionWorker(
                self.page.deno.text(),
                self.page.ffmpeg.text(),
                self.page.ffprobe.text(),
            )
            worker.moveToThread(thread)
            thread.started.connect(worker.run)
            worker.completed.connect(self.versions_ready, Qt.QueuedConnection)
            worker.completed.connect(thread.quit)
            worker.completed.connect(worker.deleteLater)
            thread.finished.connect(self._thread_finished, Qt.QueuedConnection)
        except Exception as exc:
            if worker is not None and thread is not None:
                delete_unstarted_worker(worker, thread)
            elif thread is not None:
                thread.deleteLater()
            self.loaded = False
            self.page._show_local_core_start_failure(exc)
            return
        self._runtime = (thread, worker)
        try:
            thread.start()
        except Exception as exc:
            self._runtime = None
            self._deferred_finishes.discard(thread)
            delete_unstarted_worker(worker, thread)
            self.loaded = False
            self.page._show_local_core_start_failure(exc)

    @Slot(object)
    def versions_ready(self, results: object) -> None:
        if self.shutdown_requested:
            return
        self.page._apply_local_core_versions(results)
        self.loaded = True

    @Slot()
    def _thread_finished(self) -> None:
        thread = self.sender()
        if thread is not None:
            self.defer_finish(thread)

    def defer_finish(self, thread: Any) -> None:
        """Keep the detector owned until its queued result updates the page."""

        if thread in self._deferred_finishes:
            return
        self._deferred_finishes.add(thread)
        QTimer.singleShot(0, self, partial(self.complete_finish, thread))

    def complete_finish(self, thread: Any) -> None:
        self._deferred_finishes.discard(thread)
        runtime = self._runtime
        if runtime is None or runtime[0] is not thread:
            try:
                thread.deleteLater()
            except RuntimeError:
                pass
            return
        self._runtime = None
        try:
            thread.deleteLater()
        except RuntimeError:
            pass
        if self.shutdown_requested:
            return
        if self.pending:
            self.pending = False
            QTimer.singleShot(0, self, lambda: self.refresh(force=True))

    def request_shutdown(self) -> None:
        self.shutdown_requested = True
        self.pending = False
        runtime = self._runtime
        if runtime is None:
            return
        thread, worker = runtime
        try:
            worker.cancel()
        except (AttributeError, RuntimeError):
            pass
        try:
            if thread.isRunning():
                thread.requestInterruption()
                thread.quit()
        except RuntimeError:
            pass

    @property
    def running(self) -> bool:
        return self._runtime is not None


__all__ = ["LocalCoreVersionController"]

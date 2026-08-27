from __future__ import annotations

from collections import deque
from collections.abc import Callable
from typing import TypedDict

from PySide6.QtCore import QObject, QThread, QTimer, Qt

from app.core.collection_service import CollectionProbeRequest, CollectionProbeWorker
from app.core.qt_lifecycle import delete_unstarted_worker


class ProbeState(TypedDict, total=False):
    request: CollectionProbeRequest
    context: dict[str, object]
    url: str
    entry_count: int
    metadata: dict[str, object]
    parent_id: str
    thread: QThread | None
    worker: object | None
    confirmed: bool
    parent_collection_task_id: str
    collection_index: int
    visited_source_keys: set[str]
    source_key_registered: bool
    finished: bool


class CollectionProbeCoordinator(QObject):
    """Own collection-probe queueing and QThread lifecycle outside the page."""

    def __init__(
        self,
        *,
        on_metadata: Callable[[str, object], None],
        on_entries: Callable[[str, object], None],
        on_single: Callable[[str, object], None],
        on_failed: Callable[[str, str], None],
        on_finished: Callable[[str, bool, int], None],
        on_start_error: Callable[[str, Exception], None],
        on_slot_released: Callable[[], None],
        parent: QObject | None = None,
        max_concurrent: int = 2,
    ) -> None:
        super().__init__(parent)
        self.states: dict[str, ProbeState] = {}
        self.queue: deque[str] = deque()
        self.deferred_finishes: set[str] = set()
        self.max_concurrent = max(1, int(max_concurrent))
        self._shutdown_requested = False
        self._on_metadata = on_metadata
        self._on_entries = on_entries
        self._on_single = on_single
        self._on_failed = on_failed
        self._on_finished = on_finished
        self._on_start_error = on_start_error
        self._on_slot_released = on_slot_released

    @property
    def running(self) -> bool:
        return any(
            request_id in self.deferred_finishes
            or self.thread_is_running(state.get("thread"))
            for request_id, state in self.states.items()
        )

    @property
    def shutdown_requested(self) -> bool:
        return self._shutdown_requested

    def enqueue(self, request_id: str, state: ProbeState) -> bool:
        if self._shutdown_requested or request_id in self.states:
            return False
        self.states[request_id] = state
        self.queue.append(request_id)
        return True

    def result_state(self, request_id: str) -> ProbeState | None:
        """Return state only while worker results are still authoritative."""

        if self._shutdown_requested:
            return None
        state = self.states.get(request_id)
        if state is None or state.get("confirmed"):
            return None
        return state

    def request_shutdown(self) -> None:
        self._shutdown_requested = True
        self.queue.clear()
        for request_id, state in tuple(self.states.items()):
            state["confirmed"] = True
            worker = state.get("worker")
            if isinstance(worker, CollectionProbeWorker):
                try:
                    worker.cancel()
                except RuntimeError:
                    pass
            if not self.thread_is_running(state.get("thread")):
                self.states.pop(request_id, None)

    def cancel(self, request_id: str) -> bool:
        """Cancel one queued/running probe without allowing it to start later."""

        state = self.states.get(request_id)
        if state is None:
            return False
        try:
            self.queue.remove(request_id)
        except ValueError:
            pass
        state["confirmed"] = True
        worker = state.get("worker")
        if isinstance(worker, CollectionProbeWorker):
            try:
                worker.cancel()
            except RuntimeError:
                pass
        thread = state.get("thread")
        if not self.thread_is_running(thread):
            self.states.pop(request_id, None)
        return True

    @staticmethod
    def thread_is_running(thread: object) -> bool:
        if not isinstance(thread, QThread):
            return False
        try:
            return thread.isRunning()
        except RuntimeError:
            return False

    def start_pending(self) -> None:
        if self._shutdown_requested:
            return
        running = sum(
            request_id in self.deferred_finishes
            or self.thread_is_running(state.get("thread"))
            for request_id, state in self.states.items()
        )
        while self.queue and running < self.max_concurrent:
            request_id = self.queue.popleft()
            state = self.states.get(request_id)
            if state is None:
                continue
            if self._start_one(request_id, state):
                running += 1

    def _start_one(self, request_id: str, state: ProbeState) -> bool:
        thread: QThread | None = None
        worker: CollectionProbeWorker | None = None
        try:
            thread = QThread(self)
            worker = CollectionProbeWorker(state["request"])
            worker.moveToThread(thread)
            thread.started.connect(worker.run)
            worker.metadata.connect(self._on_metadata, Qt.QueuedConnection)
            worker.entries.connect(self._on_entries, Qt.QueuedConnection)
            worker.single.connect(self._on_single, Qt.QueuedConnection)
            worker.failed.connect(self._on_failed, Qt.QueuedConnection)
            worker.finished.connect(self._on_finished, Qt.QueuedConnection)
            worker.finished.connect(thread.quit)
            worker.finished.connect(worker.deleteLater)
            thread.finished.connect(
                lambda request_id=request_id, thread=thread:
                self.defer_thread_finish(request_id, thread),
                Qt.QueuedConnection,
            )
        except Exception as exc:
            if worker is not None and thread is not None:
                delete_unstarted_worker(worker, thread)
            elif thread is not None:
                thread.deleteLater()
            try:
                self._on_start_error(request_id, exc)
            finally:
                self.states.pop(request_id, None)
            return False

        state["thread"] = thread
        state["worker"] = worker
        try:
            thread.start()
        except Exception as exc:
            state["thread"] = None
            state["worker"] = None
            delete_unstarted_worker(worker, thread)
            try:
                self._on_start_error(request_id, exc)
            finally:
                self.states.pop(request_id, None)
            return False
        return True

    def defer_thread_finish(self, request_id: str, thread: QThread) -> None:
        """Let queued result signals settle before releasing a concurrency slot."""

        if request_id in self.deferred_finishes:
            return
        self.deferred_finishes.add(request_id)
        QTimer.singleShot(
            0,
            lambda request_id=request_id, thread=thread:
            self.complete_thread_finish(request_id, thread),
        )

    def complete_thread_finish(self, request_id: str, thread: QThread) -> None:
        self.deferred_finishes.discard(request_id)
        state = self.states.get(request_id)
        if state is not None and state.get("thread") is thread:
            state["thread"] = None
            state["worker"] = None
            if state.get("confirmed"):
                self.states.pop(request_id, None)
        try:
            thread.deleteLater()
        except RuntimeError:
            pass
        self._on_slot_released()

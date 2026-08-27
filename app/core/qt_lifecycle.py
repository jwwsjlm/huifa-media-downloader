from __future__ import annotations

from PySide6.QtCore import QObject, QThread
from shiboken6 import delete as delete_qt_object


def delete_unstarted_worker(worker: QObject, thread: QThread) -> None:
    """Destroy a worker whose target QThread event loop never started.

    Once a QObject has been moved to a QThread, ``deleteLater`` depends on
    that thread's event loop. If ``QThread.start`` raises, the event loop will
    never run, so queued deletion leaks the worker and may destabilize a later
    Qt event-loop turn. Both objects are known idle here and may be destroyed
    synchronously, worker first.
    """

    for obj in (worker, thread):
        try:
            delete_qt_object(obj)
        except RuntimeError:
            pass


__all__ = ["delete_unstarted_worker"]

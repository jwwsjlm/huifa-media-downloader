from __future__ import annotations

from collections import deque
from enum import Enum
from typing import Callable, Iterable, TypeVar


TaskT = TypeVar("TaskT")


class QueueStartOutcome(Enum):
    READY = "ready"
    RETRY_LATER = "retry_later"
    DROPPED = "dropped"


class DownloadTaskQueue(deque[str]):
    """FIFO task queue with duplicate-safe lifecycle operations."""

    def append_unique(self, task_id: object) -> bool:
        normalized = str(task_id)
        if normalized in self:
            return False
        self.append(normalized)
        return True

    def appendleft_unique(self, task_id: object) -> bool:
        normalized = str(task_id)
        if normalized in self:
            return False
        self.appendleft(normalized)
        return True

    def extend_unique(self, task_ids: Iterable[object]) -> int:
        seen = set(self)
        added = 0
        for task_id in task_ids:
            normalized = str(task_id)
            if normalized in seen:
                continue
            self.append(normalized)
            seen.add(normalized)
            added += 1
        return added

    def remove_all(self, task_ids: Iterable[object]) -> int:
        targets = {str(task_id) for task_id in task_ids}
        if not targets or not self:
            return 0
        retained = [task_id for task_id in self if task_id not in targets]
        removed = len(self) - len(retained)
        if removed:
            self.clear()
            self.extend(retained)
        return removed

    def requeue_front(self, task_id: object) -> None:
        normalized = str(task_id)
        self.remove_all((normalized,))
        self.appendleft(normalized)

    def requeue_back(self, task_id: object) -> None:
        normalized = str(task_id)
        self.remove_all((normalized,))
        self.append(normalized)

    def take_next(
        self,
        resolve: Callable[[str], TaskT | None],
        is_runnable: Callable[[TaskT], bool],
    ) -> TaskT | None:
        """Discard stale entries and return the next runnable task."""

        while self:
            task = resolve(self.popleft())
            if task is not None and is_runnable(task):
                return task
        return None


__all__ = ["DownloadTaskQueue", "QueueStartOutcome"]

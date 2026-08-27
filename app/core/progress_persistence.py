from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass(slots=True)
class ProgressPersistenceBuffer:
    """Coalesce mutable task progress without owning database or Qt policy."""

    pending: dict[str, Any] = field(default_factory=dict)
    persisted_at: dict[str, float] = field(default_factory=dict)
    last_error_at: float = 0.0

    def should_write_immediately(self, task_id: str, *, force: bool) -> bool:
        normalized = str(task_id)
        return bool(
            force
            or (
                normalized not in self.persisted_at
                and normalized not in self.pending
            )
        )

    def enqueue(self, task: Any) -> None:
        self.pending[str(task.id)] = task

    def batch(self) -> tuple[Any, ...]:
        return tuple(self.pending.values())

    def mark_persisted(self, tasks: Iterable[Any], now: float) -> None:
        for task in tasks:
            task_id = str(task.id)
            if self.pending.get(task_id) is task:
                self.pending.pop(task_id, None)
            self.persisted_at[task_id] = now
        self.last_error_at = 0.0

    def forget(self, task_id: str) -> None:
        normalized = str(task_id)
        self.pending.pop(normalized, None)
        self.persisted_at.pop(normalized, None)

    def should_report_error(self, now: float, *, interval: float = 30.0) -> bool:
        if (
            self.last_error_at > 0.0
            and now - self.last_error_at < max(0.0, float(interval))
        ):
            return False
        self.last_error_at = now
        return True

    def clear(self) -> None:
        self.pending.clear()
        self.persisted_at.clear()
        self.last_error_at = 0.0


__all__ = ["ProgressPersistenceBuffer"]

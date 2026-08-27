from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from app.core.download_progress import (
    non_negative_float,
    non_negative_int,
    optional_non_negative_float,
)


TaskIndexState = tuple[str, str, float]


def _decrement_counter(counter: Counter[str], key: str) -> None:
    counter[key] -= 1
    if counter[key] <= 0:
        counter.pop(key, None)


@dataclass(slots=True)
class DownloadTaskIndex:
    """Constant-time task status, hierarchy and transfer-speed index."""

    states: dict[str, TaskIndexState] = field(default_factory=dict)
    _children_by_parent: dict[str, set[str]] = field(
        default_factory=lambda: defaultdict(set)
    )
    status_counts_all: Counter[str] = field(default_factory=Counter)
    status_counts_top: Counter[str] = field(default_factory=Counter)
    _total_speed_bps: float = 0.0

    @staticmethod
    def _normalize_state(
        parent_id: Any,
        status: Any,
        speed_bps: Any,
    ) -> TaskIndexState:
        return (
            str(parent_id or ""),
            str(status or "queued"),
            non_negative_float(speed_bps),
        )

    def _remove_from_parent(self, task_id: str, parent_id: str) -> None:
        if not parent_id:
            return
        siblings = self._children_by_parent.get(parent_id)
        if siblings is None:
            return
        siblings.discard(task_id)
        if not siblings:
            self._children_by_parent.pop(parent_id, None)

    def _repair_total_speed_if_needed(self) -> float:
        normalized = optional_non_negative_float(self._total_speed_bps)
        if normalized is not None:
            return normalized
        normalized = sum(state[2] for state in self.states.values())
        self._total_speed_bps = normalized
        return normalized

    def _apply_speed_delta(self, previous: float, current: float) -> None:
        total = self._repair_total_speed_if_needed() - previous + current
        if abs(total) < 1e-6:
            total = 0.0
        self._total_speed_bps = max(0.0, total)

    def sync(
        self,
        task_id: Any,
        *,
        parent_id: Any,
        status: Any,
        speed_bps: Any,
    ) -> bool:
        normalized_id = str(task_id)
        current = self._normalize_state(parent_id, status, speed_bps)
        previous = self.states.get(normalized_id)
        if previous == current:
            return False

        if previous is not None:
            previous_parent, previous_status, previous_speed = previous
            _decrement_counter(self.status_counts_all, previous_status)
            if previous_parent:
                self._remove_from_parent(normalized_id, previous_parent)
            else:
                _decrement_counter(self.status_counts_top, previous_status)
        else:
            previous_speed = 0.0

        parent, normalized_status, speed = current
        self.status_counts_all[normalized_status] += 1
        if parent:
            self._children_by_parent[parent].add(normalized_id)
        else:
            self.status_counts_top[normalized_status] += 1
        self._apply_speed_delta(previous_speed, speed)
        self.states[normalized_id] = current
        return True

    def remove(self, task_id: Any) -> TaskIndexState | None:
        normalized_id = str(task_id)
        previous = self.states.get(normalized_id)
        if previous is None:
            return None
        parent_id, status, speed = previous
        _decrement_counter(self.status_counts_all, status)
        if parent_id:
            self._remove_from_parent(normalized_id, parent_id)
        else:
            _decrement_counter(self.status_counts_top, status)
        self._apply_speed_delta(speed, 0.0)
        self.states.pop(normalized_id, None)
        return previous

    def clear(self) -> None:
        self.states.clear()
        self._children_by_parent.clear()
        self.status_counts_all.clear()
        self.status_counts_top.clear()
        self._total_speed_bps = 0.0

    @property
    def total_speed_bps(self) -> float:
        return self._repair_total_speed_if_needed()

    def child_ids(self, parent_id: Any) -> frozenset[str]:
        return frozenset(self._children_by_parent.get(str(parent_id), ()))

    def _counts(self, top_level_only: bool) -> Counter[str]:
        return self.status_counts_top if top_level_only else self.status_counts_all

    def statistics(self, *, top_level_only: bool = False) -> dict[str, int]:
        counts = self._counts(top_level_only)

        def count(*statuses: str) -> int:
            return sum(non_negative_int(counts.get(status)) for status in statuses)

        failed = count("failed", "partial_failed", "canceled")
        resumable_paused = count("paused")
        paused = resumable_paused + count("暂停中")
        completed = count("completed")
        return {
            "total": sum(non_negative_int(value) for value in counts.values()),
            "active": count("downloading", "parsing_collection", "canceling"),
            "queued": count("queued"),
            "paused": paused,
            "processing": count("processing"),
            "completed": completed,
            "failed": failed,
            "pausable": count("downloading", "queued"),
            "resumable": resumable_paused + failed,
            "cleanable": completed,
        }

__all__ = ["DownloadTaskIndex", "TaskIndexState"]

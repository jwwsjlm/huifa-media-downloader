from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.download_service import DownloadTask


TASK_FILTER_STATUSES: dict[str, frozenset[str] | None] = {
    "全部": None,
    "下载中": frozenset({"downloading", "canceling", "parsing_collection"}),
    "排队中": frozenset({"queued"}),
    "已暂停": frozenset({"paused", "暂停中"}),
    "处理中": frozenset({
        "waiting_selection",
        "parsing_collection",
        "canceling",
        "暂停中",
        "processing",
    }),
    "已完成": frozenset({"completed"}),
    "文件已删除": frozenset({"deleted"}),
    "失败": frozenset({"failed", "partial_failed", "canceled"}),
}

TASK_STATUS_SORT_ORDER: dict[str, int] = {
    "downloading": 0,
    "processing": 0,
    "canceling": 0,
    "parsing_collection": 1,
    "waiting_selection": 1,
    "queued": 2,
    "暂停中": 3,
    "paused": 3,
    "failed": 4,
    "partial_failed": 4,
    "canceled": 5,
    "completed": 6,
    "deleted": 7,
}


@dataclass(slots=True)
class TaskListPagingState:
    """Own the mutable paging state used by the QWidget task-card list."""

    ordered_ids: list[str] = field(default_factory=list)
    pending_ids: deque[str] = field(default_factory=deque)
    render_goal: int = 0
    loading: bool = True
    append_pending: bool = False

    def set_ordered(
        self,
        ordered_ids: Iterable[str],
        materialized_ids: Iterable[str],
    ) -> None:
        self.ordered_ids = [str(task_id) for task_id in ordered_ids]
        materialized = {str(task_id) for task_id in materialized_ids}
        self.pending_ids = deque(
            task_id for task_id in self.ordered_ids
            if task_id not in materialized
        )
        self.append_pending = (
            len(materialized) <= len(self.ordered_ids)
            and all(
                task_id in materialized
                for task_id in self.ordered_ids[:len(materialized)]
            )
        )

    def begin_restore(self, ordered_ids: Iterable[str], page_size: int) -> None:
        self.ordered_ids = [str(task_id) for task_id in ordered_ids]
        self.pending_ids = deque(self.ordered_ids)
        self.render_goal = min(max(0, int(page_size)), len(self.ordered_ids))
        self.loading = self.render_goal > 0
        self.append_pending = True

    def begin_more(
        self,
        materialized_count: int,
        remaining_count: int,
        page_size: int,
    ) -> bool:
        count = min(
            max(0, int(page_size)),
            max(0, int(remaining_count)),
        )
        if count <= 0:
            return False
        self.render_goal = max(0, int(materialized_count)) + count
        self.loading = True
        return True

    def prioritize(self, matches: Callable[[str], bool]) -> list[str]:
        matching: list[str] = []
        remaining: list[str] = []
        for task_id in self.pending_ids:
            (matching if matches(task_id) else remaining).append(task_id)
        self.pending_ids = deque([*matching, *remaining])
        if matching and remaining:
            self.append_pending = False
        return matching

    def remove(self, task_id: str) -> None:
        task_id = str(task_id or "")
        self.ordered_ids = [value for value in self.ordered_ids if value != task_id]
        self.pending_ids = deque(
            value for value in self.pending_ids if value != task_id
        )

    def materialized_row(
        self,
        task_id: str,
        materialized_ids: Iterable[str],
    ) -> int:
        """Return the visual row that preserves the canonical task order."""

        task_id = str(task_id or "")
        try:
            ordered_index = self.ordered_ids.index(task_id)
        except ValueError:
            return -1
        materialized = {str(value) for value in materialized_ids}
        return sum(
            preceding_id in materialized
            for preceding_id in self.ordered_ids[:ordered_index]
        )

    def finish(self) -> None:
        self.loading = False

    def clear(self) -> None:
        self.ordered_ids.clear()
        self.pending_ids.clear()
        self.render_goal = 0
        self.loading = False
        self.append_pending = False


def task_matches_filter(
    task: DownloadTask,
    filter_name: str,
    query: str,
) -> bool:
    """Return whether a task matches one dashboard status/search filter."""

    allowed = TASK_FILTER_STATUSES.get(str(filter_name or "全部"))
    if allowed and task.status not in allowed:
        return False
    needle = str(query or "").strip().casefold()
    if not needle:
        return True
    haystack = " ".join((
        str(task.id or ""),
        str(task.title or ""),
        str(task.url or ""),
    )).casefold()
    return needle in haystack


def ordered_top_level_tasks(
    tasks: Iterable[DownloadTask],
    sort_mode: str,
) -> list[DownloadTask]:
    """Sort top-level tasks deterministically for dashboard presentation."""

    top_level = [task for task in tasks if not task.parent_task_id]

    def chronological_key(task: DownloadTask) -> tuple[str, str]:
        return str(task.created_at or ""), str(task.id or "")

    mode = str(sort_mode or "newest")
    if mode == "oldest":
        return sorted(top_level, key=chronological_key)
    if mode == "title":
        return sorted(
            top_level,
            key=lambda task: (
                str(task.title or task.url or "").casefold(),
                *chronological_key(task),
            ),
        )
    if mode == "status":
        return sorted(
            top_level,
            key=lambda task: (
                TASK_STATUS_SORT_ORDER.get(task.status, 8),
                *chronological_key(task),
            ),
        )
    return sorted(top_level, key=chronological_key, reverse=True)

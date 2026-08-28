from __future__ import annotations

from collections.abc import Sequence
from typing import Any


from app.core.download_service import DownloadTask
from app.ui.i18n import text as ui_text
from app.ui.task_list import TaskListPagingState, ordered_top_level_tasks


def enrich_completed_task_metadata(
    tasks: Sequence[DownloadTask],
    database: Any,
) -> bool:
    """Restore catalog-only card metadata with one bounded database lookup."""

    completed = [task for task in tasks if task.status == "completed"]
    if not completed or database is None:
        return False

    try:
        urls = list(dict.fromkeys(
            str(task.url or "")
            for task in completed
            if str(task.url or "")
        ))
        media_items = list(database.latest_media_by_source_urls(urls).values())
    except Exception:
        # Optional card metadata must not prevent durable history loading.
        return False

    by_path = {
        str(media.video_path): media
        for media in media_items
        if str(media.video_path or "")
    }
    by_url = {
        str(media.source_url): media
        for media in media_items
        if str(media.source_url or "")
    }
    enriched = False
    for task in completed:
        media = by_path.get(str(task.media_path or "")) or by_url.get(
            str(task.url or "")
        )
        if media is None:
            continue
        task.uploader = media.uploader or ""
        task.downloaded_at = media.downloaded_at or ""
        task.media_path = task.media_path or media.video_path
        task.thumbnail_path = task.thumbnail_path or media.thumbnail_path
        enriched = True
    return enriched


class TaskListRestoreController:
    """Page restored task history without materializing every card widget."""

    def __init__(
        self,
        page: Any,
        *,
        page_size: int,
        batch_size: int,
    ) -> None:
        self.page = page
        self.service = page.window.download_service
        self.paging = page.task_paging
        self.render_timer = page._task_render_timer
        self.items = page.items
        self.status_label = page.status
        self.load_more_button = page.load_more_button
        self.page_size = max(1, int(page_size))
        self.batch_size = max(1, int(batch_size))

    def database(self) -> Any:
        return self.page.window.db

    def sort_mode(self) -> str:
        return str(self.page.sort_box.currentData() or "newest")

    def clear_rows(self) -> None:
        self.page.task_rows.clear()

    def create_row(self, task: DownloadTask, row: int | None) -> None:
        self.page.task_rows.create(task, row)

    def task_matches(self, task: DownloadTask) -> bool:
        return self.page.task_presentation.task_matches(task)

    def apply_filter(self) -> None:
        self.page.task_presentation.apply_filter()

    def refresh_presentation(self) -> None:
        self.page.task_presentation.refresh()

    def set_loaded(self) -> None:
        self.paging.finish()
        self.refresh_presentation()
        self.update_load_more_button()

    def begin(self, tasks: Sequence[DownloadTask]) -> None:
        enrich_completed_task_metadata(tasks, self.database())
        ordered = ordered_top_level_tasks(tasks, self.sort_mode())
        self.render_timer.stop()
        self.clear_rows()
        self.paging.begin_restore(
            (task.id for task in ordered),
            self.page_size,
        )
        if self.paging.render_goal:
            self.render_timer.start()
            self.refresh_presentation()
        else:
            self.set_loaded()

    def render_batch(self) -> None:
        added = 0
        while (
            self.paging.pending_ids
            and len(self.items) < self.paging.render_goal
            and added < self.batch_size
        ):
            task_id = self.paging.pending_ids.popleft()
            if task_id in self.items:
                continue
            task = self.service.tasks.get(task_id)
            if task is None or task.parent_task_id:
                continue
            row = self.paging.materialized_row(task_id, self.items)
            self.create_row(task, row if row >= 0 else None)
            added += 1
        if (
            self.paging.pending_ids
            and len(self.items) < self.paging.render_goal
        ):
            return
        self.render_timer.stop()
        self.paging.finish()
        self.apply_filter()
        self.update_load_more_button()
        self.status_label.setText(ui_text('Task history loaded'))

    def remaining_count(self) -> int:
        return sum(
            1
            for task_id in self.paging.pending_ids
            if (task := self.service.tasks.get(task_id)) is not None
            and self.task_matches(task)
        )

    def update_load_more_button(self) -> None:
        self._set_load_more_available(self.remaining_count())

    def _set_load_more_available(self, remaining: int) -> None:
        available = remaining > 0 and not self.paging.loading
        self.load_more_button.setVisible(available)
        self.load_more_button.setEnabled(available)

    def load_more(self) -> None:
        remaining = self.prioritize_pending_matches()
        if remaining <= 0:
            return
        if not self.paging.begin_more(
            len(self.items),
            remaining,
            self.page_size,
        ):
            return
        self.refresh_presentation()
        self.render_timer.start()

    def prioritize_pending_matches(self) -> int:
        if not self.paging.pending_ids:
            self._set_load_more_available(0)
            return 0

        def matches(task_id: str) -> bool:
            task = self.service.tasks.get(task_id)
            return task is not None and self.task_matches(task)

        matching = self.paging.prioritize(matches)
        materialized_matches = sum(
            1
            for task_id in self.items
            if (task := self.service.tasks.get(task_id)) is not None
            and self.task_matches(task)
        )
        if matching and materialized_matches == 0 and not self.render_timer.isActive():
            if self.paging.begin_more(
                len(self.items),
                len(matching),
                self.page_size,
            ):
                self.render_timer.start()
        remaining = len(matching)
        self._set_load_more_available(remaining)
        return remaining

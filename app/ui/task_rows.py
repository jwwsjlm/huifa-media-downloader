from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtWidgets import QListWidgetItem

from app.core.download_service import DownloadTask
from app.ui.task_card import TASK_CARD_HEIGHT, DownloadTaskCard
from app.ui.task_list import TaskListPagingState, ordered_top_level_tasks

try:
    from shiboken6 import isValid as _qt_object_is_valid
except ImportError:
    _qt_object_is_valid = None


def _widget_is_valid(widget: Any) -> bool:
    if widget is None:
        return False
    if _qt_object_is_valid is None:
        return True
    try:
        return bool(_qt_object_is_valid(widget))
    except (RuntimeError, ReferenceError):
        return False


def _delete_widget_later(widget: Any) -> None:
    if widget is None:
        return
    try:
        widget.deleteLater()
    except (RuntimeError, ReferenceError):
        pass


class TaskRowController:
    """Own QListWidget row/card lifetime and canonical visual ordering."""

    def __init__(self, page: Any, *, page_size: int) -> None:
        self.page = page
        self.service = page.window.download_service
        self.task_list = page.task_list
        self.items = page.items
        self.cards = page.cards
        self.paging = page.task_paging
        self.render_timer = page._task_render_timer
        self.page_size = max(1, int(page_size))
        self._rebuild_pending: set[str] = set()
        self._width_sync_pending = False

    def sort_mode(self) -> str:
        return str(self.page.sort_box.currentData() or "newest")

    def card_factory(self, task: DownloadTask) -> DownloadTaskCard:
        return self.page._new_task_card(task)

    def apply_filter(self) -> None:
        self.page.task_presentation.apply_filter()

    def sync_selection(self) -> None:
        self.page.task_presentation.sync_selection()

    def refresh_ordered_ids(self) -> None:
        ordered = ordered_top_level_tasks(
            self.service.tasks.values(),
            self.sort_mode(),
        )
        self.paging.set_ordered(
            (task.id for task in ordered),
            self.items,
        )

    def remove_materialized(self, task_id: str) -> None:
        item = self.items.pop(task_id, None)
        card = self.cards.pop(task_id, None)
        attached_card = self.task_list.itemWidget(item) if item is not None else None
        if item is not None:
            self.task_list.removeItemWidget(item)
            row = self.task_list.row(item)
            if row >= 0:
                removed = self.task_list.takeItem(row)
                del removed
        _delete_widget_later(card)
        if attached_card is not card:
            _delete_widget_later(attached_card)

    def insert_new(self, task: DownloadTask, *, refresh: bool = True) -> None:
        self.insert_many((task,), refresh=refresh)

    def insert_many(
        self,
        tasks: Iterable[DownloadTask],
        *,
        refresh: bool = True,
    ) -> None:
        candidates = tuple(tasks)
        if not candidates:
            return
        self.refresh_ordered_ids()
        ordered_positions = {
            task_id: index
            for index, task_id in enumerate(self.paging.ordered_ids)
        }
        for task in candidates:
            ordered_index = ordered_positions.get(task.id)
            if ordered_index is not None:
                self._insert_ordered(task, ordered_index)
        self.paging.set_ordered(
            self.paging.ordered_ids,
            self.items,
        )
        if refresh:
            self.apply_filter()

    def _insert_ordered(self, task: DownloadTask, ordered_index: int) -> None:
        current_count = self.task_list.count()
        materialized_capacity = max(
            current_count,
            min(self.page_size, len(self.paging.ordered_ids)),
        )
        if ordered_index >= materialized_capacity:
            return
        row = self.paging.materialized_row(task.id, self.items)
        self.create(task, row=row if row >= 0 else None)
        if self.task_list.count() > materialized_capacity:
            overflow_id = next((
                task_id
                for task_id in reversed(self.paging.ordered_ids)
                if task_id in self.items
            ), "")
            if overflow_id:
                self.remove_materialized(overflow_id)

    def create(self, task: DownloadTask, row: int | None = None) -> None:
        existing_item = self.items.get(task.id)
        if existing_item is not None:
            existing_card = self.cards.get(task.id)
            if (
                _widget_is_valid(existing_card)
                and self.task_list.itemWidget(existing_item) is existing_card
            ):
                existing_card.update_task(task)
            else:
                self.schedule_rebuild(task.id)
            return

        # Build the card before touching QListWidget. If card construction or
        # signal wiring fails, no orphan blank row is left behind.
        card = self.card_factory(task)
        item = QListWidgetItem()
        item.setData(Qt.UserRole, task.id)
        item.setSizeHint(QSize(0, TASK_CARD_HEIGHT))
        item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        try:
            if row is None or row < 0 or row >= self.task_list.count():
                self.task_list.addItem(item)
            else:
                self.task_list.insertItem(row, item)
            card.setFixedWidth(max(320, self.task_list.viewport().width() - 8))
            self.task_list.setItemWidget(item, card)
            if self.task_list.itemWidget(item) is not card:
                raise RuntimeError("task card was not attached to its list row")
        except Exception:
            item_row = self.task_list.row(item)
            if item_row >= 0:
                removed = self.task_list.takeItem(item_row)
                del removed
            _delete_widget_later(card)
            raise
        self.items[task.id] = item
        self.cards[task.id] = card
        self.schedule_width_sync()

    def schedule_width_sync(self) -> None:
        if self._width_sync_pending:
            return
        self._width_sync_pending = True
        QTimer.singleShot(0, self.task_list, self.sync_widths)

    def sync_widths(self) -> None:
        self._width_sync_pending = False
        card_width = max(320, self.task_list.viewport().width() - 8)
        for task_id, card in list(self.cards.items()):
            if _widget_is_valid(card):
                try:
                    card.setFixedWidth(card_width)
                except (RuntimeError, ReferenceError):
                    self.schedule_rebuild(task_id)
            else:
                self.schedule_rebuild(task_id)

    def schedule_rebuild(self, task_id: str) -> None:
        if task_id in self._rebuild_pending:
            return
        self._rebuild_pending.add(task_id)
        QTimer.singleShot(
            0,
            self.task_list,
            lambda task_id=task_id: self.rebuild(task_id),
        )

    def rebuild(self, task_id: str) -> None:
        self._rebuild_pending.discard(task_id)
        task = self.service.tasks.get(task_id)
        item = self.items.get(task_id)
        if task is None or item is None:
            return
        row = self.task_list.row(item)
        selected = item.isSelected()
        self.remove_materialized(task_id)
        self.create(task, row=max(0, row))
        replacement = self.items.get(task_id)
        if replacement is not None:
            replacement.setSelected(selected)
        self.sync_selection()

    def clear(self) -> None:
        self.task_list.clearSelection()
        for task_id, item in list(self.items.items()):
            card = self.cards.get(task_id)
            attached_card = self.task_list.itemWidget(item)
            self.task_list.removeItemWidget(item)
            _delete_widget_later(card)
            if attached_card is not card:
                _delete_widget_later(attached_card)
        self.task_list.clear()
        self.items.clear()
        self.cards.clear()
        self._rebuild_pending.clear()

    def sort(self, selected_ids: set[str]) -> None:
        self.render_timer.stop()
        self.refresh_ordered_ids()
        materialized_count = max(
            min(self.page_size, len(self.paging.ordered_ids)),
            self.task_list.count(),
        )
        visible_ids = self.paging.ordered_ids[:materialized_count]
        self.clear()
        for task_id in visible_ids:
            task = self.service.tasks.get(task_id)
            if task is not None:
                self.create(task)
        self.paging.set_ordered(
            self.paging.ordered_ids,
            self.items,
        )
        self.paging.render_goal = len(self.items)
        self.paging.finish()
        for task_id in selected_ids:
            item = self.items.get(task_id)
            if item is not None:
                item.setSelected(True)
        self.sync_selection()
        self.apply_filter()


__all__ = ["TaskRowController"]

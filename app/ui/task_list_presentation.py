from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from PySide6.QtCore import QSignalBlocker, Qt, QTimer
from PySide6.QtWidgets import QAbstractItemView

from app.ui.i18n import format_text as ui_format
from app.ui.i18n import text as ui_text
from app.ui.task_list import TaskListPagingState, task_matches_filter


class TaskListPresentationController:
    """Keep task filtering, selection and summary widgets consistent."""

    def __init__(self, page: Any) -> None:
        self.page = page
        self.service = page.window.download_service
        self.task_list = page.task_list
        self.task_content_stack = page.task_content_stack
        self.empty_label = page.empty_label
        self.search_box = page.search_box
        self.filter_box = page.filter_box
        self.count_label = page.count_label
        self.metric_cards = page.metric_cards
        self.pause_all_button = page.pause_all_button
        self.resume_all_button = page.resume_all_button
        self.cleanup_button = page.cleanup_button
        self.log_button = page.log_button
        self.paging = page.task_paging
        self.items = page.items
        self.cards = page.cards
        self.search_filter_timer = page._search_filter_timer
        self.prioritize_pending_matches = page._prioritize_pending_matches
        self._selection_anchor_row = -1

    def reset_selection_anchor(self) -> None:
        self._selection_anchor_row = -1

    def selected_ids(self) -> list[str]:
        return [
            str(item.data(Qt.UserRole) or "")
            for item in self.task_list.selectedItems()
            if item.data(Qt.UserRole) and not item.isHidden()
        ]

    def select_from_card(self, task_id: str, modifiers: Any) -> None:
        item = self.items.get(task_id)
        if item is None or item.isHidden():
            return
        row = self.task_list.row(item)
        anchor = self._selection_anchor_row
        if anchor < 0:
            anchor = self.task_list.currentRow()
        if modifiers & Qt.ShiftModifier and anchor >= 0:
            start, end = sorted((anchor, row))
            self.task_list.clearSelection()
            for current_row in range(start, end + 1):
                list_item = self.task_list.item(current_row)
                if list_item is not None and not list_item.isHidden():
                    list_item.setSelected(True)
        elif modifiers & Qt.ControlModifier:
            item.setSelected(not item.isSelected())
            self._selection_anchor_row = row
        else:
            self.task_list.clearSelection()
            item.setSelected(True)
            self._selection_anchor_row = row
        self.task_list.setCurrentItem(item)
        self.sync_selection()

    def sync_selection(self) -> None:
        selected = set(self.selected_ids())
        for task_id, card in list(self.cards.items()):
            try:
                card.set_selected(task_id in selected)
            except (RuntimeError, ReferenceError):
                continue
        self.log_button.setEnabled(len(selected) == 1)

    def task_matches(self, task: Any) -> bool:
        return task_matches_filter(
            task,
            str(self.filter_box.currentData() or "全部"),
            self.search_box.text(),
        )

    def apply_filter(self) -> None:
        self.search_filter_timer.stop()
        self.prioritize_pending_matches()
        selection_changed = False
        for task_id, item in list(self.items.items()):
            task = self.service.tasks.get(task_id)
            hidden = task is None or not self.task_matches(task)
            if hidden and item.isSelected():
                item.setSelected(False)
                selection_changed = True
            item.setHidden(hidden)
        self.sync_metric_selection()
        if selection_changed:
            self.reset_selection_anchor()
        self.sync_selection()
        self.refresh()

    def activate_metric_filter(self, filter_name: str) -> None:
        index = self.filter_box.findData(filter_name)
        if index < 0:
            return
        # Both widgets normally emit filter requests. Block them here and
        # perform one coherent refresh after the two values have changed.
        with QSignalBlocker(self.search_box), QSignalBlocker(self.filter_box):
            self.search_box.clear()
            self.filter_box.setCurrentIndex(index)
        self.apply_filter()

    def sync_metric_selection(self) -> None:
        selected = str(self.filter_box.currentData() or "全部")
        for filter_name, metric in self.metric_cards.items():
            metric.set_active(filter_name == selected)

    def refresh(self, statistics: Mapping[str, int] | None = None) -> None:
        stats = dict(
            statistics
            if statistics is not None
            else self.service.task_statistics(top_level_only=True)
        )
        self._refresh_count(stats)
        self._refresh_empty_state(stats)
        self._refresh_action_states(stats)

    def _refresh_count(self, stats: Mapping[str, int]) -> None:
        count = int(stats.get("total", 0))
        self.count_label.setText(ui_format(
            '{count} tasks',
            context="task.count.total",
            count=count,
        ))
        metric_values = {
            "全部": count,
            "下载中": int(stats.get("active", 0)),
            "排队中": int(stats.get("queued", 0)),
            "已暂停": int(stats.get("paused", 0)),
            "已完成": int(stats.get("completed", 0)),
            "失败": int(stats.get("failed", 0)),
        }
        for filter_name, value in metric_values.items():
            metric = self.metric_cards.get(filter_name)
            if metric is not None:
                metric.set_value(value)
        self.sync_metric_selection()

    def _refresh_empty_state(self, stats: Mapping[str, int]) -> None:
        total_count = int(stats.get("total", 0))
        visible_count = sum(
            1
            for index in range(self.task_list.count())
            if not self.task_list.item(index).isHidden()
        )
        if self.paging.loading:
            self.empty_label.setText(ui_text('Loading task history…'))
            self.task_content_stack.setCurrentWidget(self.empty_label)
        elif total_count == 0:
            self.task_list.clearSelection()
            self.task_list.setSelectionMode(QAbstractItemView.NoSelection)
            self.empty_label.setText(ui_text(
                'No download tasks yet\nPaste a video or playlist URL and click Add & Download\nChange the download folder in Settings',
            ))
            self.task_content_stack.setCurrentWidget(self.empty_label)
        elif visible_count == 0:
            self.task_list.clearSelection()
            self.task_list.setSelectionMode(QAbstractItemView.NoSelection)
            self.empty_label.setText(ui_text(
                'No matching tasks\nTry another search term or status filter',
            ))
            self.task_content_stack.setCurrentWidget(self.empty_label)
        else:
            self.task_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
            self.task_content_stack.setCurrentWidget(self.task_list)
        self.sync_selection()

    def _refresh_action_states(self, stats: Mapping[str, int]) -> None:
        self.pause_all_button.setEnabled(int(stats["pausable"]) > 0)
        self.resume_all_button.setEnabled(int(stats["resumable"]) > 0)
        self.cleanup_button.setEnabled(int(stats["cleanable"]) > 0)


__all__ = [
    "TaskListPresentationController",
]

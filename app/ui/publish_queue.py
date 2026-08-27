from __future__ import annotations

from collections import deque

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.storage.models import MediaItem
from app.ui.i18n import format_text as ui_format
from app.ui.i18n import runtime_text
from app.ui.i18n import text as ui_text
from app.ui.media_presentation import platform_label


PUBLISH_QUEUE_PAGE_SIZE = 100
PUBLISH_QUEUE_RENDER_BATCH_SIZE = 20
PUBLISH_QUEUE_SEARCH_DEBOUNCE_MS = 300

PUBLISH_STATUS_TEXT = {
    "pending": "Pending",
    "uploading": "Publishing",
    "success": "Succeeded",
    "failed": "Failed",
}


def publish_queue_search_text(row) -> str:
    """Build one localized and raw search index for a publication task."""

    platform = str(row["platform"] or "")
    status = str(row["status"] or "")
    localized_status = ui_text(PUBLISH_STATUS_TEXT.get(status, status))
    return " ".join(
        str(value or "")
        for value in (
            row["id"],
            platform,
            platform_label(platform),
            row["account"],
            status,
            localized_status,
            row["title"],
            runtime_text(row["result"] or ""),
        )
    ).lower()


class PublishQueuePage(QWidget):
    """Paged publication queue with localized search and incremental updates."""

    def __init__(self, window) -> None:
        super().__init__()
        self.window = window
        self.loaded = False
        self.dirty = True
        self._media_filter_id = 0
        self.items: dict[int, QTreeWidgetItem] = {}
        self._pending_rows: deque = deque()
        self._queue_total = 0
        self._queue_render_goal = 0
        self._queue_filter_materialized_key = ""
        self._restore_selected_task_id = 0

        self._queue_render_timer = QTimer(self)
        self._queue_render_timer.setInterval(0)
        self._queue_render_timer.timeout.connect(self._render_queue_batch)
        self._search_filter_timer = QTimer(self)
        self._search_filter_timer.setSingleShot(True)
        self._search_filter_timer.setInterval(PUBLISH_QUEUE_SEARCH_DEBOUNCE_MS)
        self._search_filter_timer.timeout.connect(self.apply_filter)

        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        title = QLabel(ui_text("Publish Queue"))
        title.setObjectName("pageTitle")
        header.addWidget(title)
        self.media_scope = QLabel()
        self.media_scope.setObjectName("mutedText")
        self.media_scope.setMinimumWidth(0)
        self.media_scope.setMaximumWidth(180)
        self.media_scope.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.media_scope.hide()
        header.addWidget(self.media_scope)
        self.clear_media_scope = QPushButton(ui_text("Show All"))
        self.clear_media_scope.setToolTip(ui_text(
            "Clear the video filter and show all publish tasks"
        ))
        self.clear_media_scope.clicked.connect(self.clear_media_filter)
        self.clear_media_scope.hide()
        header.addWidget(self.clear_media_scope)
        header.addStretch(1)
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText(ui_text(
            "Search title, platform, status or result"
        ))
        self.search_box.setMinimumWidth(120)
        self.search_box.setMaximumWidth(260)
        self.search_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.search_box.textChanged.connect(self._schedule_search_filter)
        header.addWidget(self.search_box)
        self.refresh_button = QPushButton(ui_text("Refresh"))
        self.refresh_button.clicked.connect(self.refresh)
        self.refresh_button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        header.addWidget(self.refresh_button)
        self.run_button = QPushButton(ui_text("Run Selected"))
        self.run_button.clicked.connect(self.run_selected)
        self.run_button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        header.addWidget(self.run_button)
        self.retry_button = QPushButton(ui_text("Retry Failed"))
        self.retry_button.clicked.connect(self.retry_selected)
        self.retry_button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        header.addWidget(self.retry_button)
        layout.addLayout(header)

        self.tree = QTreeWidget()
        self.tree.setObjectName("publishQueueTree")
        self.tree.setHeaderLabels([
            ui_text("ID"),
            ui_text("Platform"),
            ui_text("Account", context="publish_queue.column"),
            ui_text("Status"),
            ui_text("Title"),
            ui_text("Result"),
        ])
        self.tree.setMinimumHeight(260)
        self.tree.setAlternatingRowColors(True)
        self.tree.setRootIsDecorated(False)
        self.tree.setItemsExpandable(False)
        self.tree.setIndentation(0)
        self.tree.currentItemChanged.connect(lambda *_: self._sync_action_state())
        tree_header = self.tree.header()
        tree_header.setStretchLastSection(False)
        for column, width in ((0, 64), (1, 110), (2, 110), (3, 100), (4, 260)):
            tree_header.resizeSection(column, width)
        tree_header.setSectionResizeMode(4, QHeaderView.Stretch)
        tree_header.setSectionResizeMode(5, QHeaderView.Stretch)
        self.queue_stack = QStackedWidget()
        self.queue_stack.setMinimumHeight(260)
        self.queue_stack.addWidget(self.tree)
        self.empty_state = QLabel(ui_text(
            "No publish tasks yet\nSelect a video in Completed to create a publish task"
        ))
        self.empty_state.setObjectName("emptyState")
        self.empty_state.setAlignment(Qt.AlignCenter)
        self.empty_state.setWordWrap(True)
        self.queue_stack.addWidget(self.empty_state)
        layout.addWidget(self.queue_stack, 1)
        self.load_more_button = QPushButton(ui_text("Load More Publish Tasks"))
        self.load_more_button.clicked.connect(self.load_more_tasks)
        self.load_more_button.hide()
        layout.addWidget(self.load_more_button, 0, Qt.AlignHCenter)
        self._sync_action_state()

    def _schedule_search_filter(self, _value: str = "") -> None:
        self._search_filter_timer.start()

    def ensure_loaded(self) -> None:
        if not self.loaded or self.dirty:
            self.refresh()

    def mark_dirty(self) -> None:
        self.dirty = True
        if self.isVisible():
            self.refresh()

    def focus_media(self, media: MediaItem) -> None:
        self._media_filter_id = int(media.id or 0)
        title = (media.title or ui_text("Untitled video")).strip()
        metrics = QFontMetrics(self.font())
        self.media_scope.setText(
            ui_text("Current video: ") + metrics.elidedText(title, Qt.ElideRight, 260)
        )
        self.media_scope.setToolTip(
            ui_text("Showing publish tasks for this video only:\n") + title
        )
        enabled = self._media_filter_id > 0
        self.media_scope.setVisible(enabled)
        self.clear_media_scope.setVisible(enabled)
        self.refresh()

    def focus_task(self, task_id: int) -> QTreeWidgetItem | None:
        """Reveal a task even when the queue was scoped, searched, or paged."""

        selected_task_id = int(task_id or 0)
        if selected_task_id <= 0:
            return None
        needs_refresh = (
            not self.loaded
            or self.dirty
            or self._media_filter_id > 0
            or bool(self.search_box.text().strip())
        )
        self._media_filter_id = 0
        self.media_scope.hide()
        self.clear_media_scope.hide()
        self.search_box.clear()
        self._search_filter_timer.stop()
        if needs_refresh:
            self.refresh()
        row = self.window.db.get_publish_task(selected_task_id)
        if row is None:
            return None
        item = self.items.get(selected_task_id)
        if item is None:
            self._pending_rows = deque(
                pending
                for pending in self._pending_rows
                if int(pending["id"] or 0) != selected_task_id
            )
            item = self._insert_queue_row(row, index=0)
            self._queue_render_goal = max(
                self._queue_render_goal,
                len(self.items) + len(self._pending_rows),
            )
        self.apply_filter()
        self.tree.setCurrentItem(item)
        self.tree.scrollToItem(item)
        return item

    def clear_media_filter(self) -> None:
        self._media_filter_id = 0
        self.media_scope.hide()
        self.clear_media_scope.hide()
        self.refresh()

    def refresh(self) -> None:
        selected_item = self.tree.currentItem()
        selected_task_id = int(selected_item.text(0)) if selected_item else 0
        self._search_filter_timer.stop()
        self._queue_render_timer.stop()
        self.tree.clear()
        self.items.clear()
        self._queue_filter_materialized_key = ""
        self._restore_selected_task_id = selected_task_id
        self._queue_total = self._database_queue_count()
        rows = self._database_queue_page(PUBLISH_QUEUE_PAGE_SIZE, 0)
        self._pending_rows = deque(rows)
        self._queue_render_goal = min(PUBLISH_QUEUE_PAGE_SIZE, self._queue_total)
        self.loaded = True
        self.dirty = False
        if self._pending_rows:
            self._queue_render_timer.start()
        else:
            self.apply_filter()
            self._update_queue_load_button()
        self._sync_action_state()

    def _insert_queue_row(self, row, *, index: int | None = None) -> QTreeWidgetItem:
        task_id = int(row["id"] or 0)
        item = QTreeWidgetItem()
        self._update_item(item, row)
        if index is None:
            self.tree.addTopLevelItem(item)
        else:
            self.tree.insertTopLevelItem(max(0, index), item)
        self.items[task_id] = item
        return item

    def _database_queue_count(self) -> int:
        return max(0, int(
            self.window.db.count_publish_tasks(self._media_filter_id or None)
        ))

    def _database_queue_page(self, limit: int, offset: int = 0):
        return list(self.window.db.list_publish_tasks(
            limit=limit,
            offset=offset,
            media_id=self._media_filter_id or None,
        ))

    def _render_queue_batch(self) -> None:
        added = 0
        while (
            self._pending_rows
            and len(self.items) < self._queue_render_goal
            and added < PUBLISH_QUEUE_RENDER_BATCH_SIZE
        ):
            row = self._pending_rows.popleft()
            task_id = int(row["id"] or 0)
            if task_id <= 0 or task_id in self.items:
                continue
            item = self._insert_queue_row(row)
            if task_id == self._restore_selected_task_id:
                self.tree.setCurrentItem(item)
            added += 1
        if self._pending_rows and len(self.items) < self._queue_render_goal:
            return
        self._queue_render_timer.stop()
        self._restore_selected_task_id = 0
        self.apply_filter()
        self._update_queue_load_button()
        self._sync_action_state()

    def _update_queue_load_button(self) -> None:
        remaining = max(0, self._queue_total - len(self.items))
        self.load_more_button.setVisible(remaining > 0)
        if remaining:
            self.load_more_button.setText(ui_format(
                "Load More Publish Tasks ({remaining} remaining)",
                remaining=remaining,
            ))
            self.load_more_button.setEnabled(not self._queue_render_timer.isActive())

    def _materialize_queue_history(self) -> None:
        if len(self.items) + len(self._pending_rows) >= self._queue_total:
            return
        offset = len(self.items) + len(self._pending_rows)
        self._pending_rows.extend(
            self._database_queue_page(self._queue_total - offset, offset)
        )

    def _prioritize_pending_queue_rows(self) -> None:
        query = self.search_box.text().strip().lower()
        if not query or not self._pending_rows:
            return
        matching = []
        remaining = []
        for row in self._pending_rows:
            (matching if query in publish_queue_search_text(row) else remaining).append(row)
        self._pending_rows = deque(matching + remaining)
        if matching:
            self._queue_render_goal = max(
                self._queue_render_goal,
                len(self.items) + min(PUBLISH_QUEUE_PAGE_SIZE, len(matching)),
            )
            self._queue_render_timer.start()

    def load_more_tasks(self) -> None:
        if self._queue_render_timer.isActive():
            return
        if not self._pending_rows:
            self._pending_rows.extend(self._database_queue_page(
                min(PUBLISH_QUEUE_PAGE_SIZE, self._queue_total - len(self.items)),
                len(self.items),
            ))
        if not self._pending_rows:
            self._update_queue_load_button()
            return
        self._queue_render_goal = len(self.items) + min(
            PUBLISH_QUEUE_PAGE_SIZE,
            self._queue_total - len(self.items),
        )
        self.load_more_button.setEnabled(False)
        self._queue_render_timer.start()

    def refresh_task(self, task_id: int, row=None) -> None:
        selected_task_id = int(task_id or 0)
        if selected_task_id <= 0:
            self.mark_dirty()
            return
        if row is None:
            row = self.window.db.get_publish_task(selected_task_id)
        if row is None:
            self.mark_dirty()
            return
        row_media_id = int(row["media_id"] or 0)
        if self._media_filter_id > 0 and row_media_id != self._media_filter_id:
            return
        if not self.loaded or not self.isVisible():
            self.dirty = True
            return
        item = self.items.get(selected_task_id)
        if item is None:
            item = self._insert_queue_row(row, index=0)
            self._queue_total = max(self._queue_total, len(self.items))
        self._update_item(item, row)
        self.dirty = False
        self.apply_filter()
        self._sync_action_state()

    @staticmethod
    def _update_item(item: QTreeWidgetItem, row) -> None:
        platform = str(row["platform"] or "")
        status = str(row["status"] or "")
        values = (
            str(row["id"]),
            platform_label(platform),
            row["account"] or ui_text("Default"),
            ui_text(PUBLISH_STATUS_TEXT.get(status, status)),
            row["title"],
            runtime_text(row["result"] or ""),
        )
        for column, value in enumerate(values):
            item.setText(column, str(value))
        item.setTextAlignment(0, Qt.AlignCenter)
        item.setData(0, Qt.UserRole, int(row["media_id"] or 0))
        item.setData(0, Qt.UserRole + 1, publish_queue_search_text(row))
        item.setData(0, Qt.UserRole + 2, status)

    def apply_filter(self) -> None:
        query = self.search_box.text().strip().lower()
        filter_key = f"{self._media_filter_id}\0{query}"
        if (
            query
            and filter_key != self._queue_filter_materialized_key
            and not self._queue_render_timer.isActive()
        ):
            self._queue_filter_materialized_key = filter_key
            self._materialize_queue_history()
            self._prioritize_pending_queue_rows()
        visible = 0
        for index in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(index)
            haystack = str(item.data(0, Qt.UserRole + 1) or "")
            outside_scope = (
                self._media_filter_id > 0
                and int(item.data(0, Qt.UserRole) or 0) != self._media_filter_id
            )
            matched = not outside_scope and not (query and query not in haystack)
            item.setHidden(not matched)
            visible += int(matched)
        if visible:
            self.queue_stack.setCurrentWidget(self.tree)
        else:
            self.queue_stack.setCurrentWidget(self.empty_state)
            if query:
                self.empty_state.setText(ui_text(
                    "No matching publish tasks\nTry another search term"
                ))
            elif self._media_filter_id > 0:
                self.empty_state.setText(ui_text(
                    "This video has no publish tasks yet\nCreate one from Completed"
                ))
            else:
                self.empty_state.setText(ui_text(
                    "No publish tasks yet\nSelect a video in Completed to create one"
                ))
        self._update_queue_load_button()
        self._sync_action_state()

    def _sync_action_state(self) -> None:
        item = self.tree.currentItem()
        status = str(item.data(0, Qt.UserRole + 2) or "") if item else ""
        visible_selection = bool(item is not None and not item.isHidden())
        self.run_button.setEnabled(visible_selection and status in {"pending", "failed"})
        self.retry_button.setEnabled(visible_selection and status == "failed")

    def run_selected(self) -> None:
        item = self.tree.currentItem()
        if item is not None and self.run_button.isEnabled():
            self.window.publish_service.run_task(int(item.text(0)))

    def retry_selected(self) -> None:
        item = self.tree.currentItem()
        if item is not None and self.retry_button.isEnabled():
            self.window.publish_service.retry_task(int(item.text(0)))

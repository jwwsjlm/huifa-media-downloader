from __future__ import annotations

import os
from collections import deque
from pathlib import Path

from PySide6.QtCore import QItemSelectionModel, QPoint, QSize, Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.storage.models import MediaItem
from app.ui.completed_media_card import CompletedMediaCard
from app.ui.distribution_plan import (
    distribution_platform_states,
    distribution_preselected_platforms,
    distribution_target_platforms,
)
from app.ui.i18n import format_text as ui_format
from app.ui.i18n import runtime_text
from app.ui.i18n import text as ui_text
from app.ui.media_presentation import PLATFORM_TEXT
from app.ui.metric_card import TaskMetricCard


MEDIA_INITIAL_PAGE_SIZE = 50
MEDIA_PAGE_SIZE = 50
MEDIA_RENDER_BATCH_SIZE = 8
MEDIA_SEARCH_DEBOUNCE_MS = 300

FILTER_ALL = "all"
FILTER_NEEDS_DISTRIBUTION = "needs_distribution"
FILTER_PUBLISHED = "published"
FILTER_QUEUED = "queued"
FILTER_RETRY_NEEDED = "retry_needed"
FILTER_COMPLETE = "complete"

MEDIA_FILTER_KEYS = (
    FILTER_ALL,
    FILTER_NEEDS_DISTRIBUTION,
    FILTER_PUBLISHED,
    FILTER_QUEUED,
    FILTER_RETRY_NEEDED,
    FILTER_COMPLETE,
)


class CompletedPage(QWidget):
    """Paged completed-media catalog and distribution dashboard."""

    def __init__(self, window) -> None:
        super().__init__()
        self.window = window
        self.items: dict[int, QListWidgetItem] = {}
        self.cards: dict[int, CompletedMediaCard] = {}
        self.loaded = False
        self.dirty = True
        self._pending_media: deque[MediaItem] = deque()
        self._media_catalog: dict[int, MediaItem] = {}
        self._media_summaries: dict[int, dict[str, str]] = {}
        self._media_platforms: tuple[str, ...] = ()
        self._media_render_goal = 0
        self._media_total = 0
        self._media_metric_counts: dict[str, int] = {}
        self._restore_selected_id = 0
        self._filter_materialized_key = ""

        self._media_render_timer = QTimer(self)
        self._media_render_timer.setInterval(0)
        self._media_render_timer.timeout.connect(self._render_media_batch)
        self._search_filter_timer = QTimer(self)
        self._search_filter_timer.setSingleShot(True)
        self._search_filter_timer.setInterval(MEDIA_SEARCH_DEBOUNCE_MS)
        self._search_filter_timer.timeout.connect(self.apply_filter)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)
        layout.addLayout(self._build_header())
        layout.addLayout(self._build_metrics())

        self.list = QListWidget()
        self.list.setObjectName("completedList")
        self.list.setSpacing(8)
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self.menu)
        self.list.itemSelectionChanged.connect(self.sync_selection)

        self.content_stack = QStackedWidget()
        self.content_stack.setObjectName("completedContentStack")
        self.content_stack.setMinimumHeight(240)
        self.content_stack.addWidget(self.list)
        self.empty_state = QLabel(ui_text(
            "No completed videos yet\nCompleted downloads will appear here for cover management and distribution"
        ))
        self.empty_state.setObjectName("emptyState")
        self.empty_state.setAlignment(Qt.AlignCenter)
        self.empty_state.setWordWrap(True)
        self.empty_state.setMinimumHeight(240)
        self.content_stack.addWidget(self.empty_state)

        self.load_more_button = QPushButton(ui_text("Load More Media"))
        self.load_more_button.clicked.connect(self.load_more_media)
        self.load_more_button.hide()
        layout.addWidget(self.load_more_button, 0, Qt.AlignHCenter)
        layout.addWidget(self.content_stack, 1)

    def _build_header(self) -> QHBoxLayout:
        header = QHBoxLayout()
        title = QLabel(ui_text("Media Library & Distribution"))
        title.setObjectName("pageTitle")
        header.addWidget(title)
        self.summary = QLabel(ui_text("Open this page to load your media library"))
        self.summary.setObjectName("mutedText")
        header.addWidget(self.summary)
        header.addStretch(1)

        self.filter_box = QComboBox()
        for key, translation_key in (
            (FILTER_ALL, "All"),
            (FILTER_NEEDS_DISTRIBUTION, "Needs Distribution"),
            (FILTER_PUBLISHED, "Published"),
            (FILTER_QUEUED, "Queued"),
            (FILTER_RETRY_NEEDED, "Retry Needed"),
            (FILTER_COMPLETE, "All Targets Complete"),
        ):
            self.filter_box.addItem(ui_text(translation_key, context="completed.filter"), key)
        self.filter_box.setMinimumWidth(110)
        self.filter_box.currentIndexChanged.connect(self.apply_filter)
        header.addWidget(self.filter_box)

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText(ui_text("Search title, author, path or platform"))
        self.search_box.setMaximumWidth(260)
        self.search_box.textChanged.connect(self._schedule_search_filter)
        header.addWidget(self.search_box)
        refresh = QPushButton(ui_text("Refresh"))
        refresh.clicked.connect(self.refresh)
        header.addWidget(refresh)
        return header

    def _build_metrics(self) -> QHBoxLayout:
        metrics_row = QHBoxLayout()
        metrics_row.setSpacing(8)
        self.metric_cards: dict[str, TaskMetricCard] = {}
        for caption, filter_name, tone in (
            (ui_text("All Media"), FILTER_ALL, "neutral"),
            (ui_text("Needs Distribution"), FILTER_NEEDS_DISTRIBUTION, "queued"),
            (ui_text("Published"), FILTER_PUBLISHED, "active"),
            (ui_text("In Queue"), FILTER_QUEUED, "paused"),
            (ui_text("Retry Needed"), FILTER_RETRY_NEEDED, "danger"),
            (ui_text("All Targets Complete", context="media.metric"), FILTER_COMPLETE, "success"),
        ):
            metric = TaskMetricCard(caption, filter_name, tone, self, subject="media")
            metric.activated.connect(self._activate_metric_filter)
            metrics_row.addWidget(metric, 1)
            self.metric_cards[filter_name] = metric
        return metrics_row

    def _schedule_search_filter(self, _value: str = "") -> None:
        self._search_filter_timer.start()

    def _load_distribution_counts(self) -> dict[str, int]:
        counts = self.window.db.media_distribution_counts(self._media_platforms)
        return {
            key: max(0, int(counts.get(key, 0)))
            for key in MEDIA_FILTER_KEYS
        }

    def ensure_loaded(self) -> None:
        if not self.loaded or self.dirty:
            self.refresh()

    def mark_dirty(self) -> None:
        self.dirty = True
        if self.isVisible():
            self.refresh()

    def refresh_media_distribution(self, media_id: int) -> None:
        """Refresh only the live card affected by a publish transition."""

        selected_media_id = int(media_id or 0)
        if selected_media_id <= 0:
            self.mark_dirty()
            return
        if not self.loaded or not self.isVisible():
            self.dirty = True
            return
        media = self._media_catalog.get(selected_media_id)
        if media is None:
            self.mark_dirty()
            return

        states = self.window.db.publish_statuses_for_media(selected_media_id)
        self._media_summaries[selected_media_id] = distribution_platform_states(
            states,
            self._media_platforms,
        )
        self._media_metric_counts = self._load_distribution_counts()
        self._filter_materialized_key = ""
        item = self.items.get(selected_media_id)
        if item is not None:
            old_card = self.cards.get(selected_media_id)
            if old_card is not None:
                self.list.removeItemWidget(item)
                old_card.deleteLater()
            card = self._create_media_card(media, self._media_summaries[selected_media_id])
            self.list.setItemWidget(item, card)
            self.cards[selected_media_id] = card
            item.setData(Qt.UserRole + 1, self._media_search_text(media))

        self.dirty = False
        self._update_media_metrics()
        self.apply_filter()
        self.sync_selection()
        if not self._media_render_timer.isActive():
            self._update_media_summary()

    def refresh(self) -> None:
        current_item = self.list.currentItem()
        selected_id = int(current_item.data(Qt.UserRole) or 0) if current_item else 0
        requested_count = max(MEDIA_INITIAL_PAGE_SIZE, len(self._media_catalog))
        self._search_filter_timer.stop()
        self._media_render_timer.stop()
        self.list.clear()
        self.items.clear()
        self.cards.clear()
        self._media_platforms = distribution_target_platforms(
            self.window.app_settings.get("publish_target_platforms")
        )
        self._media_total = max(0, int(self.window.db.count_media()))
        media_items = list(self.window.db.list_media(
            limit=min(requested_count, self._media_total),
            offset=0,
        ))
        media_ids = [
            int(media.id or 0)
            for media in media_items
            if int(media.id or 0) > 0
        ]
        self._media_summaries = {
            media_id: distribution_platform_states(states, self._media_platforms)
            for media_id, states in self.window.db.publish_statuses_for_media_ids(
                media_ids
            ).items()
        }
        self._media_metric_counts = self._load_distribution_counts()
        self._media_catalog = {
            int(media.id or 0): media
            for media in media_items
            if int(media.id or 0) > 0
        }
        self._pending_media = deque(media_items)
        self._media_render_goal = min(requested_count, self._media_total)
        self._restore_selected_id = selected_id
        self._filter_materialized_key = ""
        self.loaded = True
        self.dirty = False
        self._update_media_metrics()
        if self._pending_media:
            self.summary.setText(ui_format(
                "Loading media cards: 0 / {total}",
                total=self._media_render_goal,
            ))
            self._media_render_timer.start()
        else:
            self.summary.setText(ui_text("0 videos"))
            self.apply_filter()
            self._update_media_load_button()

    def _render_media_batch(self) -> None:
        added = 0
        while (
            self._pending_media
            and len(self.items) < self._media_render_goal
            and added < MEDIA_RENDER_BATCH_SIZE
        ):
            media = self._pending_media.popleft()
            media_id = int(media.id or 0)
            if media_id <= 0 or media_id in self.items:
                continue
            states = self._media_summaries.get(media_id, {})
            item = QListWidgetItem()
            item.setData(Qt.UserRole, media_id)
            item.setData(Qt.UserRole + 1, self._media_search_text(media))
            item.setSizeHint(QSize(0, 172))
            self.list.addItem(item)
            card = self._create_media_card(media, states)
            self.list.setItemWidget(item, card)
            self.items[media_id] = item
            self.cards[media_id] = card
            if media_id == self._restore_selected_id:
                item.setSelected(True)
            added += 1

        if self._pending_media and len(self.items) < self._media_render_goal:
            self.summary.setText(ui_format(
                "Loading media cards: {current} / {total}",
                current=len(self.items),
                total=self._media_render_goal,
            ))
            return
        self._media_render_timer.stop()
        self.apply_filter()
        self.sync_selection()
        self._update_media_summary()
        self._update_media_load_button()

    def _create_media_card(
        self,
        media: MediaItem,
        states: dict[str, str],
    ) -> CompletedMediaCard:
        card = CompletedMediaCard(media, states, self._media_platforms, self.list)
        card.selected_requested.connect(self.select_media)
        card.publish_requested.connect(self.open_publish)
        card.queue_requested.connect(self.open_publish_queue)
        card.open_requested.connect(self.open_folder)
        card.cover_requested.connect(self.show_cover_menu)
        return card

    def _update_media_summary(self) -> None:
        rendered = len(self.items)
        prefix = (
            ui_format("Showing {rendered}/{total}", rendered=rendered, total=self._media_total)
            if rendered < self._media_total
            else ui_format("{count} videos", count=self._media_total)
        )
        self.summary.setText(prefix)
        self._update_media_metrics()

    def _update_media_load_button(self) -> None:
        remaining = max(0, self._media_total - len(self.items))
        self.load_more_button.setVisible(remaining > 0)
        if remaining:
            self.load_more_button.setText(ui_format(
                "Load More Media ({remaining} remaining)",
                remaining=remaining,
            ))
            self.load_more_button.setEnabled(not self._media_render_timer.isActive())

    def _selected_filter_key(self) -> str:
        value = str(self.filter_box.currentData() or FILTER_ALL)
        return value if value in MEDIA_FILTER_KEYS else FILTER_ALL

    def load_more_media(self) -> None:
        if self._media_render_timer.isActive():
            return
        if not self._pending_media:
            self._fetch_more_media(MEDIA_PAGE_SIZE)
        if not self._pending_media:
            self._update_media_load_button()
            return
        self._prioritize_pending_media()
        self._media_render_goal = len(self.items) + min(
            MEDIA_PAGE_SIZE,
            self._media_total - len(self.items),
        )
        self.load_more_button.setEnabled(False)
        self._media_render_timer.start()

    def _fetch_more_media(self, limit: int | None = None) -> int:
        offset = len(self._media_catalog)
        remaining = max(0, self._media_total - offset)
        if remaining <= 0:
            return 0
        requested = remaining if limit is None else min(remaining, max(0, int(limit)))
        if requested <= 0:
            return 0
        media_items = list(self.window.db.list_media(limit=requested, offset=offset))
        ids = [int(media.id or 0) for media in media_items if int(media.id or 0) > 0]
        summaries = self.window.db.publish_statuses_for_media_ids(ids)
        for media_id, states in summaries.items():
            self._media_summaries[media_id] = distribution_platform_states(
                states,
                self._media_platforms,
            )
        added = 0
        for media in media_items:
            media_id = int(media.id or 0)
            if media_id <= 0 or media_id in self._media_catalog:
                continue
            self._media_catalog[media_id] = media
            self._pending_media.append(media)
            added += 1
        return added

    def apply_filter(self, _value=None) -> None:
        query = self.search_box.text().strip().lower()
        selected = self._selected_filter_key()
        filter_key = f"{selected}\0{query}"
        if (
            (query or selected != FILTER_ALL)
            and filter_key != self._filter_materialized_key
            and not self._media_render_timer.isActive()
        ):
            self._filter_materialized_key = filter_key
            if len(self._media_catalog) < self._media_total:
                self._fetch_more_media(None)
            self._prioritize_pending_media()

        visible = 0
        for index in range(self.list.count()):
            item = self.list.item(index)
            media_id = int(item.data(Qt.UserRole) or 0)
            query_match = not query or query in str(item.data(Qt.UserRole + 1) or "")
            matched = query_match and self._media_matches_filter(media_id, selected)
            item.setHidden(not matched)
            visible += int(matched)
        self._sync_media_metric_selection()
        self.content_stack.setCurrentWidget(self.list if visible else self.empty_state)
        if (query or selected != FILTER_ALL) and not visible:
            self.empty_state.setText(ui_text(
                "No matching media\nTry another search term or filter"
            ))
        elif not visible:
            self.empty_state.setText(ui_text(
                "No completed videos yet\nCompleted downloads will appear here for cover management and distribution"
            ))

    def _activate_metric_filter(self, filter_name: str) -> None:
        index = self.filter_box.findData(filter_name)
        if index < 0:
            return
        self.search_box.clear()
        self._search_filter_timer.stop()
        if self.filter_box.currentIndex() == index:
            self.apply_filter()
        else:
            self.filter_box.setCurrentIndex(index)

    def _sync_media_metric_selection(self) -> None:
        selected = self._selected_filter_key()
        for filter_name, metric in self.metric_cards.items():
            metric.set_active(filter_name == selected)

    def _distribution_flags(self, media_id: int) -> dict[str, bool]:
        states = self._media_summaries.get(media_id, {})
        success = {name for name, state in states.items() if state == "success"}
        active = any(state in {"pending", "uploading"} for state in states.values())
        failed = any(state == "failed" for state in states.values())
        complete = bool(self._media_platforms) and all(
            states.get(platform) == "success"
            for platform in self._media_platforms
        )
        return {
            FILTER_NEEDS_DISTRIBUTION: not complete,
            FILTER_PUBLISHED: bool(success),
            FILTER_QUEUED: active,
            FILTER_RETRY_NEEDED: failed,
            FILTER_COMPLETE: complete,
        }

    def _media_matches_filter(self, media_id: int, selected: str | None = None) -> bool:
        selected = selected or self._selected_filter_key()
        if selected == FILTER_ALL:
            return True
        return self._distribution_flags(media_id).get(selected, False)

    def _update_media_metrics(self) -> None:
        counts = self._media_metric_counts or {
            FILTER_ALL: self._media_total,
            FILTER_NEEDS_DISTRIBUTION: self._media_total,
            FILTER_PUBLISHED: 0,
            FILTER_QUEUED: 0,
            FILTER_RETRY_NEEDED: 0,
            FILTER_COMPLETE: 0,
        }
        for filter_name, value in counts.items():
            metric = self.metric_cards.get(filter_name)
            if metric is not None:
                metric.set_value(value)
        self._sync_media_metric_selection()

    def _media_search_text(self, media: MediaItem) -> str:
        states = self._media_summaries.get(int(media.id or 0), {})
        return " ".join([
            media.title,
            media.uploader,
            media.video_path,
            media.source_url,
            " ".join(PLATFORM_TEXT.get(name, name) for name in states),
        ]).lower()

    def _prioritize_pending_media(self) -> None:
        query = self.search_box.text().strip().lower()
        selected = self._selected_filter_key()
        if (not query and selected == FILTER_ALL) or not self._pending_media:
            return
        matching: list[MediaItem] = []
        remaining: list[MediaItem] = []
        for media in self._pending_media:
            query_match = not query or query in self._media_search_text(media)
            status_match = self._media_matches_filter(int(media.id or 0), selected)
            (matching if query_match and status_match else remaining).append(media)
        self._pending_media = deque(matching + remaining)
        if matching:
            self._media_render_goal = max(
                self._media_render_goal,
                len(self.items) + min(MEDIA_PAGE_SIZE, len(matching)),
            )
            self._media_render_timer.start()

    def select_media(self, media_id: int) -> None:
        item = self.items.get(media_id)
        if item is not None:
            self.list.setCurrentItem(item, QItemSelectionModel.ClearAndSelect)

    def sync_selection(self) -> None:
        selected = {
            int(item.data(Qt.UserRole) or 0)
            for item in self.list.selectedItems()
        }
        for media_id, card in list(self.cards.items()):
            try:
                card.set_selected(media_id in selected)
            except RuntimeError:
                self.cards.pop(media_id, None)

    def open_publish(self, media_id: int) -> None:
        media = self.window.db.get_media(media_id)
        if media:
            states = self._media_summaries.get(media_id, {})
            self.window.publish_ui.open_editor(
                media,
                distribution_preselected_platforms(states, self._media_platforms),
            )

    def open_publish_queue(self, media_id: int) -> None:
        media = self.window.db.get_media(media_id)
        if media:
            self.window.publish_ui.focus_queue(media)

    def open_folder(self, media_id: int) -> None:
        media = self.window.db.get_media(media_id)
        if not media:
            return
        if not str(media.video_path or "").strip():
            QMessageBox.warning(
                self,
                ui_text("Unable to Open Folder"),
                ui_text("Video path not recorded"),
            )
            return
        folder = Path(media.video_path).parent
        try:
            os.startfile(str(folder))
        except OSError as exc:
            QMessageBox.warning(
                self,
                ui_text("Unable to Open Folder"),
                ui_format(
                    "Unable to open media folder:\n{folder}\n\n{error}",
                    folder=folder,
                    error=runtime_text(exc),
                ),
            )

    def show_cover_menu(self, media_id: int) -> None:
        media = self.window.db.get_media(media_id)
        if not media or not media.thumbnail_path or not Path(media.thumbnail_path).is_file():
            QMessageBox.information(
                self,
                ui_text("No Cover Available"),
                ui_text("The current video has no local cover image."),
            )
            return
        menu = QMenu(self)
        copy_action = menu.addAction(ui_text("Copy Cover to Clipboard"))
        save_action = menu.addAction(ui_text("Save as JPG…"))
        menu.addSeparator()
        studio_action = menu.addAction(ui_text("Open Cover Studio…"))
        card = self.cards.get(media_id)
        anchor = getattr(card, "cover_button", None)
        menu_position = (
            anchor.mapToGlobal(QPoint(0, anchor.height()))
            if anchor is not None
            else self.mapToGlobal(QPoint(0, 0))
        )
        chosen = menu.exec(menu_position)
        if chosen is copy_action:
            self.copy_cover(media)
        elif chosen is save_action:
            self.window.cover_workflow.save_as_jpeg(media)
        elif chosen is studio_action:
            self.window.cover_workflow.open_studio(media)

    def copy_cover(self, media: MediaItem) -> None:
        clipboard_data = self.window.cover_workflow.copy_to_clipboard(media)
        if clipboard_data is None:
            return
        self.summary.setText(ui_format(
            "Copied a {width}×{height} cover using the default settings",
            width=clipboard_data.width,
            height=clipboard_data.height,
        ))

    def menu(self, pos) -> None:
        item = self.list.itemAt(pos)
        if not item:
            return
        menu = QMenu(self)
        media_id = int(item.data(Qt.UserRole) or 0)
        states = self._media_summaries.get(media_id, {})
        preselected = distribution_preselected_platforms(states, self._media_platforms)
        failed = [name for name, state in states.items() if state == "failed"]
        active = [name for name, state in states.items() if state in {"pending", "uploading"}]
        publish = menu.addAction(
            ui_format("Continue Distribution ({count})…", count=len(preselected))
            if preselected
            else ui_text("Create Publish Task…")
        )
        queue_action = None
        if failed or active:
            menu.addSeparator()
            queue_label = (
                ui_format("Handle Failed Tasks ({count})", count=len(failed))
                if failed
                else ui_text("View Publish Queue")
            )
            queue_action = menu.addAction(queue_label)
        menu.addSeparator()
        copy_cover = menu.addAction(ui_text("Copy Cover"))
        save_cover = menu.addAction(ui_text("Save Cover as JPG…"))
        cover_studio = menu.addAction(ui_text("Cover Studio…"))
        menu.addSeparator()
        open_folder = menu.addAction(ui_text("Open Folder"))
        copy_path = menu.addAction(ui_text("Copy Video Path"))
        action = menu.exec(self.list.mapToGlobal(pos))
        media = self.window.db.get_media(item.data(Qt.UserRole))
        if not media:
            return
        if action is publish:
            self.window.publish_ui.open_editor(media, preselected)
        elif queue_action is not None and action is queue_action:
            self.window.publish_ui.focus_queue(media)
        elif action is copy_cover:
            self.copy_cover(media)
        elif action is save_cover:
            self.window.cover_workflow.save_as_jpeg(media)
        elif action is cover_studio:
            self.window.cover_workflow.open_studio(media)
        elif action is open_folder:
            self.open_folder(int(media.id or 0))
        elif action is copy_path:
            QApplication.clipboard().setText(media.video_path)
            self.summary.setText(ui_text("Video path copied"))

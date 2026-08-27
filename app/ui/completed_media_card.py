from __future__ import annotations

from collections.abc import Mapping, Set
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from app.core.platforms import detect_platform
from app.storage.models import MediaItem
from app.ui.distribution_plan import distribution_platform_states
from app.ui.i18n import format_text as ui_format
from app.ui.i18n import text as ui_text
from app.ui.media_presentation import (
    PLATFORM_TEXT,
    platform_icon_pixmap,
    platform_label,
    thumbnail_pixmap,
)


@dataclass(frozen=True, slots=True)
class DistributionCardState:
    success: frozenset[str]
    active: frozenset[str]
    failed: frozenset[str]
    not_started: frozenset[str]
    total: int

    @property
    def remaining(self) -> int:
        return max(0, self.total - len(self.success))

    @property
    def has_queue_task(self) -> bool:
        return bool(self.active or self.failed)


def distribution_card_state(
    platform_states: Mapping[str, str],
    available_platforms: tuple[str, ...],
) -> DistributionCardState:
    normalized = distribution_platform_states(platform_states, available_platforms)
    success = frozenset(
        name for name, state in normalized.items() if state == "success"
    )
    active = frozenset(
        name
        for name, state in normalized.items()
        if state in {"pending", "uploading"}
    )
    failed = frozenset(
        name for name, state in normalized.items() if state == "failed"
    )
    not_started = frozenset(set(available_platforms) - set(normalized))
    return DistributionCardState(
        success=success,
        active=active,
        failed=failed,
        not_started=not_started,
        total=len(available_platforms),
    )


def resolved_media_platform(media: MediaItem) -> str:
    detected = detect_platform(media.source_url) if media.source_url else "generic"
    stored = str(media.source_platform or "").strip().casefold()
    if detected == "generic" and stored:
        return stored
    return detected or stored or "generic"


def joined_platform_labels(
    platforms: Set[str],
    available_platforms: tuple[str, ...],
) -> str:
    return "、".join(
        platform_label(name)
        for name in available_platforms
        if name in platforms
    )


class CompletedMediaCard(QFrame):
    """Presentation and actions for one completed media item."""

    selected_requested = Signal(int)
    publish_requested = Signal(int)
    queue_requested = Signal(int)
    open_requested = Signal(int)
    cover_requested = Signal(int)

    def __init__(
        self,
        media: MediaItem,
        platform_states: Mapping[str, str],
        available_platforms: tuple[str, ...],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.media = media
        self._title_text = media.title or ui_text("Untitled video")
        self._path_text = media.video_path or ui_text("Video path not recorded")
        self._thumbnail_exists = bool(
            media.thumbnail_path and Path(media.thumbnail_path).is_file()
        )
        self._video_exists = bool(media.video_path and Path(media.video_path).is_file())
        state = distribution_card_state(platform_states, available_platforms)
        self._has_uncreated_target = bool(state.not_started)
        self._has_queue_task = state.has_queue_task

        self._configure_card()
        root = self._build_root_layout()
        root.addWidget(self._build_thumbnail())
        root.addWidget(self._build_source_icon(), 0, Qt.AlignTop)
        root.addLayout(self._build_content(state, available_platforms), 1)
        root.addLayout(self._build_actions(state))

    def _configure_card(self) -> None:
        self.setObjectName("completedCard")
        self.setProperty("selected", False)
        self.setMinimumHeight(132)

    def _build_root_layout(self) -> QHBoxLayout:
        root = QHBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(12)
        return root

    def _build_thumbnail(self) -> QLabel:
        self.thumbnail = QLabel(ui_text("No cover"))
        self.thumbnail.setObjectName("completedThumbnail")
        self.thumbnail.setFixedSize(144, 86)
        self.thumbnail.setAlignment(Qt.AlignCenter)
        if self._thumbnail_exists:
            pixmap = thumbnail_pixmap(
                self.media.thumbnail_path,
                144,
                86,
                Qt.KeepAspectRatioByExpanding,
            )
            if not pixmap.isNull():
                self.thumbnail.setText("")
                self.thumbnail.setPixmap(pixmap)
        return self.thumbnail

    def _build_source_icon(self) -> QLabel:
        source = resolved_media_platform(self.media)
        self.source_icon = QLabel()
        self.source_icon.setFixedSize(30, 30)
        self.source_icon.setAlignment(Qt.AlignCenter)
        self.source_icon.setPixmap(platform_icon_pixmap(source))
        source_label = platform_label(source) if source in PLATFORM_TEXT else ui_text("Other Site")
        self.source_icon.setToolTip(ui_format(
            "Source platform: {platform}\n{url}",
            platform=source_label,
            url=self.media.source_url,
        ))
        return self.source_icon

    def _build_content(
        self,
        state: DistributionCardState,
        available_platforms: tuple[str, ...],
    ) -> QVBoxLayout:
        content = QVBoxLayout()
        content.setSpacing(4)
        self.title = QLabel(self._title_text)
        self.title.setObjectName("completedTitle")
        self.title.setToolTip(self._title_text)
        content.addWidget(self.title)

        publisher = self.media.uploader or ui_text("Unknown author")
        downloaded_at = self.media.downloaded_at or ui_text("Download time unknown")
        self.meta = QLabel(f"{publisher}  ·  {downloaded_at}")
        self.meta.setObjectName("mutedText")
        content.addWidget(self.meta)

        content.addWidget(self._build_distribution_label(state, available_platforms))
        content.addLayout(self._build_distribution_chips(state, available_platforms))
        content.addWidget(self._build_path_label())
        return content

    def _build_distribution_label(
        self,
        state: DistributionCardState,
        available_platforms: tuple[str, ...],
    ) -> QLabel:
        self.distribution = QLabel(ui_format(
            "Platforms {completed}/{total}  ·  {remaining} remaining",
            completed=len(state.success),
            total=state.total,
            remaining=state.remaining,
        ))
        self.distribution.setObjectName("distributionStatus")
        self.distribution.setProperty(
            "state",
            "complete" if not state.remaining else "pending",
        )
        detail_lines: list[str] = []
        for platforms, label in (
            (state.success, "Published: "),
            (state.active, "Queued: "),
            (state.failed, "Failed: "),
            (state.not_started, "Not created: "),
        ):
            if platforms:
                detail_lines.append(
                    ui_text(label)
                    + joined_platform_labels(platforms, available_platforms)
                )
        missing = [
            platform_label(name)
            for name in available_platforms
            if name not in state.success
        ]
        if missing:
            detail_lines.append(ui_text("Not successfully distributed: ") + "、".join(missing))
        self.distribution.setToolTip("\n".join(detail_lines) or ui_text("No publish task created"))
        return self.distribution

    def _build_distribution_chips(
        self,
        state: DistributionCardState,
        available_platforms: tuple[str, ...],
    ) -> QHBoxLayout:
        chips = QHBoxLayout()
        chips.setContentsMargins(0, 0, 0, 0)
        chips.setSpacing(6)
        self._add_distribution_chip(
            chips,
            "success",
            ui_format("✓ Published {count}", count=len(state.success)),
            state.success,
            available_platforms,
        )
        self._add_distribution_chip(
            chips,
            "active",
            ui_format("◷ Queued {count}", count=len(state.active)),
            state.active,
            available_platforms,
        )
        self._add_distribution_chip(
            chips,
            "failed",
            ui_format("! Retry {count}", count=len(state.failed)),
            state.failed,
            available_platforms,
        )
        self._add_distribution_chip(
            chips,
            "notStarted",
            ui_format("○ Not created {count}", count=len(state.not_started)),
            state.not_started,
            available_platforms,
        )
        chips.addStretch(1)
        return chips

    def _build_path_label(self) -> QLabel:
        self.path = QLabel(self._path_text)
        self.path.setObjectName("completedPath")
        state_text = (
            ui_text("Available")
            if self._video_exists
            else ui_text("File missing", context="media.file_status")
        )
        self.path.setToolTip(
            ui_text("Video path: ")
            + f"{self._path_text}\n"
            + ui_text("File status: ")
            + state_text
        )
        self.path.setProperty("missing", not self._video_exists)
        return self.path

    def _build_actions(self, state: DistributionCardState) -> QVBoxLayout:
        actions = QVBoxLayout()
        actions.setSpacing(6)
        media_id = int(self.media.id or 0)
        if state.not_started:
            publish = QPushButton(
                ui_format("Continue ({count})", count=len(state.not_started)),
            )
            publish.setToolTip(ui_text("Open the publish editor with only uncreated targets selected"))
            publish.clicked.connect(lambda: self.publish_requested.emit(media_id))
        elif state.failed:
            publish = QPushButton(
                ui_format("Handle Failed ({count})", count=len(state.failed)),
            )
            publish.setToolTip(ui_text("Open this video's publish queue to inspect and retry failed tasks"))
            publish.clicked.connect(lambda: self.queue_requested.emit(media_id))
        elif state.active:
            publish = QPushButton(ui_text("View Publish Queue"))
            publish.setToolTip(ui_text("Open this video's publish queue and view active or waiting tasks"))
            publish.clicked.connect(lambda: self.queue_requested.emit(media_id))
        else:
            publish = QPushButton(ui_text("All Targets Complete"))
            publish.setEnabled(False)
            publish.setToolTip(ui_text(
                "All targets were published successfully; use the context menu to create another task"
            ))
        publish.setObjectName("primaryButton")
        self.publish_button = publish
        cover = QPushButton(ui_text("Cover Tools"))
        cover.setEnabled(self._thumbnail_exists)
        cover.clicked.connect(lambda: self.cover_requested.emit(media_id))
        self.cover_button = cover
        open_folder = QPushButton(ui_text("Open Folder"))
        open_folder.clicked.connect(lambda: self.open_requested.emit(media_id))
        actions.addWidget(publish)
        if state.failed and state.not_started:
            retry_failed = QPushButton(
                ui_format("Handle Failed ({count})", count=len(state.failed)),
            )
            retry_failed.setToolTip(ui_text("Open this video's failed publish tasks to inspect and retry"))
            retry_failed.clicked.connect(lambda: self.queue_requested.emit(media_id))
            actions.addWidget(retry_failed)
        actions.addWidget(cover)
        actions.addWidget(open_folder)
        actions.addStretch(1)
        return actions

    def _add_distribution_chip(
        self,
        layout: QHBoxLayout,
        state: str,
        text: str,
        platforms: Set[str],
        available_platforms: tuple[str, ...],
    ) -> None:
        chip = QLabel(text)
        chip.setObjectName("distributionChip")
        chip.setProperty("state", state)
        names = [platform_label(name) for name in available_platforms if name in platforms]
        label = {
            "success": ui_text("Published successfully"),
            "active": ui_text("Created and queued"),
            "failed": ui_text("Failed; retry in Publish Queue"),
            "notStarted": ui_text("No publish task created"),
        }[state]
        chip.setToolTip(f"{label}: " + (", ".join(names) if names else ui_text("None")))
        layout.addWidget(chip)

    def set_selected(self, selected: bool) -> None:
        if self.property("selected") == selected:
            return
        self.setProperty("selected", selected)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self.media.id is not None:
            self.selected_requested.emit(int(self.media.id))
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self.media.id is not None:
            if self._has_uncreated_target:
                self.publish_requested.emit(int(self.media.id))
            elif self._has_queue_task:
                self.queue_requested.emit(int(self.media.id))
        super().mouseDoubleClickEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        width = max(120, self.width() - 390)
        metrics = QFontMetrics(self.title.font())
        self.title.setText(metrics.elidedText(self._title_text, Qt.ElideRight, width))
        self.path.setText(metrics.elidedText(self._path_text, Qt.ElideMiddle, width))

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QEvent, Qt, Signal, QTimer
from PySide6.QtGui import QFontMetrics, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.core.download_options import DownloadOptions
from app.core.download_service import DownloadTask, format_duration
from app.core.log_service import DownloadLogService
from app.core.platforms import detect_platform
from app.storage.models import MediaItem
from app.ui.i18n import (
    format_text as ui_format,
    runtime_text,
    text as ui_text,
)
from app.ui.media_presentation import (
    PLATFORM_TEXT,
    compact_path_display,
    error_category_text,
    platform_icon_pixmap,
    platform_label,
    thumbnail_pixmap,
)


STATUS_TEXT = {
    "queued": "Queued",
    "downloading": "Downloading",
    "processing": "Processing",
    "canceling": "Canceling",
    "暂停中": "Pausing",
    "paused": "Paused",
    "waiting_selection": "Select format",
    "deleted": "File missing",
    "completed": "Completed",
    "failed": "Failed",
    "canceled": "Canceled",
    "parsing_collection": "Parsing collection",
    "partial_failed": "Partially failed",
}


STAGE_TEXT = {
    "queued": "Waiting to start",
    "parsing": "Parsing video information",
    "formats": "Fetching available formats",
    "waiting_selection": "Waiting for format selection",
    "waiting_disk": "Checking and reserving disk space",
    "downloading": "Downloading video and audio",
    "downloading_video": "Downloading video",
    "downloading_audio": "Downloading audio",
    "merging": "Merging video and audio",
    "transcoding": "Converting video format",
    "thumbnail": "Downloading thumbnail",
    "metadata": "Writing metadata",
    "verifying": "Verifying media file",
    "reconnecting": "Network interrupted, retrying",
    "completed": "Download complete",
    "failed": "Download failed",
    "paused": "Paused",
    "canceled": "Canceled",
    "parsing_collection": "Parsing collection",
    "partial_failed": "Some collection items failed",
}


PIPELINE_STAGES = (
    ("parsing", "Parse"),
    ("formats", "Format"),
    ("waiting_disk", "Disk"),
    ("downloading", "Download"),
    ("merging", "Merge"),
    ("transcoding", "Convert"),
    ("thumbnail", "Cover"),
    ("metadata", "Metadata"),
    ("verifying", "Verify"),
    ("completed", "Done"),
)


# Active stages without a trustworthy percentage use a busy indicator.


INDETERMINATE_TASK_STAGES = frozenset({
    "parsing",
    "formats",
    "waiting_disk",
    "merging",
    "thumbnail",
    "metadata",
    "verifying",
    "parsing_collection",
})


TRANSFER_TASK_STAGES = frozenset({"downloading", "downloading_video", "downloading_audio"})


ACTIVITY_TIMER_STAGES = INDETERMINATE_TASK_STAGES | TRANSFER_TASK_STAGES | frozenset({
    "transcoding",
    "reconnecting",
})


TERMINAL_TASK_STATUSES = frozenset({
    "completed", "failed", "partial_failed", "canceled", "deleted",
})


# A stable row height prevents relayout jumps as progress fields appear.


TASK_CARD_HEIGHT = 150


class DownloadTaskCard(QFrame):
    cancel_requested = Signal(str)
    pause_requested = Signal(str)
    resume_requested = Signal(str)
    retry_requested = Signal(str)
    open_requested = Signal(str)
    context_requested = Signal(str, object)
    selection_requested = Signal(str, object)

    def __init__(self, task: DownloadTask):
        super().__init__()
        self._initialize_runtime_state(task)
        self._configure_card_shell()
        self.thumbnail_wrap = self._build_thumbnail_panel()
        self._build_text_widgets()
        self.action = self._build_action_button()
        self._install_pointer_handlers()
        self._compose_card_layout()
        self.update_task(task)

    def _initialize_runtime_state(self, task: DownloadTask) -> None:
        self.task_id = task.id
        self._thumbnail_loaded_path = ""
        # Progress signals can arrive several times per second.  Avoid doing
        # a filesystem stat and an image decode decision on every signal when
        # the thumbnail path has not changed.  A short retry window still
        # handles the small race where the worker has persisted the path just
        # before the image file is flushed to disk.
        self._thumbnail_checked_path = ""
        self._thumbnail_exists = False
        self._thumbnail_last_probe_at = 0.0
        self._stage_color = ""
        self._status_color = ""
        self._title_text = ""
        self._url_text = ""
        self._url_tooltip_text = ""
        self._platform_source_url: str | None = None
        self._status = task.status
        self._task = task
        self._tracked_stage = ""
        self._stage_activity_started_at = time.monotonic()
        self._stage_activity_baseline = 0.0
        self._activity_timer = QTimer(self)
        self._activity_timer.setInterval(1000)
        self._activity_timer.timeout.connect(self._refresh_processing_activity)

    def _configure_card_shell(self) -> None:
        self.setObjectName("taskCard")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(TASK_CARD_HEIGHT)

    def _build_thumbnail_panel(self) -> QWidget:
        self.thumbnail = QLabel(ui_text('Video'))
        self.thumbnail.setFixedSize(116, 68)
        self.thumbnail.setAlignment(Qt.AlignCenter)
        self.thumbnail.setObjectName("taskThumbnail")
        self.platform_icon = QLabel()
        self.platform_icon.setFixedSize(28, 28)
        self.platform_icon.setAlignment(Qt.AlignCenter)
        self.platform_icon.setObjectName("platformIcon")
        self.platform_icon.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        thumbnail_wrap = QWidget()
        thumbnail_wrap.setFixedSize(116, 68)
        thumbnail_layout = QGridLayout(thumbnail_wrap)
        thumbnail_layout.setContentsMargins(0, 0, 0, 0)
        thumbnail_layout.setSpacing(0)
        thumbnail_layout.addWidget(self.thumbnail, 0, 0)
        thumbnail_layout.addWidget(self.platform_icon, 0, 0, Qt.AlignLeft | Qt.AlignBottom)
        return thumbnail_wrap

    def _build_text_widgets(self) -> None:
        self.title = QLabel()
        self.title.setObjectName("taskTitle")
        self.title.setWordWrap(False)
        self.title.setMinimumWidth(0)
        self.title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.url = QLabel()
        self.url.setObjectName("mutedText")
        self.url.setMinimumWidth(0)
        self.url.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.url.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.status = QLabel()
        self.status.setObjectName("taskStatus")
        self.status.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        self.stage = QLabel()
        self.stage.setObjectName("taskStage")
        self.stage.setMinimumWidth(0)
        self.stage.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        self.quality_badge = QLabel()
        self.quality_badge.setObjectName("taskQuality")
        self.quality_badge.setMinimumWidth(0)
        self.quality_badge.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.pipeline = QLabel()
        self.pipeline.setObjectName("taskPipeline")
        self.pipeline.setMinimumWidth(0)
        self.pipeline.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.pipeline.setTextFormat(Qt.RichText)
        self.pipeline.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(True)
        self.progress.setFixedHeight(18)
        self.details = QLabel()
        self.details.setObjectName("mutedText")
        self.details.setWordWrap(False)
        self.details.setMinimumWidth(0)
        self.details.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.details.setFixedHeight(18)

    def _build_action_button(self) -> QPushButton:
        action = QPushButton()
        # Keep the longest action label ("打开文件夹") fully visible instead
        # of letting Qt elide/compress it on completed cards.
        action.setFixedWidth(104)
        action.clicked.connect(self._action_clicked)
        return action

    def _install_pointer_handlers(self) -> None:
        for widget in (
            self,
            self.platform_icon,
            self.thumbnail,
            self.title,
            self.url,
            self.status,
            self.pipeline,
            self.progress,
            self.details,
        ):
            widget.setContextMenuPolicy(Qt.CustomContextMenu)
            widget.customContextMenuRequested.connect(
                lambda pos, source=widget: self.context_requested.emit(self.task_id, source.mapToGlobal(pos))
            )
            widget.installEventFilter(self)
    def _compose_card_layout(self) -> None:
        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(4)
        text_layout.addWidget(self.title)
        text_layout.addWidget(self.url)
        stage_layout = QHBoxLayout()
        stage_layout.setContentsMargins(0, 0, 0, 0)
        stage_layout.setSpacing(8)
        stage_layout.addWidget(self.stage)
        stage_layout.addWidget(self.quality_badge, 1)
        text_layout.addLayout(stage_layout)
        text_layout.addWidget(self.pipeline)
        text_layout.addWidget(self.progress)
        text_layout.addWidget(self.details)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(12)
        layout.addWidget(self.thumbnail_wrap)
        layout.addLayout(text_layout, 1)
        layout.addWidget(self.status)
        layout.addWidget(self.action)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            self.selection_requested.emit(self.task_id, event.modifiers())
        return super().eventFilter(watched, event)

    def set_selected(self, selected: bool) -> None:
        try:
            self.setProperty("selected", selected)
            self.style().unpolish(self)
            self.style().polish(self)
        except RuntimeError:
            # Qt may emit itemSelectionChanged while QListWidget is tearing
            # down an item widget; selection cleanup must not crash the app.
            return

    def _action_clicked(self) -> None:
        status = self._status
        if status == "downloading":
            self.pause_requested.emit(self.task_id)
        elif status == "paused":
            self.resume_requested.emit(self.task_id)
        elif status in {"queued", "processing", "canceling", "暂停中", "waiting_selection", "parsing_collection"}:
            self.cancel_requested.emit(self.task_id)
        elif status in {"failed", "partial_failed", "canceled", "deleted"}:
            self.retry_requested.emit(self.task_id)
        elif status == "completed":
            self.open_requested.emit(self.task_id)

    def _set_identity_text(
        self,
        title: str,
        url: str,
        *,
        uploader: str = "",
    ) -> None:
        """Update the full identity cache used by resize-time elision."""

        self._title_text = str(title or "")
        self._url_text = str(url or "")
        uploader = str(uploader or "")
        self._url_tooltip_text = (
            ui_format(
                '{url}\nUploader: {uploader}',
                url=self._url_text,
                uploader=uploader,
            )
            if uploader
            else self._url_text
        )
        self._refresh_elided_text()

    def update_media(self, media: MediaItem) -> None:
        thumbnail_path = str(media.thumbnail_path or "")
        if thumbnail_path and Path(thumbnail_path).is_file():
            pixmap = thumbnail_pixmap(thumbnail_path, 116, 68)
            if not pixmap.isNull():
                self.thumbnail.setText("")
                self.thumbnail.setPixmap(pixmap)
                self._thumbnail_loaded_path = thumbnail_path
                self._thumbnail_checked_path = thumbnail_path
                self._thumbnail_exists = True
                self._thumbnail_last_probe_at = time.monotonic()
        # Keep the source URL visible after metadata completion; uploader is
        # useful metadata but must not replace the actionable/copyable link.
        self._set_identity_text(
            media.title or self._title_text,
            media.source_url or self._url_text,
            uploader=media.uploader,
        )
        self._render_platform_identity(self._url_text)

    def _thumbnail_file_available(self, path: str) -> bool:
        """Cache thumbnail existence checks during high-frequency progress updates."""
        normalized = str(path or "")
        now = time.monotonic()
        # Re-probe a missing file at most twice per second so a just-written
        # thumbnail becomes visible without turning every progress callback
        # into a filesystem round trip.
        should_probe = (
            normalized != self._thumbnail_checked_path
            or (not self._thumbnail_exists and now - self._thumbnail_last_probe_at >= 0.5)
        )
        if should_probe:
            self._thumbnail_checked_path = normalized
            self._thumbnail_last_probe_at = now
            self._thumbnail_exists = bool(normalized and Path(normalized).is_file())
        return self._thumbnail_exists

    @staticmethod
    def _set_label_color(label: QLabel, color: str, previous: str) -> str:
        """Avoid reparsing the stylesheet when a task update keeps its state."""
        if color != previous:
            label.setStyleSheet(f"color: {color}; font-weight: 600;")
            return color
        return previous

    @staticmethod
    def _safe_non_negative_float(value: object) -> float:
        """Coerce persisted/extractor counters without propagating NaN or infinity."""

        try:
            number = float(value or 0.0)
        except (TypeError, ValueError, OverflowError):
            return 0.0
        return max(0.0, number) if math.isfinite(number) else 0.0

    @classmethod
    def _safe_non_negative_int(cls, value: object) -> int:
        return int(cls._safe_non_negative_float(value))

    def _live_stage_elapsed(self, task: DownloadTask) -> float:
        now = time.monotonic()
        reported = self._safe_non_negative_float(task.stage_elapsed_seconds)
        if task.stage != self._tracked_stage:
            self._tracked_stage = task.stage
            self._stage_activity_started_at = now
            self._stage_activity_baseline = reported
        else:
            estimated = self._stage_activity_baseline + max(
                0.0, now - self._stage_activity_started_at
            )
            if reported > estimated:
                self._stage_activity_started_at = now
                self._stage_activity_baseline = reported
        return max(
            reported,
            self._stage_activity_baseline
            + max(0.0, now - self._stage_activity_started_at),
        )

    def _refresh_processing_activity(self) -> None:
        task = self._task
        if self._task_has_live_activity(task):
            self.update_task(task)
        else:
            self._activity_timer.stop()

    @staticmethod
    def _task_has_live_activity(task: DownloadTask) -> bool:
        return (
            task.status not in TERMINAL_TASK_STATUSES
            and task.status not in {"queued", "paused", "waiting_selection"}
            and (
                task.stage in ACTIVITY_TIMER_STAGES
                or task.status in {"canceling", "暂停中"}
            )
        )

    def _sync_activity_timer(self, task: DownloadTask) -> None:
        if self._task_has_live_activity(task):
            if not self._activity_timer.isActive():
                self._activity_timer.start()
        else:
            self._activity_timer.stop()

    def _render_task_identity(self, task: DownloadTask) -> None:
        task_title = str(task.title or task.url or "")
        if task_title == "等待获取视频信息":
            task_title = ui_text('Waiting for video information')
        task_url = str(task.url or "")
        self._set_identity_text(task_title, task_url)
        self._render_platform_identity(task_url)

    def _render_platform_identity(self, source_url: str) -> None:
        if source_url == self._platform_source_url:
            return
        self._platform_source_url = source_url
        platform = detect_platform(source_url)
        platform_name = (
            platform_label(platform)
            if platform in PLATFORM_TEXT
            else ui_text('Other Site')
        )
        self.platform_icon.setPixmap(platform_icon_pixmap(platform))
        self.platform_icon.setToolTip(ui_format(
            'Source platform: {platform}\n{url}',
            platform=platform_name,
            url=source_url,
        ))

    def _render_task_thumbnail(self, task: DownloadTask) -> None:
        """Load a changed persisted thumbnail while preserving the fast path."""

        thumbnail_path = str(task.thumbnail_path or "")
        thumbnail_available = self._thumbnail_file_available(thumbnail_path)
        if thumbnail_available and thumbnail_path != self._thumbnail_loaded_path:
            pixmap = thumbnail_pixmap(thumbnail_path, 116, 68)
            if not pixmap.isNull():
                self.thumbnail.setText("")
                self.thumbnail.setPixmap(pixmap)
                self._thumbnail_loaded_path = thumbnail_path
        elif not thumbnail_available:
            self.thumbnail.setPixmap(QPixmap())
            self.thumbnail.setText(
                ui_text('Collection') if task.task_kind == 'collection' else ui_text('Video')
            )
            self._thumbnail_loaded_path = ""

    def _build_stage_text(self, task: DownloadTask) -> str:
        stage_text = str(task.stage_text or STAGE_TEXT.get(task.stage, task.stage) or "")
        if task.status == "canceling":
            stage_text = ui_text('Canceling')
        elif task.status == "暂停中":
            stage_text = ui_text('Pausing')
        elif task.stage in STAGE_TEXT:
            stage_text = ui_text(STAGE_TEXT[task.stage])

        if task.stage == "reconnecting":
            retry_text = ui_format(
                'Attempt {current}/{total}',
                current=self._safe_non_negative_int(task.retry_count),
                total=self._safe_non_negative_int(task.retry_total),
            )
            stage_text = f"{stage_text} · {retry_text}"
            if task.reconnect_message:
                stage_text += f" · {runtime_text(task.reconnect_message)}"
        if task.status == "deleted":
            stage_text = ui_text(
                'The media file is missing. You can download it again.',
            )

        options = task.options_json if isinstance(task.options_json, Mapping) else {}
        collection = options.get('_collection', {})
        if task.task_kind == 'collection' and isinstance(collection, Mapping):
            stage_text += ui_format(
                ' · {parsed} parsed · {selected} selected · {completed} completed · {failed} failed · {queued} queued · {skipped} skipped',
                parsed=self._safe_non_negative_int(collection.get('parsed')),
                selected=self._safe_non_negative_int(collection.get('selected')),
                completed=self._safe_non_negative_int(collection.get('completed')),
                failed=self._safe_non_negative_int(collection.get('failed')),
                queued=self._safe_non_negative_int(collection.get('queued')),
                skipped=self._safe_non_negative_int(collection.get('skipped')),
            )
        return stage_text

    def _render_format_badge(self, task: DownloadTask) -> None:
        options = task.options_json if isinstance(task.options_json, Mapping) else {}
        task_options = DownloadOptions.from_mapping(options)
        selected_quality = str(task.selected_quality or "").strip()
        if task_options.content_mode == 'audio':
            content_label = ui_text('Audio')
            output_label = (
                ui_text('Best available audio')
                if task_options.audio_format == 'best'
                else task_options.audio_format.upper()
            )
        elif task_options.content_mode == 'manual':
            content_label = ui_text('Manual')
            output_label = ui_text('Choose after parsing')
        else:
            content_label = ui_text('Video')
            effective_container = task_options.effective_container()
            output_label = (
                ui_text('Automatic')
                if effective_container == 'auto'
                else effective_container.upper()
            )

        task_format_text = f"{content_label} · {output_label}"
        quality_tooltip = ''
        if selected_quality:
            quality_text = ui_format(
                'Current quality: {quality}',
                quality=selected_quality,
            )
            self.quality_badge.setText(f"{quality_text} · {task_format_text}")
            quality_tooltip = ui_format(
                'Video format actually selected by yt-dlp: {quality}',
                quality=selected_quality,
            )
        else:
            self.quality_badge.setText(task_format_text)

        options_tooltip = ui_format(
            'Task options: {content} · {format}',
            content=content_label,
            format=output_label,
        )
        if task_options.content_mode == 'video':
            codec_labels = {
                'auto': ui_text('Automatic codec'),
                'h264': 'H.264 / AVC',
                'h265': 'H.265 / HEVC',
                'av1': 'AV1',
                'vp9': 'VP9',
            }
            fps_label = (
                ui_text('Highest available frame rate')
                if task_options.video_fps == 'best'
                else f'{task_options.video_fps} FPS'
            )
            effective_codec = task_options.effective_video_codec()
            codec_label = codec_labels.get(effective_codec, effective_codec)
            target_label = {
                'auto': ui_text('Automatic compatibility'),
                'windows': 'Windows',
                'macos': 'macOS',
                'linux': 'Linux',
                'ios': 'iOS',
                'android': 'Android',
            }.get(task_options.compatibility_target, task_options.compatibility_target)
            options_tooltip += '\n' + ui_format(
                'Frame rate: {fps} · Source codec: {codec} · Playback target: {target}',
                fps=fps_label,
                codec=codec_label,
                target=target_label,
            )
        self.quality_badge.setToolTip(
            f"{quality_tooltip}\n{options_tooltip}" if quality_tooltip else options_tooltip
        )
        self.quality_badge.show()

    @staticmethod
    def _task_stage_color(task: DownloadTask) -> str:
        if task.stage in {"merging", "transcoding", "thumbnail", "metadata", "verifying"}:
            return "#7b5bc7"
        if task.stage == "completed":
            return "#20a35a"
        if task.stage in {"failed", "partial_failed"} or task.status == "partial_failed":
            return "#d64444"
        if task.status == "deleted" or task.stage in {"paused", "reconnecting", "waiting_disk"}:
            return "#d48716"
        if task.stage == "canceled":
            return "#8b96a6"
        return "#2f7bdc"

    @staticmethod
    def _normalized_pipeline_stage(task: DownloadTask) -> str:
        stage_code = {
            "completed": "completed",
            "deleted": "completed",
            "failed": "failed",
            "canceled": "canceled",
            "paused": "paused",
        }.get(task.status, task.stage)
        if stage_code in {"downloading_video", "downloading_audio", "reconnecting"}:
            return "downloading"
        if stage_code == "waiting_selection":
            return "formats"
        return stage_code

    def _render_stage(self, task: DownloadTask) -> str:
        stage_text = self._build_stage_text(task)
        self.stage.setText(stage_text)
        self.stage.setToolTip(stage_text)
        stage_color = self._task_stage_color(task)
        self._stage_color = self._set_label_color(self.stage, stage_color, self._stage_color)
        stage_code = self._normalized_pipeline_stage(task)
        current_index = next(
            (index for index, (code, _label) in enumerate(PIPELINE_STAGES) if code == stage_code),
            0,
        )
        self.pipeline.setText(self._pipeline_html(task, current_index, stage_code))
        self.pipeline.setToolTip(ui_text(
            'Pipeline: Parse → Format → Disk → Download → Merge → Convert → Cover → Metadata → Verify → Done',
        ))
        return stage_text

    def _visible_progress_values(self, task: DownloadTask) -> tuple[float, int]:
        return (
            max(
                self._safe_non_negative_float(task.progress),
                self._safe_non_negative_float(task.visible_progress),
            ),
            max(
                self._safe_non_negative_int(task.total_bytes),
                self._safe_non_negative_int(task.visible_total_bytes),
            ),
        )

    def _progress_is_indeterminate(
        self,
        task: DownloadTask,
        visible_progress: float,
        visible_total: int,
    ) -> bool:
        active_status = (
            task.status not in TERMINAL_TASK_STATUSES
            and task.status not in {"queued", "paused", "waiting_selection"}
        )
        stage_progress = self._safe_non_negative_float(task.stage_progress)
        return active_status and (
            task.status in {"canceling", "暂停中"}
            or task.stage in INDETERMINATE_TASK_STAGES
            or (task.stage == "transcoding" and stage_progress <= 0)
            or (
                task.stage in {"downloading_video", "downloading_audio"}
                and stage_progress <= 0
            )
            or (
                task.stage == "downloading"
                and visible_progress <= 0
                and visible_total <= 0
            )
            or (task.stage == "reconnecting" and visible_progress <= 0)
        )

    def _render_progress(self, task: DownloadTask) -> tuple[float, int]:
        visible_progress, visible_total = self._visible_progress_values(task)
        indeterminate = self._progress_is_indeterminate(task, visible_progress, visible_total)
        self.progress.setRange(0, 0 if indeterminate else 100)

        if not indeterminate:
            value = visible_progress
            if task.stage in {"transcoding", "downloading_video", "downloading_audio"}:
                value = self._safe_non_negative_float(task.stage_progress)
            self.progress.setValue(max(0, min(100, int(value))))
        if task.status == "completed" or task.stage == "completed":
            self.progress.setRange(0, 100)
            self.progress.setValue(100)
        elif task.stage == "reconnecting" and not indeterminate:
            self.progress.setValue(max(1, min(99, int(visible_progress or 1))))
        self.progress.setFormat("%p%")
        return visible_progress, visible_total

    @staticmethod
    def _task_status_is_processing(task: DownloadTask) -> bool:
        return (
            task.status in {"downloading", "processing"}
            and task.stage in {"merging", "transcoding", "thumbnail", "metadata", "verifying"}
        )

    @classmethod
    def _task_status_color(cls, task: DownloadTask) -> str:
        if cls._task_status_is_processing(task):
            return "#7b5bc7"
        return {
            "downloading": "#2f7bdc",
            "processing": "#7b5bc7",
            "canceling": "#d48716",
            "queued": "#8b96a6",
            "paused": "#d48716",
            "暂停中": "#d48716",
            "completed": "#20a35a",
            "failed": "#d64444",
            "partial_failed": "#d64444",
            "canceled": "#8b96a6",
            "deleted": "#d48716",
        }.get(task.status, "#2f7bdc")

    def _render_status(self, task: DownloadTask) -> None:
        display_status = (
            ui_text('Processing')
            if self._task_status_is_processing(task)
            else ui_text(STATUS_TEXT.get(task.status, task.status))
        )
        self.status.setText(display_status)
        self._status_color = self._set_label_color(
            self.status,
            self._task_status_color(task),
            self._status_color,
        )
        if task.status == "failed" and task.error:
            error = str(task.error)
            diagnosis = DownloadLogService.classify_error(error)
            self.status.setToolTip(
                ui_text('Diagnosis: ')
                + error_category_text(diagnosis)
                + f"\n{runtime_text(error)}"
            )
        else:
            self.status.setToolTip("")

    @classmethod
    def _format_bytes(cls, value: object) -> str:
        units = ("B", "KiB", "MiB", "GiB", "TiB")
        number = cls._safe_non_negative_float(value)
        unit = units[0]
        for unit in units:
            if number < 1024 or unit == units[-1]:
                break
            number /= 1024
        return f"{number:.1f} {unit}"

    def _stream_progress_detail(self, task: DownloadTask) -> str:
        stream_parts: list[str] = []
        video_progress = self._safe_non_negative_float(task.video_progress)
        audio_progress = self._safe_non_negative_float(task.audio_progress)
        if video_progress > 0:
            stream_parts.append(f"{ui_text('Video')} {video_progress:.0f}%")
        if audio_progress > 0:
            stream_parts.append(f"{ui_text('Audio')} {audio_progress:.0f}%")
        return " / ".join(stream_parts)

    def _completed_task_details(self, task: DownloadTask) -> str:
        media_path = Path(str(task.media_path)) if task.media_path else None
        file_size = 0
        file_date = ""
        if media_path is not None:
            try:
                stat = media_path.stat()
                file_size = self._safe_non_negative_int(stat.st_size)
                file_date = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
            except (OSError, OverflowError, ValueError):
                pass
        if not file_date and task.downloaded_at:
            downloaded_at = str(task.downloaded_at)
            try:
                file_date = datetime.fromisoformat(downloaded_at).strftime("%Y-%m-%d %H:%M")
            except ValueError:
                file_date = downloaded_at.replace("T", " ")[:16]
        unknown = ui_text('Unknown')
        size_text = self._format_bytes(file_size) if file_size else str(task.size or unknown)
        author_text = str(task.uploader or unknown)
        return ui_format(
            'File date {date}  Size {size}  Author {author}',
            date=file_date or unknown,
            size=size_text,
            author=author_text,
        )

    def _local_processing_details(
        self,
        task: DownloadTask,
        live_stage_elapsed: float,
        stream_detail: str,
    ) -> str:
        processing_text = {
            "merging": ui_text(
                'Network download complete; merging locally. The final file size will be shown when done.',
            ),
            "transcoding": ui_text(
                'Network download complete; converting locally. The final file size will be shown when done.',
            ),
            "thumbnail": ui_text(
                'Network download complete; processing the thumbnail. The final file size will be shown when done.',
            ),
            "metadata": ui_text(
                'Network download complete; writing metadata. The final file size will be shown when done.',
            ),
            "verifying": ui_text(
                'Network download complete; verifying the output. Its actual size will be shown when done.',
            ),
        }[task.stage]
        stage_progress_detail = ""
        if task.stage == "transcoding":
            encoder = str(task.current_transcode_encoder or "").strip()
            progress_value = min(100.0, self._safe_non_negative_float(task.stage_progress))
            if progress_value > 0:
                stage_progress_detail = ui_format(
                    'Conversion progress {progress}%',
                    progress=f"{progress_value:.0f}",
                )
            if encoder:
                stage_progress_detail = " · ".join(
                    value for value in (stage_progress_detail, encoder) if value
                )
        activity_text = ui_format(
            'Current stage elapsed {elapsed}',
            elapsed=format_duration(live_stage_elapsed),
        )
        return "  ".join(
            value
            for value in (stream_detail, stage_progress_detail, activity_text, processing_text)
            if value
        )

    def _transfer_details(
        self,
        task: DownloadTask,
        visible_progress: float,
        stream_detail: str,
    ) -> str:
        visible_downloaded = max(
            self._safe_non_negative_int(task.downloaded_bytes),
            self._safe_non_negative_int(task.visible_downloaded_bytes),
        )
        visible_total = max(
            self._safe_non_negative_int(task.total_bytes),
            self._safe_non_negative_int(task.visible_total_bytes),
        )
        visible_size = str(task.size or task.visible_size or "")
        total_text = self._format_bytes(visible_total) if visible_total else (visible_size or ui_text('Unknown'))
        if visible_downloaded:
            byte_detail = f"{ui_text('Downloaded')} {self._format_bytes(visible_downloaded)} / {total_text}"
        elif visible_progress > 0 and not visible_total:
            byte_detail = f"{ui_text('Progress')} {visible_progress:.0f}%"
            if visible_size:
                byte_detail += f" · {ui_text('Total')} {visible_size}"
        else:
            byte_detail = f"{ui_text('Downloaded')} 0 B / {total_text}"
        speed = str(task.speed or task.visible_speed or '--')
        eta = str(task.eta or task.visible_eta or '--')
        speed_detail = f"{ui_text('Speed')} {speed}"
        eta_detail = f"{ui_text('ETA')} {eta}"
        return "  ".join(
            value for value in (byte_detail, stream_detail, speed_detail, eta_detail) if value
        )

    def _storage_preview_detail(self, task: DownloadTask) -> str:
        options = task.options_json if isinstance(task.options_json, Mapping) else {}
        storage_preview = options.get("_storage_preview", {})
        if (
            task.status == "completed"
            or not isinstance(storage_preview, Mapping)
            or not storage_preview
        ):
            return ""
        route = (
            ui_text('Cross-disk transfer after processing')
            if storage_preview.get("cross_volume")
            else ui_text('Same-disk processing')
        )
        temporary_dir = compact_path_display(str(storage_preview.get("temporary_dir") or ""))
        final_dir = compact_path_display(str(storage_preview.get("final_dir") or ""))
        path_detail = ui_format(
            'Temporary path {temporary} · Final path {final}',
            temporary=temporary_dir or ui_text('Unknown'),
            final=final_dir or ui_text('Unknown'),
        )
        if storage_preview.get("known"):
            estimate_detail = ui_format(
                'Temporary peak {temporary} · Final output {final}',
                temporary=self._format_bytes(storage_preview.get("temporary_bytes")),
                final=self._format_bytes(storage_preview.get("final_bytes")),
            )
        else:
            estimate_detail = ui_text(
                'Size information is not available yet; required storage will be checked before download.',
            )
        return ui_format(
            'Storage preview: {estimate} · {paths} · {route}',
            estimate=estimate_detail,
            paths=path_detail,
            route=route,
        )

    def _supplemental_task_details(self, task: DownloadTask, live_stage_elapsed: float) -> str:
        parts: list[str] = []
        retry_count = self._safe_non_negative_int(task.retry_count)
        if retry_count and task.stage != "reconnecting":
            parts.append(ui_format(
                'Reconnected {count} time(s)',
                count=retry_count,
            ))
        elapsed_seconds = self._safe_non_negative_float(task.elapsed_seconds)
        if elapsed_seconds > 0:
            timing_detail = ui_format(
                'Total elapsed {elapsed}',
                elapsed=format_duration(elapsed_seconds),
            )
            if live_stage_elapsed > 0 and task.stage not in {"completed", "failed", "canceled"}:
                timing_detail += ui_format(
                    ' · Current stage {elapsed}',
                    elapsed=format_duration(live_stage_elapsed),
                )
            parts.append(timing_detail)
        return "  ".join(parts)

    def _build_task_details(
        self,
        task: DownloadTask,
        live_stage_elapsed: float,
        visible_progress: float,
        visible_total: int,
    ) -> str:
        stream_detail = self._stream_progress_detail(task)
        activity_text = ui_format(
            'Current stage elapsed {elapsed}',
            elapsed=format_duration(live_stage_elapsed),
        )
        if task.status == "completed":
            details = self._completed_task_details(task)
        elif task.status == "deleted":
            details = ui_format(
                'Original size {size}  The file is missing; choose Download Again',
                size=str(task.size or ui_text('Unknown')),
            )
        elif task.status == "failed":
            details = ui_format(
                'Task failed: {error}',
                error=runtime_text(str(task.error)) if task.error else ui_text('Unknown'),
            )
        elif task.status == "partial_failed":
            details = " · ".join((
                ui_text('Some collection items failed'),
                ui_text('Retry Failed Items'),
            ))
        elif task.status == "canceled":
            details = ui_text('Task canceled')
        elif task.status in {"canceling", "暂停中"}:
            details = "  ".join((
                activity_text,
                ui_text('The task is active. This stage cannot report an exact percentage.'),
            ))
        elif self._task_status_is_processing(task):
            details = self._local_processing_details(task, live_stage_elapsed, stream_detail)
        elif task.stage in {"parsing", "formats", "waiting_disk", "parsing_collection"}:
            details = "  ".join((
                activity_text,
                ui_text('The task is active. This stage cannot report an exact percentage.'),
            ))
        elif task.stage == "reconnecting":
            details = "  ".join((
                activity_text,
                ui_text('Waiting for the network to recover; download progress is preserved.'),
            ))
        elif task.stage in TRANSFER_TASK_STAGES and visible_progress <= 0 and visible_total <= 0:
            details = "  ".join((
                activity_text,
                ui_text('The download has started; waiting for the server to report size and progress.'),
            ))
        elif task.status == "waiting_selection" or task.stage == "waiting_selection":
            details = ui_text('Waiting for you to select a format; the task is not stuck.')
        elif task.status == "queued":
            details = ui_text('Waiting in the download queue; it will start when a task slot is available.')
        else:
            details = self._transfer_details(task, visible_progress, stream_detail)

        if task.status != "completed":
            details = "  ".join(
                value
                for value in (
                    details,
                    self._storage_preview_detail(task),
                    self._supplemental_task_details(task, live_stage_elapsed),
                )
                if value
            )
        return details

    def _render_details(
        self,
        task: DownloadTask,
        stage_text: str,
        live_stage_elapsed: float,
        visible_progress: float,
        visible_total: int,
    ) -> None:
        details = self._build_task_details(
            task,
            live_stage_elapsed,
            visible_progress,
            visible_total,
        )
        self.details.setText(details)
        progress_tooltip = ui_format(
            'Current stage: {stage}\nPipeline: Parse → Format → Download → Merge → Cover → Metadata → Verify → Done\n{details}',
            stage=stage_text,
            details=details or ui_text('Waiting for the task to start'),
        )
        if task.current_filename:
            progress_tooltip += ui_format(
                '\nCurrent file: {filename}',
                filename=str(task.current_filename),
            )
        self.progress.setToolTip(progress_tooltip)

    def _render_action(self, task: DownloadTask) -> None:
        if task.status == "downloading":
            text, enabled = ui_text('Pause'), True
        elif task.status == "paused":
            text, enabled = ui_text('Continue'), True
        elif task.status in {"processing", "waiting_selection", "parsing_collection"}:
            text, enabled = ui_text('Cancel'), True
        elif task.status in {"queued", "canceling", "暂停中"}:
            text, enabled = ui_text('Cancel'), task.status == "queued"
        elif task.status in {"failed", "partial_failed", "canceled"}:
            text, enabled = ui_text('Retry'), True
        elif task.status == "deleted":
            text, enabled = ui_text('Download Again'), True
        else:
            text, enabled = ui_text('Open Folder'), task.status == "completed"
        self.action.setText(text)
        self.action.setEnabled(enabled)

    def update_task(self, task: DownloadTask) -> None:
        self._task = task
        live_stage_elapsed = self._live_stage_elapsed(task)
        self._sync_activity_timer(task)
        self._status = task.status
        self._render_task_identity(task)
        self._render_task_thumbnail(task)
        stage_text = self._render_stage(task)
        self._render_format_badge(task)
        visible_progress, visible_total = self._render_progress(task)
        self._render_status(task)
        self._render_details(
            task,
            stage_text,
            live_stage_elapsed,
            visible_progress,
            visible_total,
        )
        self._render_action(task)

    @staticmethod
    def _pipeline_html(task: DownloadTask, current_index: int, stage_code: str) -> str:
        """Render the full download pipeline as compact, color-coded steps."""
        def pipeline_label(value: str) -> str:
            return ui_text(value)
        if task.status == "queued":
            return f'<span style="color:#8b96a6;font-weight:600">○ {ui_text('Waiting to start')}</span>' \
                   f' <span style="color:#b2bac5">→ {pipeline_label("Parse")} → {pipeline_label("Format")} → {pipeline_label("Download")} → {pipeline_label("Merge")} → {pipeline_label("Done")}</span>'
        if task.status in {"canceling", "暂停中"}:
            transition_text = ui_text('Canceling') if task.status == "canceling" else ui_text('Pausing')
            return f'<span style="color:#d48716;font-weight:700">● {transition_text}</span>'
        if task.status == "paused" or task.stage == "paused":
            return f'<span style="color:#d48716;font-weight:600">⏸ {ui_text('Paused')}</span>' \
                   f' <span style="color:#8b96a6">· {ui_text('Click Continue to resume')}</span>'
        if task.status == "partial_failed" or task.stage == "partial_failed":
            return (
                f'<span style="color:#d64444;font-weight:700">⚠ '
                f'{ui_text("Some collection items failed")}</span>'
                f' <span style="color:#8b96a6">· {ui_text("Retry Failed Items")}</span>'
            )
        if task.status in {"failed", "canceled"} or task.stage in {"failed", "canceled"}:
            terminal_stage = task.status if task.status in {"failed", "canceled"} else task.stage
            labels = []
            for index, (_code, stage_label) in enumerate(PIPELINE_STAGES):
                if index < current_index:
                    labels.append(f'<span style="color:#20a35a">✓ {pipeline_label(stage_label)}</span>')
            terminal_text = STAGE_TEXT.get(terminal_stage, terminal_stage)
            labels.append(
                f'<span style="color:#d64444;font-weight:600">✕ '
                f'{ui_text(terminal_text)}</span>'
            )
            return " <span style=\"color:#b2bac5\">·</span> ".join(labels)
        parts: list[str] = []
        for index, (_code, stage_label) in enumerate(PIPELINE_STAGES):
            if index < current_index:
                color, marker, weight = "#20a35a", "✓", "600"
            elif index == current_index:
                color, marker, weight = ("#d48716", "●", "700") if stage_code == "reconnecting" else ("#2f7bdc", "●", "700")
            else:
                color, marker, weight = "#9aa5b4", "○", "400"
            parts.append(f'<span style="color:{color};font-weight:{weight}">{marker} {pipeline_label(stage_label)}</span>')
        return ' <span style="color:#b2bac5">·</span> '.join(parts)

    def _refresh_elided_text(self) -> None:
        """Keep long titles and URLs readable without growing every card."""
        title_width = max(160, self.title.width())
        url_width = max(160, self.url.width())
        metrics = QFontMetrics(self.title.font())
        self.title.setText(metrics.elidedText(self._title_text, Qt.ElideRight, title_width))
        self.url.setText(metrics.elidedText(self._url_text, Qt.ElideMiddle, url_width))
        self.title.setToolTip(self._title_text)
        self.url.setToolTip(self._url_tooltip_text or self._url_text)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._refresh_elided_text()

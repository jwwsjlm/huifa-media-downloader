from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QFontMetrics
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.download_readiness import download_readiness_report
from app.core.download_service import DownloadTask
from app.core.log_service import DownloadLogService
from app.ui.i18n import runtime_text, text as ui_text
from app.ui.media_presentation import error_category_text, thumbnail_pixmap
from app.ui.task_card import STAGE_TEXT, STATUS_TEXT


class FormatSelectionDialog(QDialog):
    """Manual single-video picker for exact video or audio streams."""

    FORMAT_ROW_HEIGHT = 52
    MIN_VISIBLE_FORMAT_ROWS = 3
    MAX_VISIBLE_FORMAT_ROWS = 5

    @staticmethod
    def _non_negative_choice_int(value: object) -> int:
        """Normalize extractor metadata without letting malformed values reach Qt."""

        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError, OverflowError):
            return 0

    @classmethod
    def _video_choice_text(cls, choice: Mapping[str, object]) -> str:
        width = cls._non_negative_choice_int(choice.get('width'))
        source_height = cls._non_negative_choice_int(choice.get('source_height'))
        quality_height = cls._non_negative_choice_int(choice.get('height'))
        if quality_height <= 0:
            quality_height = (
                min(width, source_height)
                if width > 0 and source_height > 0
                else max(width, source_height)
            )

        quality = (
            f"{quality_height}p"
            if quality_height > 0
            else str(choice.get('label') or ui_text('Video'))
        )
        if width > 0 and source_height > 0:
            quality += f" ({width}×{source_height})"

        parts = [quality, str(choice.get('ext') or '?')]
        fps = str(choice.get('fps') or '').strip()
        if fps:
            parts.append(f"{fps} {ui_text('fps')}")
        codec = str(choice.get('codec') or '').strip()
        if codec:
            parts.append(codec)
        note = str(choice.get('format_note') or '').strip()
        if bool(choice.get('hdr')) and 'hdr' not in note.casefold():
            parts.append('HDR')
        if bool(choice.get('has_audio')):
            parts.append(ui_text('Video + audio'))
        language = str(choice.get('language') or '').strip()
        if language:
            parts.append(language)
        if note:
            parts.append(note)
        return '  ·  '.join(parts)

    def __init__(
        self,
        title: str,
        thumbnail_path: str,
        choices: list[dict],
        parent=None,
        *,
        default_content_mode: str = "video",
        default_audio_format: str = "best",
    ):
        super().__init__(parent)
        self.setWindowTitle(ui_text('Select Download Format'))
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        header = QHBoxLayout()
        cover = QLabel(ui_text('Cover'))
        cover.setFixedSize(148, 84)
        cover.setAlignment(Qt.AlignCenter)
        cover.setObjectName("formatCover")
        if thumbnail_path and Path(thumbnail_path).exists():
            pixmap = thumbnail_pixmap(thumbnail_path, 148, 84)
            if not pixmap.isNull():
                cover.setText("")
                cover.setPixmap(pixmap)
        full_title = title or ui_text('Select Download Format')
        heading = QLabel()
        # Keep the preview header compact for playlist entries and other very
        # long titles.  Wrapping into a fixed-height label clipped the final
        # lines; show one stable elided line and retain the full title in the
        # tooltip instead.
        heading.setWordWrap(False)
        heading.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        heading.setText(QFontMetrics(heading.font()).elidedText(full_title, Qt.ElideRight, 380))
        heading.setToolTip(full_title)
        header.addWidget(cover)
        header.addWidget(heading, 1)
        layout.addLayout(header)

        self._choices = [dict(choice) for choice in choices]
        available_kinds = {
            str(choice.get('kind') or 'video')
            for choice in self._choices
        }
        selection_row = QHBoxLayout()
        selection_row.addWidget(QLabel(ui_text('Download Type')))
        self.content_mode = QComboBox()
        self.content_mode.addItem(ui_text('Video'), 'video')
        self.content_mode.addItem(ui_text('Audio'), 'audio')
        combo_model = self.content_mode.model()
        item_at = getattr(combo_model, 'item', None)
        if callable(item_at):
            for index in range(self.content_mode.count()):
                model_item = item_at(index)
                if model_item is not None:
                    model_item.setEnabled(
                        str(self.content_mode.itemData(index)) in available_kinds
                    )
        initial_mode = default_content_mode if default_content_mode in {'video', 'audio'} else 'video'
        if not any(str(choice.get('kind') or 'video') == initial_mode for choice in self._choices):
            initial_mode = 'audio' if any(str(choice.get('kind') or 'video') == 'audio' for choice in self._choices) else 'video'
        self.content_mode.setCurrentIndex(max(0, self.content_mode.findData(initial_mode)))
        selection_row.addWidget(self.content_mode)
        selection_row.addWidget(QLabel(ui_text('Audio Format')))
        self.audio_format = QComboBox()
        for label, value in (
            (ui_text('Original best audio'), 'best'), ('AAC', 'aac'), ('ALAC', 'alac'),
            ('FLAC', 'flac'), ('M4A', 'm4a'), ('MP3', 'mp3'), ('Opus', 'opus'),
            ('Vorbis', 'vorbis'), ('WAV', 'wav'),
        ):
            self.audio_format.addItem(label, value)
        self.audio_format.setCurrentIndex(max(0, self.audio_format.findData(default_audio_format)))
        selection_row.addWidget(self.audio_format)
        selection_row.addStretch(1)
        layout.addLayout(selection_row)

        self.list = QListWidget()
        self.list.setObjectName("formatList")
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setSpacing(3)
        self.list.setFixedHeight(self.MIN_VISIBLE_FORMAT_ROWS * self.FORMAT_ROW_HEIGHT + 12)
        self.content_mode.currentIndexChanged.connect(self._populate_choices)
        self.content_mode.currentIndexChanged.connect(self._sync_manual_controls)
        self._populate_choices()
        self._sync_manual_controls()
        self.list.itemDoubleClicked.connect(lambda _item: self.accept())
        layout.addWidget(self.list)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self._ok_button = buttons.button(QDialogButtonBox.Ok)
        self._ok_button.setText(ui_text('OK'))
        buttons.button(QDialogButtonBox.Cancel).setText(ui_text('Cancel'))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._sync_selection_availability()

    def _populate_choices(self, *_args) -> None:
        selected_kind = str(self.content_mode.currentData() or 'video')
        self.list.clear()
        for choice in self._choices:
            kind = str(choice.get('kind') or 'video')
            if kind != selected_kind:
                continue
            item = QListWidgetItem()
            item.setData(Qt.UserRole, choice)
            item.setSizeHint(QSize(0, self.FORMAT_ROW_HEIGHT - 4))
            row = QWidget()
            row.setObjectName("formatRow")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(10, 3, 10, 3)
            row_layout.setSpacing(8)
            text = QLabel()
            text.setWordWrap(False)
            if kind == 'audio':
                label = (
                    ui_text('Best available audio')
                    if choice.get('selector') == 'bestaudio/best'
                    else str(choice.get('label') or ui_text('Audio'))
                )
            else:
                label = self._video_choice_text(choice)
            text.setText(label)
            text.setToolTip(str(choice.get("label") or label))
            row_layout.addWidget(text, 1)
            self.list.addItem(item)
            self.list.setItemWidget(item, row)
        if self.list.count():
            self.list.setCurrentRow(0)
        visible_rows = min(
            self.MAX_VISIBLE_FORMAT_ROWS,
            max(self.MIN_VISIBLE_FORMAT_ROWS, self.list.count()),
        )
        self.list.setFixedHeight(visible_rows * self.FORMAT_ROW_HEIGHT + 12)
        self._sync_selection_availability()

    def _sync_selection_availability(self) -> None:
        ok_button = getattr(self, '_ok_button', None)
        if ok_button is not None:
            ok_button.setEnabled(self.list.currentItem() is not None)

    def _sync_manual_controls(self, *_args) -> None:
        self.audio_format.setEnabled(self.content_mode.currentData() == 'audio')

    def selected_choice(self) -> dict | None:
        item = self.list.currentItem()
        if not item:
            return None
        choice = dict(item.data(Qt.UserRole) or {})
        choice['content_mode'] = str(self.content_mode.currentData() or 'video')
        choice['audio_format'] = str(self.audio_format.currentData() or 'best')
        return choice


class DownloadLogDialog(QDialog):
    def __init__(self, task: DownloadTask, logs: DownloadLogService, parent=None):
        super().__init__(parent)
        task_title = task.title or task.id
        if task_title == "等待获取视频信息":
            task_title = ui_text('Waiting for video information')
        self.setWindowTitle(ui_text('Download Log: ') + f"{task_title}")
        self.resize(860, 520)
        layout = QVBoxLayout(self)
        summary = QLabel()
        summary.setObjectName("mutedText")
        events = logs.read(task.id)
        counts: dict[str, int] = {}
        for event in events:
            category = str(event.get("category") or "未知")
            counts[category] = counts.get(category, 0) + 1
        stage_label = (
            ui_text(STAGE_TEXT[task.stage])
            if task.stage in STAGE_TEXT
            else runtime_text(task.stage_text or task.stage)
        )
        if task.status == "failed":
            diagnosis = DownloadLogService.classify_error(task.error)
            diagnosis_text = error_category_text(diagnosis)
            summary.setText(
                ui_text('Status: Failed · Stage: ')
                + f"{stage_label}"
                + ui_text(' · Diagnosis: ')
                + f"{diagnosis_text}"
                + ui_text(' · Events: ')
                + str(len(events))
            )
        else:
            status_text = ui_text(STATUS_TEXT.get(task.status, task.status))
            summary.setText(
                ui_text('Status: ') + status_text
                + ui_text(' · Stage: ')
                + f"{stage_label}"
                + ui_text(' · Events: ')
                + str(len(events))
            )
        if counts:
            summary.setToolTip(
                ui_text('; ').join(
                    f"{error_category_text(key)}: {value}"
                    for key, value in counts.items()
                )
            )
        layout.addWidget(summary)
        log_path = QLabel(ui_text('Log file: ') + f"{logs.path_for(task.id)}")
        log_path.setObjectName("mutedText")
        log_path.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(log_path)

        self.text = QTextEdit()
        self.text.setReadOnly(True)
        if events:
            rendered_lines: list[str] = []
            for event in events:
                details = event.get("details") or {}
                suffix = "  " + json.dumps(details, ensure_ascii=False) if details else ""
                rendered_lines.append(
                    f"[{event.get('time', '')}] [{event.get('level', '')}] "
                    f"[{error_category_text(str(event.get('category') or '未知'))}] "
                    f"{runtime_text(event.get('message') or '')}{suffix}"
                )
            rendered_log = "\n".join(rendered_lines)
        else:
            rendered_log = ui_text('No logs.')
        self.text.setPlainText(rendered_log)
        layout.addWidget(self.text, 1)

        buttons = QHBoxLayout()
        copy_button = QPushButton(ui_text('Copy Logs'))
        copy_button.clicked.connect(self.copy_logs)
        clear_button = QPushButton(ui_text('Clear Logs'))
        clear_button.clicked.connect(lambda: self.clear_logs(task.id, logs))
        close_button = QPushButton(ui_text('Close'))
        close_button.clicked.connect(self.accept)
        buttons.addWidget(copy_button)
        buttons.addWidget(clear_button)
        buttons.addStretch(1)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

    def copy_logs(self) -> None:
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(self.text.toPlainText())

    def clear_logs(self, task_id: str, logs: DownloadLogService) -> None:
        logs.clear(task_id)
        self.text.setPlainText(ui_text('No logs.'))


class DownloadReadinessDialog(QDialog):
    """Local preflight that gives users a no-network answer before enqueueing."""

    def __init__(self, window: Any, parent=None):
        super().__init__(parent or window)
        self.window = window
        self.setWindowTitle(ui_text('Download Readiness'))
        self.resize(820, 370)
        layout = QVBoxLayout(self)
        self.summary = QLabel(ui_text('Checking the local download environment…'))
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([
            ui_text('Check', context="readiness.column"), ui_text('Status'), ui_text('Details'),
        ])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        layout.addWidget(self.tree, 1)
        buttons = QHBoxLayout()
        self.refresh_button = QPushButton(ui_text('Check Again'))
        self.refresh_button.clicked.connect(self.refresh_report)
        settings_button = QPushButton(ui_text('Open Settings'))
        settings_button.clicked.connect(self.open_settings)
        components_button = QPushButton(ui_text('Check Components'))
        components_button.setToolTip(ui_text(
            'Check yt-dlp recommended FFmpeg, Deno and other component updates through GitHub Releases',
        ))
        components_button.clicked.connect(self.check_components)
        close_button = QPushButton(ui_text('Close'))
        close_button.clicked.connect(self.accept)
        buttons.addWidget(self.refresh_button)
        buttons.addWidget(settings_button)
        buttons.addWidget(components_button)
        buttons.addStretch(1)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)
        QTimer.singleShot(0, self.refresh_report)

    def refresh_report(self) -> None:
        self.refresh_button.setEnabled(False)
        self.tree.clear()
        try:
            ready, rows = download_readiness_report(
                self.window.app_settings.get("download_dir"),
                self.window.app_settings.get("download_cookie_file"),
                self.window.app_settings.get("ffmpeg_path"),
                self.window.app_settings.get("download_cookie_source"),
                self.window.app_settings.get("download_cookie_browser"),
                self.window.app_settings.get("ytdlp_core_mode"),
                self.window.app_settings.get("ffprobe_path"),
                self.window.app_settings.get("deno_path"),
                self.window.app_settings.get("ytdlp_ejs_source"),
            )
        except Exception as exc:
            self.summary.setText(ui_text('Unable to check the download environment: ') + runtime_text(exc))
            self.refresh_button.setEnabled(True)
            return
        for row in rows:
            translated_detail = str(row.get("detail") or "")
            row_name = str(row.get("name") or "")
            row_state = str(row.get("state") or "")
            item = QTreeWidgetItem([
                row_name,
                row_state,
                translated_detail,
            ])
            if row["state"] == "不可用":
                item.setForeground(1, QBrush(QColor("#c2413a")))
            elif row["state"] in {"建议安装", "可选", "未配置"}:
                item.setForeground(1, QBrush(QColor("#b26a00")))
            else:
                item.setForeground(1, QBrush(QColor("#138a4b")))
            item.setToolTip(2, translated_detail)
            self.tree.addTopLevelItem(item)
        ready_summary = ui_text(
            'Download environment is ready. You can paste a URL to start.',
        )
        blocked_summary = ui_text(
            'Blocking issues were found. Fix items marked unavailable and try again.',
        )
        self.summary.setText(ready_summary if ready else blocked_summary)
        self.refresh_button.setEnabled(True)

    def open_settings(self) -> None:
        self.window.tabs.setCurrentWidget(self.window.settings)
        self.accept()

    def check_components(self) -> None:
        self.accept()
        self.window.check_updates()

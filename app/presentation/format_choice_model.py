from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from PySide6.QtCore import (
    QByteArray,
    QAbstractListModel,
    QModelIndex,
    Property,
    Qt,
    QUrl,
    Signal,
    Slot,
)

from app.core.download_options import DownloadOptions


class FormatChoiceModel(QAbstractListModel):
    """Expose one pending yt-dlp manual-format selection to QML."""

    ChoiceIndexRole = Qt.ItemDataRole.UserRole + 1
    KindRole = ChoiceIndexRole + 1
    LabelRole = ChoiceIndexRole + 2
    SelectorRole = ChoiceIndexRole + 3
    ExtensionRole = ChoiceIndexRole + 4
    CodecRole = ChoiceIndexRole + 5
    WidthRole = ChoiceIndexRole + 6
    HeightRole = ChoiceIndexRole + 7
    SourceHeightRole = ChoiceIndexRole + 8
    FpsRole = ChoiceIndexRole + 9
    HdrRole = ChoiceIndexRole + 10
    HasAudioRole = ChoiceIndexRole + 11
    LanguageRole = ChoiceIndexRole + 12
    FormatNoteRole = ChoiceIndexRole + 13
    AbrRole = ChoiceIndexRole + 14
    FileSizeRole = ChoiceIndexRole + 15

    countChanged = Signal()
    pendingChanged = Signal()
    modeChanged = Signal()
    audioFormatChanged = Signal()

    _ROLE_NAMES = {
        ChoiceIndexRole: b"choiceIndex",
        KindRole: b"kind",
        LabelRole: b"label",
        SelectorRole: b"selector",
        ExtensionRole: b"extension",
        CodecRole: b"codec",
        WidthRole: b"formatWidth",
        HeightRole: b"formatHeight",
        SourceHeightRole: b"sourceHeight",
        FpsRole: b"fps",
        HdrRole: b"hdr",
        HasAudioRole: b"hasAudio",
        LanguageRole: b"language",
        FormatNoteRole: b"formatNote",
        AbrRole: b"abr",
        FileSizeRole: b"fileSize",
    }

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._task_id = ""
        self._title = ""
        self._thumbnail_url = QUrl()
        self._choices: list[dict[str, Any]] = []
        self._visible_indices: list[int] = []
        self._content_mode = "video"
        self._audio_format = "best"

    def roleNames(self) -> dict[int, QByteArray]:  # type: ignore[override]
        return {role: QByteArray(name) for role, name in self._ROLE_NAMES.items()}

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # type: ignore[override]
        return 0 if parent.isValid() else len(self._visible_indices)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # type: ignore[override]
        if not index.isValid() or not 0 <= index.row() < len(self._visible_indices):
            return None
        choice_index = self._visible_indices[index.row()]
        choice = self._choices[choice_index]
        if role == Qt.ItemDataRole.DisplayRole:
            return str(choice.get("label") or "")
        if role == self.ChoiceIndexRole:
            return choice_index
        if role == self.KindRole:
            return str(choice.get("kind") or "video")
        if role == self.LabelRole:
            return str(choice.get("label") or "")
        if role == self.SelectorRole:
            return str(choice.get("selector") or "")
        if role == self.ExtensionRole:
            return str(choice.get("ext") or "")
        if role == self.CodecRole:
            return str(choice.get("codec") or "")
        if role == self.WidthRole:
            return self._non_negative_int(choice.get("width"))
        if role == self.HeightRole:
            return self._non_negative_int(choice.get("height"))
        if role == self.SourceHeightRole:
            return self._non_negative_int(choice.get("source_height"))
        if role == self.FpsRole:
            return str(choice.get("fps") or "")
        if role == self.HdrRole:
            return bool(choice.get("hdr"))
        if role == self.HasAudioRole:
            return bool(choice.get("has_audio"))
        if role == self.LanguageRole:
            return str(choice.get("language") or "")
        if role == self.FormatNoteRole:
            return str(choice.get("format_note") or "")
        if role == self.AbrRole:
            return self._non_negative_int(choice.get("abr"))
        if role == self.FileSizeRole:
            return self._non_negative_int(choice.get("filesize"))
        return None

    @Property(int, notify=countChanged)
    def count(self) -> int:
        return len(self._visible_indices)

    @Property(int, notify=countChanged)
    def firstChoiceIndex(self) -> int:
        return self._visible_indices[0] if self._visible_indices else -1

    @Property(bool, notify=pendingChanged)
    def pending(self) -> bool:
        return bool(self._task_id)

    @Property(str, notify=pendingChanged)
    def taskId(self) -> str:
        return self._task_id

    @Property(str, notify=pendingChanged)
    def title(self) -> str:
        return self._title

    @Property(QUrl, notify=pendingChanged)
    def thumbnailUrl(self) -> QUrl:
        return self._thumbnail_url

    @Property(str, notify=modeChanged)
    def contentMode(self) -> str:
        return self._content_mode

    @Property(str, notify=audioFormatChanged)
    def audioFormat(self) -> str:
        return self._audio_format

    @Property(bool, notify=pendingChanged)
    def hasVideo(self) -> bool:
        return self._has_kind("video")

    @Property(bool, notify=pendingChanged)
    def hasAudio(self) -> bool:
        return self._has_kind("audio")

    @Slot(str)
    def setContentMode(self, value: str) -> None:
        mode = str(value or "video")
        if mode not in {"video", "audio"} or not self._has_kind(mode):
            return
        if mode == self._content_mode:
            return
        self._content_mode = mode
        self._refresh_visible()
        self.modeChanged.emit()

    @Slot(str)
    def setAudioFormat(self, value: str) -> None:
        value = DownloadOptions.from_mapping({"audio_format": value}).audio_format
        if value == self._audio_format:
            return
        self._audio_format = value
        self.audioFormatChanged.emit()

    def set_payload(self, task_id: str, payload: Mapping[str, object]) -> bool:
        raw_choices = payload.get("choices")
        choices = [
            dict(choice)
            for choice in (raw_choices if isinstance(raw_choices, (list, tuple)) else ())
            if isinstance(choice, Mapping)
        ]
        if not choices:
            self.clear()
            return False
        requested_mode = str(payload.get("content_mode") or "video")
        if requested_mode not in {"video", "audio"} or not any(
            str(choice.get("kind") or "video") == requested_mode
            for choice in choices
        ):
            requested_mode = "audio" if any(
                str(choice.get("kind") or "video") == "audio"
                for choice in choices
            ) else "video"
        thumbnail_path = str(payload.get("thumbnail_path") or "")
        self.beginResetModel()
        self._task_id = str(task_id or "")
        self._title = str(payload.get("title") or "")
        self._thumbnail_url = (
            QUrl.fromLocalFile(thumbnail_path) if thumbnail_path else QUrl()
        )
        self._choices = choices
        self._content_mode = requested_mode
        self._audio_format = DownloadOptions.from_mapping(
            {"audio_format": payload.get("audio_format")}
        ).audio_format
        self._visible_indices = self._indices_for_mode(requested_mode)
        self.endResetModel()
        self.countChanged.emit()
        self.pendingChanged.emit()
        self.modeChanged.emit()
        self.audioFormatChanged.emit()
        return True

    def selected_choice(self, choice_index: int) -> dict[str, Any] | None:
        try:
            choice_index = int(choice_index)
        except (TypeError, ValueError, OverflowError):
            return None
        if not 0 <= choice_index < len(self._choices):
            return None
        choice = dict(self._choices[choice_index])
        if str(choice.get("kind") or "video") != self._content_mode:
            return None
        choice["content_mode"] = self._content_mode
        choice["audio_format"] = self._audio_format
        return choice

    @Slot()
    def clear(self) -> None:
        if not self._task_id and not self._choices:
            return
        self.beginResetModel()
        self._task_id = ""
        self._title = ""
        self._thumbnail_url = QUrl()
        self._choices = []
        self._visible_indices = []
        self._content_mode = "video"
        self._audio_format = "best"
        self.endResetModel()
        self.countChanged.emit()
        self.pendingChanged.emit()
        self.modeChanged.emit()
        self.audioFormatChanged.emit()

    def _refresh_visible(self) -> None:
        self.beginResetModel()
        self._visible_indices = self._indices_for_mode(self._content_mode)
        self.endResetModel()
        self.countChanged.emit()

    def _has_kind(self, kind: str) -> bool:
        return any(str(choice.get("kind") or "video") == kind for choice in self._choices)

    def _indices_for_mode(self, mode: str) -> list[int]:
        return [
            index
            for index, choice in enumerate(self._choices)
            if str(choice.get("kind") or "video") == mode
        ]

    @staticmethod
    def _non_negative_int(value: object) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError, OverflowError):
            return 0

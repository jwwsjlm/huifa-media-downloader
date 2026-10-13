from __future__ import annotations

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

from app.core.download_service import format_duration


class CollectionEntryModel(QAbstractListModel):
    """Expose one collection's complete filtered result set to QML."""

    IndexRole = Qt.ItemDataRole.UserRole + 1
    TitleRole = IndexRole + 1
    UploaderRole = IndexRole + 2
    DurationRole = IndexRole + 3
    DurationTextRole = IndexRole + 4
    UploadDateRole = IndexRole + 5
    ThumbnailUrlRole = IndexRole + 6
    LiveStatusRole = IndexRole + 7
    AvailabilityRole = IndexRole + 8
    EntryKindRole = IndexRole + 9
    DownloadableRole = IndexRole + 10
    DisabledReasonRole = IndexRole + 11
    SelectedRole = IndexRole + 12
    CompletedRole = IndexRole + 13
    EstimatedBytesRole = IndexRole + 14

    countChanged = Signal()
    parentTaskIdChanged = Signal()
    filterChanged = Signal()
    sortModeChanged = Signal()
    selectedCountChanged = Signal()

    _ROLE_NAMES = {
        IndexRole: b"collectionIndex",
        TitleRole: b"title",
        UploaderRole: b"uploader",
        DurationRole: b"duration",
        DurationTextRole: b"durationText",
        UploadDateRole: b"uploadDate",
        ThumbnailUrlRole: b"thumbnailUrl",
        LiveStatusRole: b"liveStatus",
        AvailabilityRole: b"availability",
        EntryKindRole: b"entryKind",
        DownloadableRole: b"downloadable",
        DisabledReasonRole: b"disabledReason",
        SelectedRole: b"selected",
        CompletedRole: b"completed",
        EstimatedBytesRole: b"estimatedBytes",
    }

    def __init__(self, database: Any, download_service: Any, parent=None) -> None:
        super().__init__(parent)
        self._database = database
        self._download_service = download_service
        self._entries: list[dict[str, Any]] = []
        self._parent_task_id = ""
        self._filter_text = ""
        self._status_filter = "all"
        self._sort_mode = "collection_index"
        self._sort_descending = False
        self._selected_count = 0

    def roleNames(self) -> dict[int, QByteArray]:  # type: ignore[override]
        return {role: QByteArray(name) for role, name in self._ROLE_NAMES.items()}

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # type: ignore[override]
        return 0 if parent.isValid() else len(self._entries)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # type: ignore[override]
        if not index.isValid() or not 0 <= index.row() < len(self._entries):
            return None
        entry = self._entries[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return str(entry.get("title") or "")
        if role == self.IndexRole:
            return int(entry.get("index") or 0)
        if role == self.TitleRole:
            return str(entry.get("title") or "")
        if role == self.UploaderRole:
            return str(entry.get("uploader") or "")
        if role == self.DurationRole:
            return float(entry.get("duration") or 0)
        if role == self.DurationTextRole:
            return format_duration(float(entry.get("duration") or 0))
        if role == self.UploadDateRole:
            return str(entry.get("upload_date") or "")
        if role == self.ThumbnailUrlRole:
            return QUrl(str(entry.get("thumbnail") or ""))
        if role == self.LiveStatusRole:
            return str(entry.get("live_status") or "")
        if role == self.AvailabilityRole:
            return str(entry.get("availability") or "")
        if role == self.EntryKindRole:
            return str(entry.get("entry_kind") or "video")
        if role == self.DownloadableRole:
            return bool(entry.get("downloadable"))
        if role == self.DisabledReasonRole:
            return str(entry.get("disabled_reason") or "")
        if role == self.SelectedRole:
            return bool(entry.get("selected"))
        if role == self.CompletedRole:
            return bool(entry.get("completed"))
        if role == self.EstimatedBytesRole:
            return max(0, int(entry.get("estimated_bytes") or 0))
        return None

    @Property(int, notify=countChanged)
    def count(self) -> int:
        return len(self._entries)

    @Property(str, notify=parentTaskIdChanged)
    def parentTaskId(self) -> str:
        return self._parent_task_id

    @Property(str, notify=filterChanged)
    def filterText(self) -> str:
        return self._filter_text

    @Property(str, notify=filterChanged)
    def statusFilter(self) -> str:
        return self._status_filter

    @Property(str, notify=sortModeChanged)
    def sortMode(self) -> str:
        return self._sort_mode

    @Property(bool, notify=sortModeChanged)
    def sortDescending(self) -> bool:
        return self._sort_descending

    @Property(int, notify=selectedCountChanged)
    def selectedCount(self) -> int:
        return self._selected_count

    @Slot(str)
    def setParentTaskId(self, task_id: str) -> None:
        task_id = str(task_id or "")
        if task_id == self._parent_task_id:
            return
        self._parent_task_id = task_id
        self.parentTaskIdChanged.emit()
        self.refresh()

    def setDatabase(self, database: Any) -> None:
        parent_changed = bool(self._parent_task_id)
        selected_changed = self._selected_count != 0
        self.beginResetModel()
        self._database = database
        self._entries = []
        self._parent_task_id = ""
        self._selected_count = 0
        self.endResetModel()
        self.countChanged.emit()
        if parent_changed:
            self.parentTaskIdChanged.emit()
        if selected_changed:
            self.selectedCountChanged.emit()

    @Slot(str)
    def setFilterText(self, value: str) -> None:
        value = str(value or "").strip()
        if value == self._filter_text:
            return
        self._filter_text = value
        self.filterChanged.emit()
        self.refresh()

    @Slot(str)
    def setStatusFilter(self, value: str) -> None:
        value = str(value or "all").strip().casefold() or "all"
        if value == self._status_filter:
            return
        self._status_filter = value
        self.filterChanged.emit()
        self.refresh()

    @Slot(str, bool)
    def setSortMode(self, value: str, descending: bool = False) -> None:
        value = str(value or "collection_index")
        if value not in {
            "collection_index",
            "title",
            "uploader",
            "duration",
            "upload_date",
            "availability",
        }:
            value = "collection_index"
        descending = bool(descending)
        if value == self._sort_mode and descending == self._sort_descending:
            return
        self._sort_mode = value
        self._sort_descending = descending
        self.sortModeChanged.emit()
        self.refresh()

    @Slot()
    def refresh(self, *, append_only: bool = False) -> None:
        append_default_view = (
            append_only
            and bool(self._parent_task_id)
            and not self._filter_text
            and self._status_filter == "all"
            and self._sort_mode == "collection_index"
            and not self._sort_descending
        )
        if append_default_view:
            stored_count = self._database.collection_probe_entry_count(
                self._parent_task_id
            )
            if stored_count >= len(self._entries):
                entries = self._database.list_collection_probe_entries(
                    self._parent_task_id,
                    offset=len(self._entries),
                    limit=2**31 - 1,
                )
                selected_count = self._database.count_selected_collection_probe_entries(
                    self._parent_task_id
                )
                if entries:
                    first = len(self._entries)
                    self.beginInsertRows(QModelIndex(), first, first + len(entries) - 1)
                    self._entries.extend(entries)
                    self.endInsertRows()
                    self.countChanged.emit()
                if selected_count != self._selected_count:
                    self._selected_count = selected_count
                    self.selectedCountChanged.emit()
                return
        if not self._parent_task_id:
            entries: list[dict[str, Any]] = []
            selected_count = 0
        else:
            entries = self._database.list_collection_probe_entries(
                self._parent_task_id,
                offset=0,
                limit=2**31 - 1,
                query=self._filter_text,
                state=self._status_filter,
                sort_column=self._sort_mode,
                sort_descending=self._sort_descending,
            )
            selected_count = self._database.count_selected_collection_probe_entries(
                self._parent_task_id
            )
        if entries == self._entries:
            if selected_count != self._selected_count:
                self._selected_count = selected_count
                self.selectedCountChanged.emit()
            return
        self.beginResetModel()
        self._entries = entries
        self.endResetModel()
        self.countChanged.emit()
        if selected_count != self._selected_count:
            self._selected_count = selected_count
            self.selectedCountChanged.emit()

    @Slot(int, bool)
    def setSelected(self, collection_index: int, selected: bool) -> None:
        if not self._parent_task_id:
            return
        target = int(collection_index or 0)
        row = next(
            (
                row
                for row, entry in enumerate(self._entries)
                if int(entry.get("index") or 0) == target
            ),
            -1,
        )
        if row < 0 or not self._entries[row].get("downloadable"):
            return
        selected = bool(selected)
        if bool(self._entries[row].get("selected")) == selected:
            return
        if not self._download_service.set_collection_probe_entry_selected(
            self._parent_task_id,
            target,
            selected,
        ):
            return
        self._entries[row]["selected"] = selected
        index = self.index(row, 0)
        self.dataChanged.emit(index, index, [self.SelectedRole])
        self._selected_count += 1 if selected else -1
        self.selectedCountChanged.emit()

    @Slot(str)
    def setSelectionMode(self, mode: str) -> None:
        if not self._parent_task_id:
            return
        if not self._download_service.set_collection_probe_selection(
            self._parent_task_id,
            str(mode or ""),
        ):
            return
        self.refresh()

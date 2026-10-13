from __future__ import annotations

from typing import Any

from PySide6.QtCore import QByteArray, QAbstractListModel, QModelIndex, Property, Qt, Signal, Slot

from app.presentation.media_text import (
    format_media_timestamp,
    platform_label,
    publish_status_label,
)

_STATUSES = frozenset({"all", "pending", "uploading", "success", "failed"})


class PublishQueueModel(QAbstractListModel):
    """Expose the full publish queue to QML without user-visible paging."""

    TaskIdRole = Qt.ItemDataRole.UserRole + 1
    MediaIdRole = TaskIdRole + 1
    PlatformRole = TaskIdRole + 2
    AccountRole = TaskIdRole + 3
    StatusRole = TaskIdRole + 4
    StatusTextRole = TaskIdRole + 5
    TitleRole = TaskIdRole + 6
    ResultRole = TaskIdRole + 7
    CreatedAtRole = TaskIdRole + 8
    CanRunRole = TaskIdRole + 9
    CanRetryRole = TaskIdRole + 10

    countChanged = Signal()
    filterChanged = Signal()
    scopeChanged = Signal()
    stateChanged = Signal()

    _ROLE_NAMES = {
        TaskIdRole: b"taskId",
        MediaIdRole: b"mediaId",
        PlatformRole: b"platform",
        AccountRole: b"account",
        StatusRole: b"status",
        StatusTextRole: b"statusText",
        TitleRole: b"title",
        ResultRole: b"result",
        CreatedAtRole: b"createdAt",
        CanRunRole: b"canRun",
        CanRetryRole: b"canRetry",
    }
    _STATUS_UPDATE_ROLES = (
        StatusRole,
        StatusTextRole,
        ResultRole,
        CanRunRole,
        CanRetryRole,
    )

    def __init__(self, database: Any, parent=None) -> None:
        super().__init__(parent)
        self._database = database
        self._catalog: list[dict[str, Any]] = []
        self._entries: list[dict[str, Any]] = []
        self._search_text = ""
        self._status_filter = "all"
        self._media_id = 0
        self._loaded = False
        self._dirty = True

    def roleNames(self) -> dict[int, QByteArray]:  # type: ignore[override]
        return {role: QByteArray(name) for role, name in self._ROLE_NAMES.items()}

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # type: ignore[override]
        return 0 if parent.isValid() else len(self._entries)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # type: ignore[override]
        if not index.isValid() or not 0 <= index.row() < len(self._entries):
            return None
        row = self._entries[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return str(row.get("title") or "")
        if role == self.TaskIdRole:
            return int(row.get("id") or 0)
        if role == self.MediaIdRole:
            return int(row.get("media_id") or 0)
        if role == self.PlatformRole:
            return platform_label(str(row.get("platform") or ""))
        if role == self.AccountRole:
            return str(row.get("account") or "default")
        status = str(row.get("status") or "pending")
        if role == self.StatusRole:
            return status
        if role == self.StatusTextRole:
            return publish_status_label(status)
        if role == self.TitleRole:
            return str(row.get("title") or "")
        if role == self.ResultRole:
            return str(row.get("result") or "")
        if role == self.CreatedAtRole:
            return format_media_timestamp(str(row.get("created_at") or ""))
        if role == self.CanRunRole:
            return status in {"pending", "failed"}
        if role == self.CanRetryRole:
            return status == "failed"
        return None

    @Property(int, notify=countChanged)
    def count(self) -> int:
        return len(self._entries)

    @Property(int, notify=scopeChanged)
    def mediaId(self) -> int:
        return self._media_id

    @Property(str, notify=filterChanged)
    def searchText(self) -> str:
        return self._search_text

    @Property(str, notify=filterChanged)
    def statusFilter(self) -> str:
        return self._status_filter

    @Property(bool, notify=stateChanged)
    def loaded(self) -> bool:
        return self._loaded

    @Property(bool, notify=stateChanged)
    def dirty(self) -> bool:
        return self._dirty

    @Slot(int)
    def setMediaId(self, media_id: int) -> None:
        target = max(0, int(media_id or 0))
        if target == self._media_id:
            return
        self._media_id = target
        self.scopeChanged.emit()
        self.refresh()

    @Slot(str)
    def setSearchText(self, value: str) -> None:
        value = str(value or "").strip()
        if value == self._search_text:
            return
        self._search_text = value
        self.filterChanged.emit()
        self._apply_view()

    @Slot(str)
    def setStatusFilter(self, value: str) -> None:
        value = str(value or "all").strip().casefold()
        if value not in _STATUSES:
            value = "all"
        if value == self._status_filter:
            return
        self._status_filter = value
        self.filterChanged.emit()
        self._apply_view()

    @Slot()
    def refresh(self) -> None:
        rows = self._database.list_publish_tasks(
            limit=None,
            media_id=self._media_id or None,
        )
        self._catalog = [dict(row) for row in rows]
        changed = not self._loaded or self._dirty
        self._loaded = True
        self._dirty = False
        self._apply_view()
        if changed:
            self.stateChanged.emit()

    @Slot()
    def markDirty(self) -> None:
        if self._dirty:
            return
        self._dirty = True
        self.stateChanged.emit()

    def setDatabase(self, database: Any) -> None:
        self.beginResetModel()
        self._database = database
        self._catalog = []
        self._entries = []
        self._loaded = False
        self._dirty = True
        self.endResetModel()
        self.countChanged.emit()
        self.stateChanged.emit()

    @Slot(int)
    def refreshTask(self, task_id: int) -> None:
        if not self._loaded:
            self.markDirty()
            return
        target = int(task_id or 0)
        row = self._database.get_publish_task(target) if target > 0 else None
        catalog_index = next(
            (index for index, entry in enumerate(self._catalog) if int(entry.get("id") or 0) == target),
            -1,
        )
        allowed = row is not None and (
            not self._media_id or int(row["media_id"] or 0) == self._media_id
        )
        if not allowed:
            if catalog_index < 0:
                return
            self._catalog.pop(catalog_index)
            entry_index = next(
                (index for index, entry in enumerate(self._entries) if int(entry.get("id") or 0) == target),
                -1,
            )
            if entry_index >= 0:
                self.beginRemoveRows(QModelIndex(), entry_index, entry_index)
                self._entries.pop(entry_index)
                self.endRemoveRows()
                self.countChanged.emit()
            return
        fresh = dict(row)
        if catalog_index < 0:
            catalog_index = next(
                (
                    index
                    for index, entry in enumerate(self._catalog)
                    if target > int(entry.get("id") or 0)
                ),
                len(self._catalog),
            )
            self._catalog.insert(catalog_index, fresh)
        elif fresh == self._catalog[catalog_index]:
            return
        else:
            self._catalog[catalog_index] = fresh

        needle = self._search_text.casefold()
        entry_index = next(
            (index for index, entry in enumerate(self._entries) if int(entry.get("id") or 0) == target),
            -1,
        )
        visible = self._matches(fresh, needle)
        if entry_index >= 0 and visible:
            self._entries[entry_index] = fresh
            index = self.index(entry_index, 0)
            self.dataChanged.emit(index, index, self._STATUS_UPDATE_ROLES)
        elif entry_index >= 0:
            self.beginRemoveRows(QModelIndex(), entry_index, entry_index)
            self._entries.pop(entry_index)
            self.endRemoveRows()
            self.countChanged.emit()
        elif visible:
            row = sum(
                1
                for entry in self._catalog[:catalog_index]
                if self._matches(entry, needle)
            )
            self.beginInsertRows(QModelIndex(), row, row)
            self._entries.insert(row, fresh)
            self.endInsertRows()
            self.countChanged.emit()

    def _apply_view(self) -> None:
        needle = self._search_text.casefold()
        entries = [
            row
            for row in self._catalog
            if self._matches(row, needle)
        ]
        if entries == self._entries:
            return
        self.beginResetModel()
        self._entries = entries
        self.endResetModel()
        self.countChanged.emit()

    def _matches(self, row: dict[str, Any], needle: str) -> bool:
        status = str(row.get("status") or "pending")
        if self._status_filter != "all" and status != self._status_filter:
            return False
        if not needle:
            return True
        return needle in " ".join(
            str(row.get(key) or "")
            for key in ("id", "platform", "account", "status", "title", "result")
        ).casefold()

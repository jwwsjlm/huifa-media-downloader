from __future__ import annotations

from collections.abc import Iterable
from difflib import SequenceMatcher
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

from app.core.download_service import DownloadTask


_ACTIVE_STATUSES = frozenset({
    "queued",
    "downloading",
    "processing",
    "waiting_processing",
    "waiting_selection",
    "parsing_collection",
    "canceling",
    "暂停中",
})

_STATUS_FILTERS = {
    "paused": frozenset({"paused", "暂停中"}),
    "failed": frozenset({"failed", "partial_failed", "processing_failed"}),
}


class TaskListModel(QAbstractListModel):
    """Expose every top-level task to QML without user-visible paging."""

    TaskIdRole = Qt.ItemDataRole.UserRole + 1
    TitleRole = TaskIdRole + 1
    UrlRole = TaskIdRole + 2
    TaskKindRole = TaskIdRole + 3
    StatusRole = TaskIdRole + 4
    StageRole = TaskIdRole + 5
    StageTextRole = TaskIdRole + 6
    ProgressRole = TaskIdRole + 7
    StageProgressRole = TaskIdRole + 8
    SpeedRole = TaskIdRole + 9
    SpeedBpsRole = TaskIdRole + 10
    DownloadedBytesRole = TaskIdRole + 11
    TotalBytesRole = TaskIdRole + 12
    EtaRole = TaskIdRole + 13
    SizeRole = TaskIdRole + 14
    ErrorRole = TaskIdRole + 15
    ThumbnailPathRole = TaskIdRole + 16
    MediaPathRole = TaskIdRole + 17
    CreatedAtRole = TaskIdRole + 18
    SelectedQualityRole = TaskIdRole + 19
    RetryCountRole = TaskIdRole + 20
    RetryTotalRole = TaskIdRole + 21
    ReconnectMessageRole = TaskIdRole + 22
    ActiveRole = TaskIdRole + 23
    TerminalRole = TaskIdRole + 24
    ThumbnailUrlRole = TaskIdRole + 25
    LocalHistoryRole = TaskIdRole + 26

    countChanged = Signal()
    filterChanged = Signal()
    sortModeChanged = Signal()

    _ROLE_NAMES = {
        TaskIdRole: b"taskId",
        TitleRole: b"title",
        UrlRole: b"url",
        TaskKindRole: b"taskKind",
        StatusRole: b"status",
        StageRole: b"stage",
        StageTextRole: b"stageText",
        ProgressRole: b"progress",
        StageProgressRole: b"stageProgress",
        SpeedRole: b"speed",
        SpeedBpsRole: b"speedBps",
        DownloadedBytesRole: b"downloadedBytes",
        TotalBytesRole: b"totalBytes",
        EtaRole: b"eta",
        SizeRole: b"size",
        ErrorRole: b"error",
        ThumbnailPathRole: b"thumbnailPath",
        MediaPathRole: b"mediaPath",
        CreatedAtRole: b"createdAt",
        SelectedQualityRole: b"selectedQuality",
        RetryCountRole: b"retryCount",
        RetryTotalRole: b"retryTotal",
        ReconnectMessageRole: b"reconnectMessage",
        ActiveRole: b"active",
        TerminalRole: b"terminal",
        ThumbnailUrlRole: b"thumbnailUrl",
        LocalHistoryRole: b"localHistory",
    }

    _DIRECT_ROLE_FIELDS = {
        TaskIdRole: "id",
        TitleRole: "title",
        UrlRole: "url",
        TaskKindRole: "task_kind",
        StatusRole: "status",
        StageRole: "stage",
        StageTextRole: "stage_text",
        ErrorRole: "error",
        ThumbnailPathRole: "thumbnail_path",
        MediaPathRole: "media_path",
        CreatedAtRole: "created_at",
        SelectedQualityRole: "selected_quality",
        ReconnectMessageRole: "reconnect_message",
    }

    # A progress tick may resolve identity or switch conversion state, but QML
    # only binds the task-card fields below. Static paths and diagnostic
    # counters update through task_updated instead of invalidating every card.
    _PROGRESS_ROLES = (
        TitleRole,
        StatusRole,
        StageRole,
        StageProgressRole,
        StageTextRole,
        ProgressRole,
        SpeedRole,
        EtaRole,
        SizeRole,
        ErrorRole,
        SelectedQualityRole,
        ActiveRole,
        ThumbnailUrlRole,
    )

    def __init__(self, download_service: Any, parent=None) -> None:
        super().__init__(parent)
        self._service = download_service
        self._task_ids: list[str] = []
        self._rows_by_task_id: dict[str, int] = {}
        self._sort_states: dict[str, tuple[bool, str]] = {}
        self._filter_text = ""
        self._status_filter = "all"
        self._sort_mode = "newest"
        self._connect_service()
        self.refresh()

    def _connect_service(self) -> None:
        self._service.task_added.connect(self._on_task_added, Qt.QueuedConnection)
        self._service.tasks_added.connect(self._on_tasks_added, Qt.QueuedConnection)
        self._service.task_updated.connect(self._on_task_updated, Qt.QueuedConnection)
        self._service.task_progress.connect(self._on_task_progress, Qt.QueuedConnection)
        self._service.task_deleted.connect(self._on_task_deleted, Qt.QueuedConnection)

    def roleNames(self) -> dict[int, QByteArray]:  # type: ignore[override]
        return {
            role: QByteArray(name)
            for role, name in self._ROLE_NAMES.items()
        }

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # type: ignore[override]
        return 0 if parent.isValid() else len(self._task_ids)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # type: ignore[override]
        if not index.isValid() or not 0 <= index.row() < len(self._task_ids):
            return None
        task = self._service.tasks.get(self._task_ids[index.row()])
        if task is None:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return str(task.title or "")
        return self._role_value(task, role)

    @Property(int, notify=countChanged)
    def count(self) -> int:
        return len(self._task_ids)

    @Property(str, notify=filterChanged)
    def filterText(self) -> str:
        return self._filter_text

    @Property(str, notify=filterChanged)
    def statusFilter(self) -> str:
        return self._status_filter

    @Property(str, notify=sortModeChanged)
    def sortMode(self) -> str:
        return self._sort_mode

    @Slot(str)
    def setFilterText(self, value: str) -> None:
        normalized = str(value or "").strip()
        if normalized == self._filter_text:
            return
        self._filter_text = normalized
        self.filterChanged.emit()
        self.refresh()

    @Slot(str)
    def setStatusFilter(self, value: str) -> None:
        normalized = str(value or "all").strip() or "all"
        if normalized == self._status_filter:
            return
        self._status_filter = normalized
        self.filterChanged.emit()
        self.refresh()

    @Slot(str)
    def setSortMode(self, value: str) -> None:
        normalized = str(value or "newest").strip()
        if normalized not in {"newest", "oldest", "title"}:
            normalized = "newest"
        if normalized == self._sort_mode:
            return
        self._sort_mode = normalized
        self.sortModeChanged.emit()
        self.refresh()

    @Slot()
    def refresh(self) -> None:
        ordered = self._ordered_task_ids()
        if ordered == self._task_ids:
            self._sort_states = {
                task_id: self._sort_state(task)
                for task_id in ordered
                if (task := self._service.tasks.get(task_id)) is not None
            }
            return
        previous_ids = set(self._task_ids)
        next_ids = set(ordered)
        if (
            [task_id for task_id in self._task_ids if task_id in next_ids]
            != [task_id for task_id in ordered if task_id in previous_ids]
        ):
            self.beginResetModel()
            self._task_ids = ordered
            self.endResetModel()
        else:
            changes = SequenceMatcher(
                a=self._task_ids, b=ordered, autojunk=False,
            ).get_opcodes()
            # Apply from the end so earlier row ranges keep their original indices.
            for operation, start, end, new_start, new_end in reversed(changes):
                if operation in {"delete", "replace"}:
                    self.beginRemoveRows(QModelIndex(), start, end - 1)
                    del self._task_ids[start:end]
                    self.endRemoveRows()
                if operation in {"insert", "replace"}:
                    self.beginInsertRows(QModelIndex(), start, start + new_end - new_start - 1)
                    self._task_ids[start:start] = ordered[new_start:new_end]
                    self.endInsertRows()
        self._reindex_rows()
        self._sort_states = {
            task_id: self._sort_state(task)
            for task_id in ordered
            if (task := self._service.tasks.get(task_id)) is not None
        }
        self.countChanged.emit()

    @Slot(int, result=str)
    def taskIdAt(self, row: int) -> str:
        return self._task_ids[row] if 0 <= row < len(self._task_ids) else ""

    def _ordered_task_ids(self) -> list[str]:
        tasks = [
            task
            for task in self._service.tasks.values()
            if not task.parent_task_id and self._matches(task)
        ]
        active = [task for task in tasks if self._is_active(task)]
        history = [task for task in tasks if not self._is_active(task)]
        return [
            *(task.id for task in self._sort_tasks(active, newest_first=True)),
            *(task.id for task in self._sort_tasks(history)),
        ]

    def _sort_tasks(
        self,
        tasks: Iterable[DownloadTask],
        *,
        newest_first: bool | None = None,
    ) -> list[DownloadTask]:
        items = list(tasks)
        if newest_first is None:
            newest_first = self._sort_mode != "oldest"
        if self._sort_mode == "title":
            return sorted(
                items,
                key=lambda task: (str(task.title or "").casefold(), str(task.id or "")),
            )
        return sorted(
            items,
            key=lambda task: (str(task.created_at or ""), str(task.id or "")),
            reverse=bool(newest_first),
        )

    def _matches(self, task: DownloadTask) -> bool:
        if self._status_filter == "active" and not self._is_active(task):
            return False
        if self._status_filter not in {"all", "active"}:
            statuses = _STATUS_FILTERS.get(
                self._status_filter,
                frozenset({self._status_filter}),
            )
            if self.display_status(task) not in statuses:
                return False
        needle = self._filter_text.casefold()
        if not needle:
            return True
        return needle in " ".join((
            str(task.id or ""),
            str(task.title or ""),
            str(task.url or ""),
        )).casefold()

    @staticmethod
    def display_status(task: DownloadTask) -> str:
        return "waiting_processing" if task.status == "processing" and task.stage == "waiting_processing" else task.status

    @staticmethod
    def _is_active(task: DownloadTask) -> bool:
        return task.status in _ACTIVE_STATUSES

    def _sort_state(self, task: DownloadTask) -> tuple[bool, str]:
        return (
            self._is_active(task),
            str(task.title or "").casefold() if self._sort_mode == "title" else "",
        )

    def _role_value(self, task: DownloadTask, role: int) -> Any:
        if role == self.StatusRole:
            return self.display_status(task)
        field = self._DIRECT_ROLE_FIELDS.get(role)
        if field is not None:
            return getattr(task, field)
        if role == self.ProgressRole:
            return float(task.progress or 0.0)
        if role == self.StageProgressRole:
            return float(task.stage_progress or 0.0)
        if role == self.SpeedRole:
            return task.speed or task.visible_speed
        if role == self.SpeedBpsRole:
            return float(task.speed_bps or 0.0)
        if role == self.DownloadedBytesRole:
            return int(task.downloaded_bytes or 0)
        if role == self.TotalBytesRole:
            return int(task.total_bytes or 0)
        if role == self.EtaRole:
            return task.eta or task.visible_eta
        if role == self.SizeRole:
            return task.size or task.visible_size
        if role == self.RetryCountRole:
            return int(task.retry_count or 0)
        if role == self.RetryTotalRole:
            return int(task.retry_total or 0)
        if role == self.ActiveRole:
            return self._is_active(task)
        if role == self.TerminalRole:
            return not self._is_active(task)
        if role == self.ThumbnailUrlRole:
            return (
                QUrl.fromLocalFile(str(task.thumbnail_path))
                if task.thumbnail_path else QUrl()
            )
        if role == self.LocalHistoryRole:
            options = task.options_json
            return bool(options.get("_local_history") if hasattr(options, "get") else False)
        return None

    @Slot(object)
    def _on_task_added(self, task: object) -> None:
        if isinstance(task, DownloadTask):
            self._reconcile_task(task.id)

    @Slot(object)
    def _on_tasks_added(self, tasks: object) -> None:
        batch = [task for task in (tasks or []) if isinstance(task, DownloadTask)]
        if any(not task.parent_task_id for task in batch):
            self.refresh()

    @Slot(object)
    def _on_task_updated(self, task: object) -> None:
        if isinstance(task, DownloadTask):
            self._reconcile_task(task.id)

    @Slot(str, object)
    def _on_task_progress(self, task_id: str, _payload: object) -> None:
        self._reconcile_task(str(task_id or ""), progress_only=True)

    @Slot(str)
    def _on_task_deleted(self, task_id: str) -> None:
        task_id = str(task_id or "")
        row = self._rows_by_task_id.get(task_id)
        if row is None:
            return
        self.beginRemoveRows(QModelIndex(), row, row)
        self._task_ids.pop(row)
        self.endRemoveRows()
        self._reindex_rows()
        self._sort_states.pop(task_id, None)
        self.countChanged.emit()

    def _reconcile_task(self, task_id: str, *, progress_only: bool = False) -> None:
        if not task_id:
            return
        task = self._service.tasks.get(task_id)
        old_row = self._rows_by_task_id.get(task_id, -1)
        visible = task is not None and not task.parent_task_id and self._matches(task)
        if old_row < 0:
            if visible:
                self._insert_task(task)
            return
        if not visible:
            self.beginRemoveRows(QModelIndex(), old_row, old_row)
            self._task_ids.pop(old_row)
            self.endRemoveRows()
            self._reindex_rows()
            self._sort_states.pop(task_id, None)
            self.countChanged.emit()
            return
        sort_state = self._sort_state(task)
        if progress_only and self._sort_states.get(task_id) == sort_state:
            index = self.index(old_row, 0)
            self.dataChanged.emit(index, index, self._PROGRESS_ROLES)
            return
        ordered = self._ordered_task_ids()
        new_row = ordered.index(task_id)
        if old_row != new_row:
            # Progress callbacks can also resolve a title or transition a
            # completed task into runtime-only ``processing``.  Reconcile the
            # row order in that same callback so active tasks stay grouped and
            # title sorting does not leave the card at a stale position.
            destination = new_row + 1 if new_row > old_row else new_row
            moved = self.beginMoveRows(
                QModelIndex(),
                old_row,
                old_row,
                QModelIndex(),
                destination,
            )
            if moved:
                task_id_value = self._task_ids.pop(old_row)
                self._task_ids.insert(new_row, task_id_value)
                self.endMoveRows()
            else:
                # Qt can reject a move when a queued batch changed the model
                # between callbacks. Reset from the service's authoritative
                # order rather than emitting an invalid range.
                self.beginResetModel()
                self._task_ids = ordered
                self.endResetModel()
            self._reindex_rows()
            self._sort_states = {
                row_task_id: self._sort_state(row_task)
                for row_task_id in self._task_ids
                if (row_task := self._service.tasks.get(row_task_id)) is not None
            }
            if moved:
                index = self.index(new_row, 0)
                roles = self._PROGRESS_ROLES if progress_only else list(self._ROLE_NAMES)
                self.dataChanged.emit(index, index, roles)
            return
        index = self.index(old_row, 0)
        roles = self._PROGRESS_ROLES if progress_only else list(self._ROLE_NAMES)
        self.dataChanged.emit(index, index, roles)
        self._sort_states[task_id] = sort_state

    def _insert_task(self, task: DownloadTask) -> None:
        ordered = self._ordered_task_ids()
        try:
            row = ordered.index(task.id)
        except ValueError:
            return
        # ``task_added`` is delivered queued to this model.  A pasted batch can
        # therefore reach us after the service already contains every new task:
        # the first item may belong at row 5 while this view still has zero
        # rows.  Qt rejects that insert range and QML only materializes one
        # delegate.  Synchronize once from the authoritative ordered list instead.
        if row > len(self._task_ids):
            self.refresh()
            return
        self.beginInsertRows(QModelIndex(), row, row)
        self._task_ids.insert(row, task.id)
        self.endInsertRows()
        self._reindex_rows()
        self._sort_states[task.id] = self._sort_state(task)
        self.countChanged.emit()

    def _reindex_rows(self) -> None:
        self._rows_by_task_id = {
            task_id: row
            for row, task_id in enumerate(self._task_ids)
        }

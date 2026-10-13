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

from app.storage.models import MediaItem
from app.presentation.distribution_plan import (
    distribution_platform_states,
    distribution_target_platforms,
)
from app.presentation.media_text import (
    format_media_timestamp,
    platform_label,
    publish_status_label,
)


_FILTERS = frozenset({
    "all",
    "needs_distribution",
    "published",
    "queued",
    "retry_needed",
    "complete",
})


class CompletedMediaModel(QAbstractListModel):
    """Expose the complete completed-media catalog to QML without paging."""

    MediaIdRole = Qt.ItemDataRole.UserRole + 1
    TitleRole = MediaIdRole + 1
    UploaderRole = MediaIdRole + 2
    ThumbnailUrlRole = MediaIdRole + 3
    VideoPathRole = MediaIdRole + 4
    SourceUrlRole = MediaIdRole + 5
    SourcePlatformRole = MediaIdRole + 6
    DownloadedAtRole = MediaIdRole + 7
    DistributionTextRole = MediaIdRole + 8
    PublishedRole = MediaIdRole + 9
    QueuedRole = MediaIdRole + 10
    RetryNeededRole = MediaIdRole + 11
    CompleteRole = MediaIdRole + 12

    countChanged = Signal()
    filterChanged = Signal()
    metricsChanged = Signal()
    stateChanged = Signal()

    _ROLE_NAMES = {
        MediaIdRole: b"mediaId",
        TitleRole: b"title",
        UploaderRole: b"uploader",
        ThumbnailUrlRole: b"thumbnailUrl",
        VideoPathRole: b"videoPath",
        SourceUrlRole: b"sourceUrl",
        SourcePlatformRole: b"sourcePlatform",
        DownloadedAtRole: b"downloadedAt",
        DistributionTextRole: b"distributionText",
        PublishedRole: b"published",
        QueuedRole: b"queued",
        RetryNeededRole: b"retryNeeded",
        CompleteRole: b"complete",
    }

    _DISTRIBUTION_ROLES = (
        DistributionTextRole,
        PublishedRole,
        QueuedRole,
        RetryNeededRole,
        CompleteRole,
    )

    def __init__(self, database: Any, settings: Any, parent=None) -> None:
        super().__init__(parent)
        self._database = database
        self._settings = settings
        self._catalog: list[tuple[MediaItem, dict[str, str]]] = []
        self._entries: list[tuple[MediaItem, dict[str, str]]] = []
        self._filter = "all"
        self._search_text = ""
        self._metrics = {name: 0 for name in _FILTERS}
        self._loaded = False
        self._dirty = True
        self._target_platforms = self._configured_target_platforms()

    def roleNames(self) -> dict[int, QByteArray]:  # type: ignore[override]
        return {role: QByteArray(name) for role, name in self._ROLE_NAMES.items()}

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # type: ignore[override]
        return 0 if parent.isValid() else len(self._entries)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # type: ignore[override]
        if not index.isValid() or not 0 <= index.row() < len(self._entries):
            return None
        media, states = self._entries[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return media.title
        if role == self.MediaIdRole:
            return int(media.id or 0)
        if role == self.TitleRole:
            return media.title
        if role == self.UploaderRole:
            return media.uploader
        if role == self.ThumbnailUrlRole:
            return QUrl.fromLocalFile(media.thumbnail_path) if media.thumbnail_path else QUrl()
        if role == self.VideoPathRole:
            return media.video_path
        if role == self.SourceUrlRole:
            return media.source_url
        if role == self.SourcePlatformRole:
            return media.source_platform
        if role == self.DownloadedAtRole:
            return format_media_timestamp(media.downloaded_at)
        if role == self.DistributionTextRole:
            return " · ".join(
                f"{platform_label(platform)}：{publish_status_label(status)}"
                for platform, status in states.items()
            )
        if role == self.PublishedRole:
            return any(status == "success" for status in states.values())
        if role == self.QueuedRole:
            return any(status in {"pending", "uploading"} for status in states.values())
        if role == self.RetryNeededRole:
            return any(status == "failed" for status in states.values())
        if role == self.CompleteRole:
            return bool(self._target_platforms) and all(
                states.get(platform) == "success"
                for platform in self._target_platforms
            )
        return None

    @Property(int, notify=countChanged)
    def count(self) -> int:
        return len(self._entries)

    @Property(str, notify=filterChanged)
    def filter(self) -> str:
        return self._filter

    @Property(str, notify=filterChanged)
    def searchText(self) -> str:
        return self._search_text

    @Property(bool, notify=stateChanged)
    def loaded(self) -> bool:
        return self._loaded

    @Property(bool, notify=stateChanged)
    def dirty(self) -> bool:
        return self._dirty

    @Property(int, notify=metricsChanged)
    def totalCount(self) -> int:
        return self._metrics["all"]

    @Property(int, notify=metricsChanged)
    def needsDistributionCount(self) -> int:
        return self._metrics["needs_distribution"]

    @Property(int, notify=metricsChanged)
    def publishedCount(self) -> int:
        return self._metrics["published"]

    @Property(int, notify=metricsChanged)
    def queuedCount(self) -> int:
        return self._metrics["queued"]

    @Property(int, notify=metricsChanged)
    def retryNeededCount(self) -> int:
        return self._metrics["retry_needed"]

    @Property(int, notify=metricsChanged)
    def completeCount(self) -> int:
        return self._metrics["complete"]

    @Slot(str)
    def setFilter(self, value: str) -> None:
        value = str(value or "all").strip()
        if value not in _FILTERS:
            value = "all"
        if value == self._filter:
            return
        self._filter = value
        self.filterChanged.emit()
        self._apply_view()

    @Slot(str)
    def setSearchText(self, value: str) -> None:
        value = str(value or "").strip()
        if value == self._search_text:
            return
        self._search_text = value
        self.filterChanged.emit()
        self._apply_view()

    @Slot()
    def refresh(self) -> None:
        media_items = self._database.list_media(limit=None)
        media_ids = [int(media.id or 0) for media in media_items if int(media.id or 0) > 0]
        target_platforms = self._configured_target_platforms()
        self._target_platforms = target_platforms
        status_by_media = self._database.publish_statuses_for_media_ids(media_ids)
        self._catalog = [
            (
                media,
                distribution_platform_states(
                    status_by_media.get(int(media.id or 0), {}),
                    target_platforms,
                ),
            )
            for media in media_items
        ]
        metrics = {name: 0 for name in _FILTERS}
        for _media, states in self._catalog:
            flags = self._flags(states, target_platforms)
            metrics["all"] += 1
            for name, active in flags.items():
                if name != "all" and active:
                    metrics[name] += 1
        metrics_changed = metrics != self._metrics
        self._metrics = metrics
        state_changed = not self._loaded or self._dirty
        self._loaded = True
        self._dirty = False
        self._apply_view()
        if metrics_changed:
            self.metricsChanged.emit()
        if state_changed:
            self.stateChanged.emit()

    @Slot()
    def markDirty(self) -> None:
        if self._dirty:
            return
        self._dirty = True
        self.stateChanged.emit()

    def refreshDistributionStatus(self, media_id: int) -> bool:
        """Update one loaded catalog card without resetting every QML delegate."""

        target = int(media_id or 0)
        if target <= 0:
            return False
        if not self._loaded:
            self.markDirty()
            return True
        catalog_index = next(
            (
                index
                for index, (media, _states) in enumerate(self._catalog)
                if int(media.id or 0) == target
            ),
            -1,
        )
        if catalog_index < 0:
            return False
        media, previous_states = self._catalog[catalog_index]
        try:
            states = distribution_platform_states(
                self._database.publish_statuses_for_media(target),
                self._target_platforms,
            )
        except Exception:
            return False
        if states == previous_states:
            return True
        entry = (media, states)
        self._catalog[catalog_index] = entry

        metrics_changed = False
        old_flags = self._flags(previous_states, self._target_platforms)
        new_flags = self._flags(states, self._target_platforms)
        for name in _FILTERS:
            value = self._metrics[name] + int(new_flags[name]) - int(old_flags[name])
            if value != self._metrics[name]:
                self._metrics[name] = max(0, value)
                metrics_changed = True

        entry_index = next(
            (
                index
                for index, (candidate, _states) in enumerate(self._entries)
                if int(candidate.id or 0) == target
            ),
            -1,
        )
        visible = self._matches(
            media,
            states,
            self._search_text.casefold(),
            self._target_platforms,
        )
        if entry_index >= 0 and visible:
            self._entries[entry_index] = entry
            index = self.index(entry_index, 0)
            self.dataChanged.emit(index, index, self._DISTRIBUTION_ROLES)
        elif entry_index >= 0:
            self.beginRemoveRows(QModelIndex(), entry_index, entry_index)
            self._entries.pop(entry_index)
            self.endRemoveRows()
            self.countChanged.emit()
        elif visible:
            row = sum(
                1
                for candidate, candidate_states in self._catalog[:catalog_index]
                if self._matches(
                    candidate,
                    candidate_states,
                    self._search_text.casefold(),
                    self._target_platforms,
                )
            )
            self.beginInsertRows(QModelIndex(), row, row)
            self._entries.insert(row, entry)
            self.endInsertRows()
            self.countChanged.emit()
        if metrics_changed:
            self.metricsChanged.emit()
        return True

    @Slot()
    def refreshDistributionPlan(self) -> None:
        target_platforms = self._configured_target_platforms()
        if target_platforms == self._target_platforms:
            return
        self._target_platforms = target_platforms
        if self._loaded:
            self.refresh()

    def media(self, media_id: int) -> MediaItem | None:
        target = int(media_id or 0)
        return next(
            (media for media, _states in self._catalog if int(media.id or 0) == target),
            None,
        )

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

    def _apply_view(self) -> None:
        needle = self._search_text.casefold()
        entries = [
            (media, states)
            for media, states in self._catalog
            if self._matches(media, states, needle, self._target_platforms)
        ]
        if entries == self._entries:
            return
        self.beginResetModel()
        self._entries = entries
        self.endResetModel()
        self.countChanged.emit()

    def _configured_target_platforms(self) -> tuple[str, ...]:
        return distribution_target_platforms(
            self._settings.get("publish_target_platforms")
        )

    def _matches(
        self,
        media: MediaItem,
        states: dict[str, str],
        needle: str,
        target_platforms: tuple[str, ...],
    ) -> bool:
        if self._filter != "all" and not self._flags(states, target_platforms).get(self._filter):
            return False
        if not needle:
            return True
        searchable = " ".join((
            media.title,
            media.uploader,
            media.video_path,
            media.source_url,
            media.source_platform,
            " ".join(f"{platform} {status}" for platform, status in states.items()),
        )).casefold()
        return needle in searchable

    @staticmethod
    def _flags(
        states: dict[str, str],
        target_platforms: tuple[str, ...] = (),
    ) -> dict[str, bool]:
        success = any(status == "success" for status in states.values())
        queued = any(status in {"pending", "uploading"} for status in states.values())
        retry_needed = any(status == "failed" for status in states.values())
        complete = bool(target_platforms) and all(
            states.get(platform) == "success" for platform in target_platforms
        )
        return {
            "all": True,
            "needs_distribution": not complete,
            "published": success,
            "queued": queued,
            "retry_needed": retry_needed,
            "complete": complete,
        }

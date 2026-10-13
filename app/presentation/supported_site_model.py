from __future__ import annotations

from typing import Any

from PySide6.QtCore import QByteArray, QAbstractListModel, QModelIndex, Property, Qt, Signal, Slot


class SupportedSiteModel(QAbstractListModel):
    """Expose the installed yt-dlp extractor names to a QML search dialog."""

    NameRole = Qt.ItemDataRole.UserRole + 1

    countChanged = Signal()
    filterChanged = Signal()
    stateChanged = Signal()

    _ROLE_NAMES = {NameRole: b"name"}

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._names: list[str] = []
        self._filtered: list[str] = []
        self._filter = ""
        self._loaded = False
        self._error = ""

    def roleNames(self) -> dict[int, QByteArray]:  # type: ignore[override]
        return {role: QByteArray(name) for role, name in self._ROLE_NAMES.items()}

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # type: ignore[override]
        return 0 if parent.isValid() else len(self._filtered)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # type: ignore[override]
        if not index.isValid() or not 0 <= index.row() < len(self._filtered):
            return None
        name = self._filtered[index.row()]
        return name if role in {Qt.ItemDataRole.DisplayRole, self.NameRole} else None

    @Property(int, notify=countChanged)
    def count(self) -> int:
        return len(self._filtered)

    @Property(int, notify=stateChanged)
    def totalCount(self) -> int:
        return len(self._names)

    @Property(bool, notify=stateChanged)
    def loaded(self) -> bool:
        return self._loaded

    @Property(str, notify=stateChanged)
    def error(self) -> str:
        return self._error

    @Property(str, notify=filterChanged)
    def filterText(self) -> str:
        return self._filter

    @Slot()
    def load(self) -> None:
        try:
            from yt_dlp import list_extractor_classes

            names = sorted({
                str(item.IE_NAME).strip()
                for item in list_extractor_classes()
                if str(getattr(item, "IE_NAME", "")).strip()
            }, key=str.casefold)
            self._error = ""
        except Exception as exc:
            names = []
            self._error = str(exc)
        self._loaded = True
        self.beginResetModel()
        self._names = names
        self._filtered = self._matching_names()
        self.endResetModel()
        self.countChanged.emit()
        self.stateChanged.emit()

    @Slot(str)
    def setFilterText(self, value: str) -> None:
        value = str(value or "").strip()
        if value == self._filter:
            return
        self._filter = value
        self.filterChanged.emit()
        self.beginResetModel()
        self._filtered = self._matching_names()
        self.endResetModel()
        self.countChanged.emit()

    def _matching_names(self) -> list[str]:
        needle = self._filter.casefold()
        return [name for name in self._names if not needle or needle in name.casefold()]

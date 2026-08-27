from __future__ import annotations

from collections.abc import Callable, Iterable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QVBoxLayout,
)

from app.ui.i18n import format_text as ui_format
from app.ui.i18n import runtime_text, text as ui_text


def installed_extractor_names() -> list[str]:
    """Return the unique extractor names provided by the active yt-dlp core."""

    from yt_dlp import list_extractor_classes

    return sorted(
        {
            str(extractor_class.IE_NAME).strip()
            for extractor_class in list_extractor_classes()
            if str(getattr(extractor_class, "IE_NAME", "")).strip()
        },
        key=str.casefold,
    )


class SupportedSitesDialog(QDialog):
    """Show and filter the extractors shipped by the installed yt-dlp build."""

    FILTER_DELAY_MS = 120

    def __init__(
        self,
        parent=None,
        *,
        extractor_loader: Callable[[], Iterable[str]] | None = None,
    ):
        super().__init__(parent)
        self._extractor_loader = extractor_loader or installed_extractor_names
        self._load_error = ""

        self.setWindowTitle(ui_text("yt-dlp Supported Sites"))
        self.resize(560, 620)
        layout = QVBoxLayout(self)
        intro = QLabel(
            ui_text(
                "Downloads are not limited to YouTube; supported sites depend "
                "on the installed yt-dlp version."
            )
        )
        intro.setWordWrap(True)
        intro.setObjectName("mutedText")
        layout.addWidget(intro)

        self.search = QLineEdit()
        self.search.setClearButtonEnabled(True)
        self.search.setPlaceholderText(
            ui_text("Search site names, e.g. bilibili, tiktok or instagram")
        )
        layout.addWidget(self.search)
        self.count = QLabel(ui_text("Loading supported sites…"))
        self.count.setObjectName("mutedText")
        layout.addWidget(self.count)
        self.list = QListWidget()
        self.list.setObjectName("supportedSitesList")
        self.list.setAlternatingRowColors(True)
        self.list.setUniformItemSizes(True)
        self.list.setToolTip(
            ui_text("Provided by the currently installed yt-dlp extractor")
        )
        layout.addWidget(self.list, 1)
        close = QPushButton(ui_text("Close"))
        close.clicked.connect(self.accept)
        layout.addWidget(close, 0, Qt.AlignRight)

        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(self.FILTER_DELAY_MS)
        self._filter_timer.timeout.connect(self.apply_filter)
        self.search.textChanged.connect(self._schedule_filter)
        self._load_extractors()

    def _load_extractors(self) -> None:
        try:
            names = sorted(
                {
                    str(name).strip()
                    for name in self._extractor_loader()
                    if name is not None and str(name).strip()
                },
                key=str.casefold,
            )
        except Exception as exc:
            names = []
            self._load_error = runtime_text(exc)

        self.list.setUpdatesEnabled(False)
        try:
            self.list.addItems(names)
        finally:
            self.list.setUpdatesEnabled(True)
        self.apply_filter()

    def _schedule_filter(self, _query: str = "") -> None:
        self._filter_timer.start()

    def apply_filter(self) -> None:
        query = self.search.text().strip().casefold()
        visible = 0
        total = self.list.count()
        for index in range(total):
            item = self.list.item(index)
            matched = not query or query in item.text().casefold()
            item.setHidden(not matched)
            visible += int(matched)

        if self._load_error and not total:
            self.count.setText(
                ui_text("Unable to read the yt-dlp site list: ")
                + self._load_error
            )
        elif query:
            self.count.setText(
                ui_format(
                    "{visible} matches out of {total} extractors",
                    visible=visible,
                    total=total,
                )
            )
        else:
            self.count.setText(
                ui_format(
                    "{count} extractors found in the current version",
                    count=total,
                )
            )

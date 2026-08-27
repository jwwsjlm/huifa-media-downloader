from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QPointF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.core.paths import application_dir
from app.ui.i18n import text as ui_text


_NAVIGATION_ICON_KEYS = {
    "Download Tasks": "download",
    "Accounts": "accounts",
    "Completed": "completed",
    "Publish Queue": "publish",
    "Settings": "settings",
    "About": "about",
    "Publish Editor": "editor",
}


def navigation_icon_key(label: str) -> str:
    """Resolve icons from either source text or the active translation."""

    candidate = str(label or "")
    for source_text, icon_key in _NAVIGATION_ICON_KEYS.items():
        if candidate in {source_text, ui_text(source_text)}:
            return icon_key
    return "page"


def _paint_navigation_icon(key: str, color: str, size: int = 24) -> QPixmap:
    """Paint small dependency-free navigation glyphs for every theme."""

    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(
        QColor(color),
        max(1.6, size * 0.085),
        Qt.SolidLine,
        Qt.RoundCap,
        Qt.RoundJoin,
    )
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)

    if key == "download":
        painter.drawLine(size // 2, 4, size // 2, 15)
        painter.drawLine(size // 2, 15, 7, 10)
        painter.drawLine(size // 2, 15, size - 7, 10)
        painter.drawLine(5, 18, 5, 20)
        painter.drawLine(5, 20, size - 5, 20)
        painter.drawLine(size - 5, 20, size - 5, 18)
    elif key == "accounts":
        painter.drawEllipse(
            QPointF(size * 0.42, size * 0.34),
            size * 0.18,
            size * 0.18,
        )
        painter.drawArc(4, 12, 13, 9, 0, 180 * 16)
        painter.drawEllipse(
            QPointF(size * 0.73, size * 0.42),
            size * 0.13,
            size * 0.13,
        )
        painter.drawArc(13, 13, 8, 7, 0, 180 * 16)
    elif key == "completed":
        painter.drawEllipse(3, 3, size - 6, size - 6)
        painter.drawLine(7, 12, 10, 16)
        painter.drawLine(10, 16, 18, 8)
    elif key == "publish":
        painter.drawLine(4, 12, 20, 5)
        painter.drawLine(20, 5, 15, 20)
        painter.drawLine(4, 12, 12, 14)
        painter.drawLine(12, 14, 15, 20)
        painter.drawLine(12, 14, 20, 5)
    elif key == "settings":
        painter.drawEllipse(7, 7, 10, 10)
        painter.drawEllipse(10, 10, 4, 4)
        for x1, y1, x2, y2 in (
            (12, 2, 12, 6),
            (12, 18, 12, 22),
            (2, 12, 6, 12),
            (18, 12, 22, 12),
            (5, 5, 8, 8),
            (16, 16, 19, 19),
            (19, 5, 16, 8),
            (8, 16, 5, 19),
        ):
            painter.drawLine(x1, y1, x2, y2)
    elif key == "editor":
        painter.drawLine(5, 19, 8, 15)
        painter.drawLine(8, 15, 17, 6)
        painter.drawLine(17, 6, 20, 9)
        painter.drawLine(20, 9, 11, 18)
        painter.drawLine(5, 19, 11, 18)
    elif key == "about":
        painter.drawEllipse(3, 3, size - 6, size - 6)
        painter.drawPoint(size // 2, 8)
        painter.drawLine(size // 2, 11, size // 2, 17)
    elif key in {"chevron_left", "chevron_right"}:
        if key == "chevron_left":
            points = ((15, 6), (9, 12), (15, 18))
        else:
            points = ((9, 6), (15, 12), (9, 18))
        painter.drawLine(*points[0], *points[1])
        painter.drawLine(*points[1], *points[2])
    else:
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(color))
        painter.drawRoundedRect(2, 2, size - 4, size - 4, 6, 6)
        painter.setBrush(Qt.white)
        painter.drawPolygon(
            QPolygonF(
                [
                    QPointF(size * 0.42, size * 0.30),
                    QPointF(size * 0.42, size * 0.70),
                    QPointF(size * 0.72, size * 0.50),
                ]
            )
        )
    painter.end()
    return pixmap


def navigation_icon(key: str) -> QIcon:
    icon = QIcon()
    icon.addPixmap(
        _paint_navigation_icon(key, "#66798f"),
        QIcon.Normal,
        QIcon.Off,
    )
    icon.addPixmap(
        _paint_navigation_icon(key, "#ffffff"),
        QIcon.Normal,
        QIcon.On,
    )
    icon.addPixmap(
        _paint_navigation_icon(key, "#9ba8b8"),
        QIcon.Disabled,
        QIcon.Off,
    )
    return icon


def application_navigation_icon() -> QIcon:
    app = QApplication.instance()
    icon = app.windowIcon() if app is not None else QIcon()
    if not icon.isNull():
        return icon
    runtime_root = Path(getattr(sys, "_MEIPASS", application_dir()))
    candidates = (
        runtime_root / "assets" / "huifa.ico",
        application_dir() / "assets" / "huifa.ico",
        application_dir() / "_internal" / "assets" / "huifa.ico",
    )
    icon_path = next((path for path in candidates if path.is_file()), None)
    return (
        QIcon(str(icon_path))
        if icon_path is not None
        else navigation_icon("brand")
    )


class SidebarNavigation(QWidget):
    """Left-side page navigation with icon-only collapse mode.

    The small QTabWidget-like API keeps page-to-page navigation calls stable
    while allowing a horizontal icon-and-label sidebar.
    """

    currentChanged = Signal(int)
    collapsedChanged = Signal(bool)
    EXPANDED_WIDTH = 196
    COLLAPSED_WIDTH = 66

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("mainNavigation")
        self.setAccessibleName(ui_text("Main navigation"))
        self._buttons: list[QToolButton] = []
        self._collapsed: bool | None = None

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.sidebar = QFrame()
        self.sidebar.setObjectName("mainSidebar")
        sidebar_layout = QVBoxLayout(self.sidebar)
        sidebar_layout.setContentsMargins(10, 12, 10, 10)
        sidebar_layout.setSpacing(8)

        self.navigation_header = QWidget()
        self.navigation_header.setObjectName("navigationHeader")
        header_layout = QHBoxLayout(self.navigation_header)
        header_layout.setContentsMargins(1, 0, 1, 0)
        header_layout.setSpacing(8)
        self.brand_icon = QLabel()
        self.brand_icon.setObjectName("navigationBrandIcon")
        self.brand_icon.setFixedSize(32, 32)
        self.brand_icon.setPixmap(application_navigation_icon().pixmap(28, 28))
        self.brand_icon.setAlignment(Qt.AlignCenter)
        self.brand_label = QLabel(ui_text("Huifa"))
        self.brand_label.setObjectName("navigationBrand")
        self.collapse_button = QToolButton()
        self.collapse_button.setObjectName("navigationCollapseButton")
        self.collapse_button.setCursor(Qt.PointingHandCursor)
        self.collapse_button.setIconSize(QSize(20, 20))
        self.collapse_button.setFixedSize(34, 34)
        self.collapse_button.clicked.connect(self.toggleCollapsed)
        header_layout.addWidget(self.brand_icon)
        header_layout.addWidget(self.brand_label, 1)
        header_layout.addWidget(self.collapse_button, 0, Qt.AlignCenter)
        sidebar_layout.addWidget(self.navigation_header)

        divider = QFrame()
        divider.setObjectName("navigationDivider")
        divider.setFrameShape(QFrame.HLine)
        sidebar_layout.addWidget(divider)

        self._button_layout = QVBoxLayout()
        self._button_layout.setContentsMargins(0, 2, 0, 0)
        self._button_layout.setSpacing(6)
        self._button_layout.addStretch(1)
        sidebar_layout.addLayout(self._button_layout, 1)

        self._stack = QStackedWidget()
        self._stack.setObjectName("mainNavigationStack")
        self._stack.currentChanged.connect(self._on_current_changed)
        root.addWidget(self.sidebar)
        root.addWidget(self._stack, 1)
        self.setCollapsed(False)

    def addTab(self, page: QWidget, label: str, icon: QIcon | None = None) -> int:
        index = self._stack.count()
        key = navigation_icon_key(label)
        button = QToolButton()
        button.setObjectName(f"navigationButton{index}")
        button.setProperty("navigationItem", True)
        button.setCheckable(True)
        button.setAutoExclusive(True)
        button.setCursor(Qt.PointingHandCursor)
        button.setText(str(label))
        button.setIcon(icon or navigation_icon(key))
        button.setIconSize(QSize(22, 22))
        button.setFixedHeight(44)
        button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        button.setAccessibleName(str(label))
        button.setToolTip(str(label))
        button.clicked.connect(
            lambda _checked=False, target=page: self.setCurrentWidget(target)
        )
        self._buttons.append(button)
        self._button_layout.insertWidget(self._button_layout.count() - 1, button)
        actual_index = self._stack.addWidget(page)
        if actual_index == self._stack.currentIndex():
            button.setChecked(True)
        button.setToolButtonStyle(
            Qt.ToolButtonIconOnly
            if self.isCollapsed()
            else Qt.ToolButtonTextBesideIcon
        )
        return actual_index

    def count(self) -> int:
        return self._stack.count()

    def removeTab(self, index: int) -> QWidget | None:
        if not 0 <= index < self._stack.count():
            return None
        page = self._stack.widget(index)
        button = self._buttons.pop(index)
        self._button_layout.removeWidget(button)
        # ``deleteLater`` is intentionally deferred until control returns to
        # Qt's event loop. Hide immediately so a completed dynamic page never
        # leaves a clickable button pointing at an already removed widget.
        button.hide()
        button.setParent(None)
        button.deleteLater()
        if page is not None:
            self._stack.removeWidget(page)
        for button_index, remaining in enumerate(self._buttons):
            remaining.setObjectName(f"navigationButton{button_index}")
            remaining.setChecked(button_index == self._stack.currentIndex())
        return page

    def widget(self, index: int) -> QWidget | None:
        return self._stack.widget(index)

    def indexOf(self, page: QWidget) -> int:
        return self._stack.indexOf(page)

    def currentIndex(self) -> int:
        return self._stack.currentIndex()

    def currentWidget(self) -> QWidget | None:
        return self._stack.currentWidget()

    def setCurrentIndex(self, index: int) -> None:
        if 0 <= index < self._stack.count():
            self._stack.setCurrentIndex(index)

    def setCurrentWidget(self, page: QWidget) -> None:
        index = self._stack.indexOf(page)
        if index >= 0:
            self._stack.setCurrentIndex(index)

    def tabText(self, index: int) -> str:
        return self._buttons[index].text() if 0 <= index < len(self._buttons) else ""

    def navigationButton(self, index: int) -> QToolButton | None:
        return self._buttons[index] if 0 <= index < len(self._buttons) else None

    def isCollapsed(self) -> bool:
        return bool(self._collapsed)

    def setCollapsed(self, collapsed: bool) -> None:
        collapsed = bool(collapsed)
        if self._collapsed is collapsed:
            return
        self._collapsed = collapsed
        self.sidebar.setFixedWidth(
            self.COLLAPSED_WIDTH if collapsed else self.EXPANDED_WIDTH
        )
        self.brand_icon.setVisible(not collapsed)
        self.brand_label.setVisible(not collapsed)
        style = (
            Qt.ToolButtonIconOnly
            if collapsed
            else Qt.ToolButtonTextBesideIcon
        )
        for button in self._buttons:
            button.setToolButtonStyle(style)
            button.setToolTip(button.text())
        if collapsed:
            action_text = ui_text("Expand navigation")
            icon_key = "chevron_right"
        else:
            action_text = ui_text("Collapse navigation")
            icon_key = "chevron_left"
        self.collapse_button.setIcon(navigation_icon(icon_key))
        self.collapse_button.setAccessibleName(action_text)
        self.collapse_button.setToolTip(action_text)
        self.sidebar.setProperty("collapsed", collapsed)
        self.sidebar.style().unpolish(self.sidebar)
        self.sidebar.style().polish(self.sidebar)
        self.collapsedChanged.emit(collapsed)

    def toggleCollapsed(self) -> None:
        self.setCollapsed(not self.isCollapsed())

    def refreshNavigationText(self) -> None:
        for button in self._buttons:
            button.setAccessibleName(button.text())
            button.setToolTip(button.text())
        action_text = (
            ui_text("Expand navigation")
            if self.isCollapsed()
            else ui_text("Collapse navigation")
        )
        self.collapse_button.setAccessibleName(action_text)
        self.collapse_button.setToolTip(action_text)

    def _on_current_changed(self, index: int) -> None:
        for button_index, button in enumerate(self._buttons):
            button.setChecked(button_index == index)
        self.currentChanged.emit(index)


def configure_main_navigation(navigation: SidebarNavigation) -> None:
    """Apply the stable identity and accessibility contract to the sidebar."""

    navigation.setObjectName("mainNavigation")
    navigation.setAccessibleName(ui_text("Main navigation"))

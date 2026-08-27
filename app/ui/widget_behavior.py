from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication, QComboBox, QScrollArea, QWidget


class ExplicitWheelFocusGuard(QObject):
    """Let setting selectors consume the wheel only after an explicit click."""

    _ARMED_PROPERTY = "settingsWheelArmed"

    def __init__(self, scroll_area: QScrollArea, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.scroll_area = scroll_area
        self._owners: dict[QWidget, QWidget] = {}
        app = QApplication.instance()
        if app is not None:
            app.focusChanged.connect(self._focus_changed)

    def watch(self, widget: QWidget) -> None:
        widget.setFocusPolicy(Qt.StrongFocus)
        for watched in (widget, *widget.findChildren(QWidget)):
            self._owners[watched] = widget
            watched.installEventFilter(self)

    @staticmethod
    def _contains_focus(owner: QWidget, focused: QWidget | None) -> bool:
        if focused is owner or (focused is not None and owner.isAncestorOf(focused)):
            return True
        if isinstance(owner, QComboBox) and focused is not None:
            popup = owner.view()
            return focused is popup or popup.isAncestorOf(focused)
        return False

    def _focus_changed(self, _old: QWidget | None, current: QWidget | None) -> None:
        for owner in set(getattr(self, "_owners", {}).values()):
            try:
                if not self._contains_focus(owner, current):
                    owner.setProperty(self._ARMED_PROPERTY, False)
            except RuntimeError:
                continue

    def _forward_to_scroll_area(self, event: QWheelEvent) -> None:
        viewport = self.scroll_area.viewport()
        position = QPointF(viewport.mapFromGlobal(event.globalPosition().toPoint()))
        forwarded = QWheelEvent(
            position,
            event.globalPosition(),
            event.pixelDelta(),
            event.angleDelta(),
            event.buttons(),
            event.modifiers(),
            event.phase(),
            event.inverted(),
            event.source(),
        )
        QApplication.sendEvent(viewport, forwarded)

    def eventFilter(self, watched, event):
        owners = getattr(self, "_owners", None)
        if owners is None:
            return False
        owner = owners.get(watched)
        if owner is None:
            return super().eventFilter(watched, event)
        if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            owner.setProperty(self._ARMED_PROPERTY, True)
        elif event.type() == QEvent.Wheel:
            focused = QApplication.focusWidget()
            if bool(owner.property(self._ARMED_PROPERTY)) and self._contains_focus(owner, focused):
                return False
            self._forward_to_scroll_area(event)
            return True
        return super().eventFilter(watched, event)

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout

from app.ui.i18n import format_text as ui_format
from app.ui.i18n import text as ui_text


class TaskMetricCard(QFrame):
    """Compact keyboard-accessible metric card that also acts as a filter."""

    activated = Signal(str)

    def __init__(
        self,
        caption: str,
        filter_name: str,
        tone: str,
        parent=None,
        *,
        subject: str = "tasks",
    ) -> None:
        super().__init__(parent)
        self.filter_name = filter_name
        self._subject = (
            ui_text(subject, context="metric.subject")
            if subject in {"tasks", "media"}
            else subject
        )
        self.setObjectName("taskMetricCard")
        self.setProperty("tone", tone)
        self.setProperty("active", False)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName(ui_format(
            "Filter {subject} by {caption}",
            caption=caption,
            subject=self._subject,
        ))
        self.setMinimumWidth(108)
        self.setMinimumHeight(64)
        self.setFixedHeight(92)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(0)
        self.value = QLabel("0")
        self.value.setObjectName("taskMetricValue")
        self.value.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.caption = QLabel(caption)
        self.caption.setObjectName("taskMetricCaption")
        self.caption.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        layout.addWidget(self.value)
        layout.addWidget(self.caption)
        self.set_value(0)

    def set_value(self, value: int) -> None:
        count = max(0, int(value))
        self.value.setText(str(count))
        self.setAccessibleDescription(ui_format(
            "{caption}: {count}. Click to filter {subject}",
            caption=self.caption.text(),
            count=count,
            subject=self._subject,
        ))

    def set_active(self, active: bool) -> None:
        active = bool(active)
        if bool(self.property("active")) == active:
            return
        self.setProperty("active", active)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.activated.emit(self.filter_name)
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() in {Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space}:
            self.activated.emit(self.filter_name)
            event.accept()
            return
        super().keyPressEvent(event)

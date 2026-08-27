from __future__ import annotations

import os

from PySide6.QtWidgets import QLineEdit

from app.ui.media_presentation import compact_path_display


def native_path_display(value: str) -> str:
    """Render a portable stored path with the current platform separator."""

    raw = str(value or "").strip()
    if os.name == "nt":
        return raw.replace("/", "\\")
    return raw.replace("\\", "/")


class PortablePathLineEdit(QLineEdit):
    """Display native separators while returning the unchanged stored path."""

    def __init__(self, path: str = "", parent=None):
        super().__init__(parent)
        self._actual_path = str(path or "").strip()
        self._internal_path_change = False
        self.textChanged.connect(self._capture_user_path)
        self._show_native_path()

    def text(self) -> str:
        return self._actual_path

    def setText(self, value: str) -> None:
        self._actual_path = str(value or "").strip()
        self._show_native_path()

    def _show_native_path(self) -> None:
        self._internal_path_change = True
        try:
            super().setText(native_path_display(self._actual_path))
        finally:
            self._internal_path_change = False

    def _capture_user_path(self, value: str) -> None:
        if not self._internal_path_change:
            self._actual_path = str(value or "").strip()


class CompactPathLineEdit(QLineEdit):
    """Show a short path when idle while preserving the configured value."""

    def __init__(self, path: str = "", parent=None):
        super().__init__(parent)
        self._actual_path = str(path or "").strip()
        self._internal_path_change = False
        self.textChanged.connect(self._capture_user_path)
        self._show_compact_path()

    def text(self) -> str:
        return self._actual_path

    def setText(self, value: str) -> None:
        self._actual_path = str(value or "").strip()
        if self.hasFocus():
            self._set_display_text(native_path_display(self._actual_path))
        else:
            self._show_compact_path()

    def _set_display_text(self, value: str) -> None:
        self._internal_path_change = True
        try:
            super().setText(value)
        finally:
            self._internal_path_change = False

    def _show_compact_path(self) -> None:
        self._set_display_text(compact_path_display(self._actual_path))

    def _capture_user_path(self, value: str) -> None:
        if not self._internal_path_change:
            self._actual_path = str(value or "").strip()

    def focusInEvent(self, event) -> None:
        self._set_display_text(native_path_display(self._actual_path))
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:
        if not self._internal_path_change:
            self._actual_path = super().text().strip()
        self._show_compact_path()
        super().focusOutEvent(event)

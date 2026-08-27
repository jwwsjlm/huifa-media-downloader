from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import (
    QApplication,
    QLineEdit,
    QPlainTextEdit,
    QTextEdit,
    QWidget,
)

from app.ui.runtime import ui_text


def text(key: str, *, context: str = "") -> str:
    return ui_text(key, app=QApplication.instance(), context=context)


def format_text(key: str, *, context: str = "", **values: object) -> str:
    """Translate a stable template, then substitute named runtime values."""
    return text(key, context=context).format(**values)


def application_name_text() -> str:
    """Return the localized product name from the normal language pack."""
    return ui_text(
        'Huifa Video Downloader',
        context="application.name",
    )


def runtime_text(value: object) -> str:
    """Return service/adaptor output unchanged for display."""
    return "" if value is None else str(value)


_STANDARD_EDIT_ACTIONS = {
    "undo": "Undo",
    "redo": "Redo",
    "cut": "Cut",
    "copy": "Copy",
    "paste": "Paste",
    "delete": "Delete",
    "select all": "Select All",
}


def translate_standard_edit_menu(menu) -> None:
    """Localize Qt's built-in line-edit actions without changing shortcuts."""
    for action in menu.actions():
        original = action.text()
        label, separator, shortcut_text = original.partition("\t")
        key = label.replace("&", "").strip()
        key = key.removesuffix("...").removesuffix("…").strip().casefold()
        translation_key = _STANDARD_EDIT_ACTIONS.get(key)
        if translation_key is not None:
            localized = ui_text(translation_key)
            action.setText(f"{localized}\t{shortcut_text}" if separator else localized)


_EDITOR_TYPES = (QLineEdit, QTextEdit, QPlainTextEdit)
_editor_polish_filter: QObject | None = None


def install_localized_edit_menu(editor: QLineEdit | QTextEdit | QPlainTextEdit) -> None:
    if bool(editor.property("huifa.localized_context_menu")):
        return
    editor.setProperty("huifa.localized_context_menu", True)
    editor.setContextMenuPolicy(Qt.CustomContextMenu)

    def show_menu(position) -> None:
        menu = editor.createStandardContextMenu()
        translate_standard_edit_menu(menu)
        menu.exec(editor.mapToGlobal(position))
        menu.deleteLater()

    editor.customContextMenuRequested.connect(show_menu)


class _EditorPolishFilter(QObject):
    """Install localized menus on editors created after the main window."""

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() in {QEvent.Type.Polish, QEvent.Type.Show} and isinstance(watched, _EDITOR_TYPES):
            install_localized_edit_menu(watched)
        return False


def _install_editor_polish_filter(app: QApplication) -> None:
    global _editor_polish_filter
    if _editor_polish_filter is not None and _editor_polish_filter.parent() is app:
        return
    _editor_polish_filter = _EditorPolishFilter(app)
    app.installEventFilter(_editor_polish_filter)


def apply_runtime_translation(root: QWidget) -> None:
    """Install localized native edit menus on this widget tree."""
    if QApplication.instance() is None:
        return
    _install_editor_polish_filter(QApplication.instance())
    editors = [
        editor
        for editor_type in _EDITOR_TYPES
        for editor in root.findChildren(editor_type)
    ]
    if isinstance(root, _EDITOR_TYPES):
        editors.insert(0, root)
    for editor in editors:
        install_localized_edit_menu(editor)

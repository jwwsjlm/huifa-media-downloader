from __future__ import annotations

from PySide6.QtWidgets import QApplication

from app.presentation.runtime import ui_text


def text(key: str, *, context: str = "") -> str:
    return ui_text(key, app=QApplication.instance(), context=context)


def format_text(key: str, *, context: str = "", **values: object) -> str:
    """Translate a stable template, then substitute named runtime values."""

    return text(key, context=context).format(**values)


def application_name_text() -> str:
    return ui_text("Huifa Video Downloader", context="application.name")


def runtime_text(value: object) -> str:
    """Return service/adaptor output unchanged for display."""

    return "" if value is None else str(value)

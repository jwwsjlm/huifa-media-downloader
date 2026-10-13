from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtWidgets import QFileDialog, QMessageBox, QWidget

from app.core.diagnostics import (
    collect_diagnostic_summary,
    normalized_diagnostics_target,
)
from app.ui.i18n import format_text as ui_format
from app.ui.i18n import runtime_text, text as ui_text


def export_diagnostics(
    parent: QWidget,
    logs: Any,
    app_settings: Any,
    database: Callable[[], Any],
) -> None:
    default_name = str(logs.root.parent / "diagnostics.zip")
    target, _selected_filter = QFileDialog.getSaveFileName(
        parent,
        ui_text("Export Diagnostics"),
        default_name,
        ui_text("ZIP Archives (*.zip)"),
    )
    if not target:
        return

    try:
        summary = collect_diagnostic_summary(app_settings, database)
        bundle = logs.export_bundle(normalized_diagnostics_target(target), summary)
    except Exception as exc:
        QMessageBox.warning(
            parent,
            ui_text("Export Failed"),
            ui_format(
                "Unable to export diagnostics:\n{error}",
                error=runtime_text(exc),
            ),
        )
        return

    QMessageBox.information(
        parent,
        ui_text("Export Complete"),
        ui_format("Diagnostic bundle saved to:\n{path}", path=bundle),
    )

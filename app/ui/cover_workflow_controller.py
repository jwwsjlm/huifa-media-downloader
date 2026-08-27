from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QWidget

from app.core.cover_service import (
    CoverExportOptions,
    CoverFitMode,
    CoverPresetId,
    CoverServiceError,
)
from app.storage.models import MediaItem
from app.ui.cover_export_paths import (
    default_cover_export_path,
    normalized_jpeg_target,
)
from app.ui.cover_studio import CoverStudioDialog
from app.ui.i18n import format_text as ui_format
from app.ui.i18n import runtime_text, text as ui_text
from app.ui.media_presentation import format_file_size


def cover_options_from_settings(settings: Any) -> CoverExportOptions:
    preset = settings.get("cover_preset") or CoverPresetId.LANDSCAPE_16_9.value
    fit_mode = settings.get("cover_fit_mode") or CoverFitMode.CROP.value
    quality = settings.get_int("cover_jpeg_quality", 90, 50, 100)
    focus_x = settings.get_int("cover_focus_x", 50, 0, 100) / 100.0
    focus_y = settings.get_int("cover_focus_y", 50, 0, 100) / 100.0
    try:
        return CoverExportOptions.from_preset(
            preset,
            quality=quality,
            fit_mode=fit_mode,
            focus_x=focus_x,
            focus_y=focus_y,
        )
    except CoverServiceError:
        # A hand-edited or partially written settings file must not make the
        # completed-media cover actions unusable. Keep valid quality/focus
        # values and fall back only the invalid enum-like choices.
        return CoverExportOptions.from_preset(
            CoverPresetId.LANDSCAPE_16_9,
            quality=quality,
            fit_mode=CoverFitMode.CROP,
            focus_x=focus_x,
            focus_y=focus_y,
        )


class CoverWorkflowController(QObject):
    """Own completed-media cover actions and their common validation."""

    def __init__(self, parent: QWidget, app_settings: Any, service: Any) -> None:
        super().__init__(parent)
        self._dialog_parent = parent
        self.app_settings = app_settings
        self.service = service

    def _local_cover_available(self, media: MediaItem) -> bool:
        if media.thumbnail_path and Path(media.thumbnail_path).is_file():
            return True
        QMessageBox.information(
            self._dialog_parent,
            ui_text("No Cover Available"),
            ui_text("The current video has no local cover image."),
        )
        return False

    def open_studio(self, media: MediaItem) -> None:
        if not self._local_cover_available(media):
            return
        try:
            CoverStudioDialog(media, self._dialog_parent).exec()
        except (CoverServiceError, RuntimeError) as exc:
            QMessageBox.warning(
                self._dialog_parent,
                ui_text("Cannot Open Cover Studio"),
                runtime_text(exc),
            )

    def copy_to_clipboard(self, media: MediaItem):
        if not self._local_cover_available(media):
            return None
        try:
            source = self.service.load_local(media.thumbnail_path)
            clipboard_data = self.service.prepare_clipboard(
                source,
                cover_options_from_settings(self.app_settings),
            )
            QApplication.clipboard().setMimeData(clipboard_data.to_mime_data())
            return clipboard_data
        except CoverServiceError as exc:
            QMessageBox.warning(
                self._dialog_parent,
                ui_text("Copy Failed"),
                runtime_text(exc),
            )
            return None

    def save_as_jpeg(self, media: MediaItem) -> None:
        if not self._local_cover_available(media):
            return
        options = cover_options_from_settings(self.app_settings)
        default = default_cover_export_path(
            media,
            width=options.width,
            height=options.height,
        )
        target, _selected_filter = QFileDialog.getSaveFileName(
            self._dialog_parent,
            ui_text("Save JPG Cover"),
            str(default),
            ui_text("JPG Images (*.jpg *.jpeg)"),
        )
        if not target:
            return
        try:
            target_path = normalized_jpeg_target(target)
            source = self.service.load_local(media.thumbnail_path)
            result = self.service.save_jpeg(
                source,
                target_path,
                options,
            )
        except (CoverServiceError, OSError, ValueError) as exc:
            QMessageBox.warning(
                self._dialog_parent,
                ui_text("Save Failed"),
                runtime_text(exc),
            )
            return
        QMessageBox.information(
            self._dialog_parent,
            ui_text("Cover Saved"),
            ui_format(
                "JPG cover saved:\n{path}\n\n{width}×{height} · {size}",
                path=result.path,
                width=result.width,
                height=result.height,
                size=format_file_size(result.byte_size),
            ),
        )


__all__ = [
    "CoverWorkflowController",
    "cover_options_from_settings",
]

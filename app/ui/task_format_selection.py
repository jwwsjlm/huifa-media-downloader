from __future__ import annotations

from typing import Any, Mapping

from PySide6.QtWidgets import QDialog, QLabel, QMessageBox, QWidget

from app.core.download_options import DownloadOptions
from app.ui.download_dialogs import FormatSelectionDialog
from app.ui.i18n import format_text as ui_format
from app.ui.i18n import runtime_text, text as ui_text


class TaskFormatSelectionController:
    """Own the modal manual-format selection workflow for download tasks."""

    def __init__(
        self,
        parent: QWidget,
        service: Any,
        status_label: QLabel,
    ) -> None:
        self._parent_widget = parent
        self._service = service
        self._status_label = status_label

    def choose(self, task_id: str, payload: Mapping[str, Any]) -> None:
        service = self._service
        task = service.tasks.get(task_id)
        if task is None:
            return

        raw_choices = payload.get("choices")
        choices = (
            [choice for choice in raw_choices if isinstance(choice, Mapping)]
            if isinstance(raw_choices, (list, tuple))
            else []
        )
        if not choices:
            QMessageBox.warning(
                self._parent_widget,
                ui_text('Cannot Select Resolution'),
                ui_text('No usable video resolution was found.'),
            )
            service.set_format_selector(task_id, "")
            return

        options = DownloadOptions.from_mapping(task.options_json)
        dialog = FormatSelectionDialog(
            task.title or payload.get("title") or ui_text('Select Download Format'),
            payload.get("thumbnail_path") or task.thumbnail_path,
            choices,
            self._parent_widget,
            default_content_mode=str(
                payload.get('content_mode') or options.content_mode
            ),
            default_audio_format=str(
                payload.get('audio_format') or options.audio_format
            ),
        )
        if dialog.exec() != QDialog.Accepted:
            self._discard_preview(task_id)
            return

        choice = dialog.selected_choice()
        if not choice:
            self._discard_preview(task_id)
            return
        try:
            applied = service.set_format_selection(task_id, choice)
        except Exception as exc:
            QMessageBox.warning(
                self._parent_widget,
                ui_text('Cannot Select Resolution'),
                runtime_text(exc),
            )
            return
        if applied is False:
            QMessageBox.warning(
                self._parent_widget,
                ui_text('Format Selection Expired'),
                ui_text(
                    'The task is no longer waiting for format selection. Submit the link again if needed.'
                ),
            )
            self._status_label.setText(ui_text('Format selection expired'))
            return

        if choice.get('content_mode') == 'audio':
            self._status_label.setText(
                ui_text('Audio format selected; starting download')
            )
        else:
            self._status_label.setText(ui_format(
                'Selected {height}p; starting download',
                height=choice.get("height", ""),
            ))

    def _discard_preview(self, task_id: str) -> None:
        try:
            self._service.discard_task(task_id)
        except Exception as exc:
            QMessageBox.warning(
                self._parent_widget,
                ui_text('Cannot Select Resolution'),
                runtime_text(exc),
            )
            return
        self._status_label.setText(ui_text(
            'Quality preview closed; no download task was created',
        ))

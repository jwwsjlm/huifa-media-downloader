from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, Slot
from PySide6.QtWidgets import QMessageBox, QWidget

from app.ui.i18n import format_text as ui_format
from app.ui.i18n import runtime_text, text as ui_text
from app.ui.runtime_components_dialog import UpdateDialog


class RuntimeUpdateDialogController(QObject):
    """Coordinate the modal all-components update check without signal leaks."""

    def __init__(
        self,
        parent: QWidget,
        service: Any,
        set_update_status: Callable[[str], None],
        set_settings_status: Callable[[str], None],
    ) -> None:
        super().__init__(parent)
        self._parent_widget = parent
        self._service = service
        self._set_update_status = set_update_status
        self._set_settings_status = set_settings_status
        self._request_active = False
        self._starting = False
        self._inline_result = False
        service.finished.connect(self.results_ready)
        service.failed.connect(self.failed)

    def check(self) -> None:
        service = self._service
        service.start_background_route_probe()
        self._request_active = True
        if service.runtime_active("check"):
            self._set_update_status(ui_text(
                'A fast check is already running; available results appear immediately while remaining routes are tested in the background.',
            ))
            self._set_settings_status(ui_text('Checking runtime components…'))
            return

        self._set_update_status(ui_text(
            'Checking official GitHub first; available results appear immediately while remaining routes are tested in the background.',
        ))
        self._set_settings_status(ui_text('Checking runtime components…'))
        self._inline_result = False
        self._starting = True
        try:
            started = service.check("")
        except Exception as exc:
            self._request_active = False
            QMessageBox.warning(
                self._parent_widget,
                ui_text('Update Check Failed'),
                runtime_text(exc),
            )
            return
        finally:
            self._starting = False
        if self._inline_result:
            return
        if not started:
            self._request_active = False
            self._set_update_status(ui_text(
                'Unable to start the check. Try again later.',
            ))

    def request_shutdown(self) -> None:
        self._request_active = False

    @Slot(object)
    def results_ready(self, results: object) -> None:
        if not self._request_active:
            return
        if self._starting:
            self._inline_result = True
        self._request_active = False
        rows = results if isinstance(results, list) else []
        updates = sum(1 for result in rows if result.get("has_update"))
        managed_updates = sum(
            1 for result in rows if result.get("upstream_update_available")
        )
        installs = sum(1 for result in rows if result.get("install_available"))
        failures = sum(1 for result in rows if result.get("error"))
        parts = [ui_format('{count} external component updates', count=updates)]
        if managed_updates:
            parts.append(ui_format(
                '{count} bundled cores have upstream updates (update the app)',
                count=managed_updates,
            ))
        if installs:
            parts.append(ui_format(
                '{count} components can be installed automatically',
                count=installs,
            ))
        if failures:
            parts.append(ui_format('{count} checks failed', count=failures))
        self._set_update_status(
            ui_text('Check complete: ') + ui_text(', ').join(parts)
        )
        UpdateDialog(rows, self._service, self._parent_widget).exec()

    @Slot(str)
    def failed(self, error: str) -> None:
        if not self._request_active:
            return
        if self._starting:
            self._inline_result = True
        self._request_active = False
        self._set_update_status(ui_text(
            'Check failed. Verify the network connection and try again.',
        ))
        QMessageBox.warning(
            self._parent_widget,
            ui_text('Update Check Failed'),
            ui_text('Unable to connect to the GitHub API:\n') + runtime_text(error),
        )


__all__ = ["RuntimeUpdateDialogController"]

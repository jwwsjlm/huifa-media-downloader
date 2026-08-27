from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QWidget

from app.core.application_updater import ApplicationUpdate
from app.ui.application_update_controller import (
    ApplicationUpdateController,
)


class _Signal:
    def __init__(self) -> None:
        self.slots = []

    def connect(self, slot) -> None:
        self.slots.append(slot)

    def emit(self, *args) -> None:
        for slot in tuple(self.slots):
            slot(*args)


class _Settings:
    def __init__(self, **values: str) -> None:
        self.values = {
            "update_repo": "owner/project",
            "update_prerelease": "false",
            "update_channel": "",
            "auto_check_updates": "true",
            **values,
        }

    def get(self, key: str) -> str:
        return str(self.values.get(key, ""))

    def get_bool(self, key: str, default: bool = False) -> bool:
        value = self.get(key).strip().casefold()
        if not value:
            return default
        return value in {"1", "true", "yes", "on"}


class _Service:
    def __init__(self) -> None:
        self.update_available = _Signal()
        self.no_update = _Signal()
        self.pending_restart_available = _Signal()
        self.no_pending_restart = _Signal()
        self.failed = _Signal()
        self.busy_changed = _Signal()
        self.busy = False
        self.current_update = None
        self.check_result = True
        self.restore_result = True
        self.auto_due = True
        self.configure_calls: list[tuple[str, dict[str, object]]] = []
        self.check_calls: list[bool] = []
        self.restore_calls = 0
        self.install_calls: list[tuple[ApplicationUpdate, bool]] = []

    def configure(self, repository: str, **kwargs) -> None:
        self.configure_calls.append((repository, kwargs))

    def check(self, *, automatic: bool = False) -> bool:
        self.check_calls.append(automatic)
        return self.check_result

    def restore_pending_restart(self) -> bool:
        self.restore_calls += 1
        return self.restore_result

    def is_auto_check_due(self) -> bool:
        return self.auto_due

    def schedule_install_and_restart(
        self,
        update: ApplicationUpdate,
        *,
        confirmed: bool,
    ) -> None:
        self.install_calls.append((update, confirmed))


def _update(*, downloaded: bool = False) -> ApplicationUpdate:
    return ApplicationUpdate(
        token="update-token",
        current_version="1.0.0",
        version="2.0.0",
        package_id="huifa",
        file_name="app.exe",
        size_bytes=1024,
        sha256="a" * 64,
        release_notes_markdown="notes",
        is_downgrade=False,
        is_portable=True,
        downloaded=downloaded,
    )


class ApplicationUpdateControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        existing = QCoreApplication.instance()
        if existing is not None and not isinstance(existing, QApplication):
            raise unittest.SkipTest("a non-GUI QCoreApplication already exists")
        cls.app = existing or QApplication([])

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.parent = QWidget()
        self.status_label = QLabel()
        self.check_button = QPushButton()
        self.service = _Service()
        self.settings = _Settings()
        self.statuses: list[tuple[str, int]] = []
        self.close_calls: list[bool] = []
        self.parent.application_update_service = self.service
        self.parent.app_settings = self.settings
        self.parent.settings = SimpleNamespace(
            application_update_status=self.status_label,
            application_update_button=self.check_button,
        )
        self.parent.application_update_dir = Path(self.temp.name)
        self.parent.application_updates_supported = True
        self.parent.application_version = "1.0.0"
        self.parent.shutdown_controller = SimpleNamespace(started=False)
        self.parent.statusBar = lambda: SimpleNamespace(
            showMessage=lambda message, timeout=0: self.statuses.append(
                (message, timeout)
            )
        )
        self.parent.close = lambda: self.close_calls.append(True)
        self.controller = ApplicationUpdateController(self.parent)

    def tearDown(self) -> None:
        self.controller.request_shutdown()
        self.parent.close()
        self.parent.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()

    def test_manual_check_configures_and_starts_service(self) -> None:
        self.controller.check()

        self.assertEqual(self.service.configure_calls[0][0], "owner/project")
        self.assertEqual(self.service.check_calls, [False])
        self.assertIn("GitHub", self.status_label.text())

    def test_failed_auto_start_does_not_suppress_later_manual_result(self) -> None:
        self.service.check_result = False
        self.controller.auto_check()
        self.assertFalse(self.controller._automatic)

        with patch(
            "app.ui.application_update_controller.QMessageBox.information"
        ) as information:
            self.controller.no_update()

        information.assert_called_once()

    def test_shutdown_stops_owned_auto_check_timer(self) -> None:
        self.controller.schedule_auto_check()
        self.assertTrue(self.controller._auto_check_timer.isActive())

        self.controller.request_shutdown()

        self.assertFalse(self.controller._auto_check_timer.isActive())

    def test_shutdown_does_not_consume_an_unshown_install_receipt(self) -> None:
        self.parent.shutdown_controller.started = True

        with patch(
            "app.ui.application_update_controller.consume_update_install_receipt"
        ) as consume:
            self.controller.show_install_result_once()

        consume.assert_not_called()

    def test_late_update_result_does_not_open_dialog_during_shutdown(self) -> None:
        update = _update()
        with patch(
            "app.ui.application_update_controller.ApplicationUpdateDialog"
        ) as dialog:
            self.controller.update_available(update)
            self.parent.shutdown_controller.started = True
            self.app.processEvents()

        dialog.assert_not_called()

    def test_restore_start_failure_schedules_auto_check(self) -> None:
        self.service.restore_result = False

        self.controller.restore_pending()

        self.assertEqual(self.service.restore_calls, 1)
        self.assertFalse(self.controller._restoring)
        self.assertTrue(self.controller._auto_check_timer.isActive())

    def test_install_schedules_replacement_then_closes_on_event_loop(self) -> None:
        update = _update(downloaded=True)

        self.controller.install(update, confirmed=True)
        self.assertEqual(self.service.install_calls, [(update, True)])
        self.assertEqual(self.close_calls, [])
        self.app.processEvents()

        self.assertEqual(self.close_calls, [True])
        self.assertTrue(self.statuses)

    def test_busy_state_controls_application_update_button(self) -> None:
        self.controller.busy_changed(True)
        self.assertFalse(self.check_button.isEnabled())

        self.controller.busy_changed(False)
        self.assertTrue(self.check_button.isEnabled())


if __name__ == "__main__":
    unittest.main()

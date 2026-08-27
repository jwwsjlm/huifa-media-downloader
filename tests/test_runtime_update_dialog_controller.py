from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QWidget

from app.ui.runtime_update_dialog_controller import (
    RuntimeUpdateDialogController,
)


class _Signal:
    def __init__(self) -> None:
        self.slots = []

    def connect(self, slot) -> None:
        self.slots.append(slot)

    def emit(self, *args) -> None:
        for slot in tuple(self.slots):
            slot(*args)


class _Service:
    def __init__(self) -> None:
        self.finished = _Signal()
        self.failed = _Signal()
        self.active_runtimes: set[str] = set()
        self.check_result = True
        self.check_error: Exception | None = None
        self.inline_failure = ""
        self.check_calls: list[str] = []
        self.route_probe_calls = 0

    def start_background_route_probe(self) -> None:
        self.route_probe_calls += 1

    def runtime_active(self, *kinds: str) -> bool:
        return any(kind in self.active_runtimes for kind in kinds)

    def check(self, app_repo: str) -> bool:
        self.check_calls.append(app_repo)
        if self.check_error is not None:
            raise self.check_error
        if self.inline_failure:
            self.failed.emit(self.inline_failure)
        return self.check_result


class RuntimeUpdateDialogControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        existing = QCoreApplication.instance()
        if existing is not None and not isinstance(existing, QApplication):
            raise unittest.SkipTest("a non-GUI QCoreApplication already exists")
        cls.app = existing or QApplication([])

    def setUp(self) -> None:
        self.parent = QWidget()
        self.service = _Service()
        self.update_statuses: list[str] = []
        self.settings_statuses: list[str] = []
        self.controller = RuntimeUpdateDialogController(
            self.parent,
            self.service,
            self.update_statuses.append,
            self.settings_statuses.append,
        )

    def tearDown(self) -> None:
        self.controller.request_shutdown()
        self.parent.close()
        self.parent.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()

    def test_existing_check_is_adopted_without_starting_a_second_thread(self) -> None:
        self.service.active_runtimes.add("check")

        self.controller.check()

        self.assertTrue(self.controller._request_active)
        self.assertEqual(self.service.check_calls, [])
        self.assertEqual(self.service.route_probe_calls, 1)

    def test_unrelated_service_results_do_not_open_modal_dialog(self) -> None:
        with patch(
            "app.ui.runtime_update_dialog_controller.UpdateDialog"
        ) as dialog, patch(
            "app.ui.runtime_update_dialog_controller.QMessageBox.warning"
        ) as warning:
            self.service.finished.emit([])
            self.service.failed.emit("unrelated failure")

        dialog.assert_not_called()
        warning.assert_not_called()
        self.assertEqual(self.update_statuses, [])

    def test_success_consumes_request_and_later_failure_is_ignored(self) -> None:
        results = [{"name": "Deno", "has_update": True}]
        self.controller.check()

        with patch(
            "app.ui.runtime_update_dialog_controller.UpdateDialog"
        ) as dialog, patch(
            "app.ui.runtime_update_dialog_controller.QMessageBox.warning"
        ) as warning:
            self.service.finished.emit(results)
            self.service.failed.emit("later settings-page failure")

        dialog.assert_called_once()
        warning.assert_not_called()
        self.assertFalse(self.controller._request_active)

    def test_failure_consumes_request_and_later_success_is_ignored(self) -> None:
        self.controller.check()

        with patch(
            "app.ui.runtime_update_dialog_controller.UpdateDialog"
        ) as dialog, patch(
            "app.ui.runtime_update_dialog_controller.QMessageBox.warning"
        ) as warning:
            self.service.failed.emit("network unavailable")
            self.service.finished.emit([])

        warning.assert_called_once()
        dialog.assert_not_called()
        self.assertFalse(self.controller._request_active)

    def test_inline_start_failure_shows_only_the_precise_failure(self) -> None:
        self.service.check_result = False
        self.service.inline_failure = "thread start failed"

        with patch(
            "app.ui.runtime_update_dialog_controller.QMessageBox.warning"
        ) as warning:
            self.controller.check()

        warning.assert_called_once()
        self.assertFalse(self.controller._request_active)
        self.assertIn("thread start failed", warning.call_args.args[-1])
        self.assertNotEqual(
            self.update_statuses[-1],
            "Unable to start the check. Try again later.",
        )

    def test_direct_start_exception_restores_idle_state(self) -> None:
        self.service.check_error = RuntimeError("cannot allocate thread")

        with patch(
            "app.ui.runtime_update_dialog_controller.QMessageBox.warning"
        ) as warning:
            self.controller.check()

        self.assertFalse(self.controller._request_active)
        self.assertIn("cannot allocate thread", warning.call_args.args[-1])

    def test_shutdown_ignores_late_result(self) -> None:
        self.controller.check()
        self.controller.request_shutdown()

        with patch(
            "app.ui.runtime_update_dialog_controller.UpdateDialog"
        ) as dialog:
            self.service.finished.emit([])

        dialog.assert_not_called()


if __name__ == "__main__":
    unittest.main()

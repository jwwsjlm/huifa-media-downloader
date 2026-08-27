from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QComboBox, QLineEdit, QPushButton, QWidget

from app.core.cookie_sources import COOKIE_SOURCE_EMBEDDED, COOKIE_SOURCE_NONE
from app.ui.download_cookie_controller import DownloadCookieController


class _Settings:
    def __init__(self) -> None:
        self.values = {"download_cookie_source": COOKIE_SOURCE_NONE}
        self.sync_count = 0
        self.error: Exception | None = None

    def set_many(self, values: dict[str, str]) -> None:
        if self.error is not None:
            raise self.error
        self.values.update({key: str(value) for key, value in values.items()})
        self.sync_count += 1


class _PublishService:
    def __init__(self, *, started: bool = True, running: bool = False) -> None:
        self.started = started
        self.running = running
        self.error: Exception | None = None
        self.before_return = None
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def run_account_action(self, *args, **kwargs) -> bool:
        self.calls.append((args, kwargs))
        if self.error is not None:
            raise self.error
        if callable(self.before_return):
            self.before_return()
        return self.started

    def is_account_action_running(self, platform: str, account: str) -> bool:
        return self.running and (platform, account) == ("browser", "download")


class DownloadCookieControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        existing = QCoreApplication.instance()
        if existing is not None and not isinstance(existing, QApplication):
            raise unittest.SkipTest("a non-GUI QCoreApplication already exists")
        cls.app = existing or QApplication([])

    def setUp(self) -> None:
        self.parent = QWidget()

    def tearDown(self) -> None:
        self.parent.close()
        self.parent.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()

    def _controller(
        self,
        service: _PublishService,
    ) -> tuple[
        DownloadCookieController,
        QComboBox,
        QPushButton,
        _Settings,
        list[str],
        list[bool],
    ]:
        source = QComboBox()
        source.addItem("None", COOKIE_SOURCE_NONE)
        source.addItem("Embedded", COOKIE_SOURCE_EMBEDDED)
        login_button = QPushButton("Open login page")
        settings = _Settings()
        statuses: list[str] = []
        refreshed: list[bool] = []
        window = SimpleNamespace(
            app_settings=settings,
            dashboard=SimpleNamespace(
                refresh_settings=lambda: refreshed.append(True),
            ),
            publish_service=service,
            run_sau_account_action=service.run_account_action,
            settings_status=lambda message: statuses.append(message),
        )
        page = self.parent
        page.window = window
        page.download_cookie_source = source
        page.download_cookie_browser = QComboBox()
        page.download_cookie_profile = QLineEdit()
        page.download_cookie_keyring = QLineEdit()
        page.download_cookie_container = QLineEdit()
        page.download_cookie_file = QLineEdit()
        page.open_cookie_login_button = login_button
        controller = DownloadCookieController(page)
        return controller, source, login_button, settings, statuses, refreshed

    def test_start_exception_restores_controls_and_keeps_source(self) -> None:
        service = _PublishService()
        service.error = RuntimeError("start failed")
        controller, source, button, settings, _statuses, _refreshed = (
            self._controller(service)
        )

        with patch(
            "app.ui.download_cookie_controller.QMessageBox.warning"
        ) as warning:
            controller.open_login()

        self.assertEqual(source.currentData(), COOKIE_SOURCE_NONE)
        self.assertTrue(button.isEnabled())
        self.assertEqual(settings.sync_count, 0)
        self.assertIn("start failed", str(warning.call_args.args[-1]))

    def test_busy_account_action_gets_the_specific_running_notice(self) -> None:
        service = _PublishService(started=False, running=True)
        controller, source, button, settings, _statuses, _refreshed = (
            self._controller(service)
        )

        with (
            patch(
                "app.ui.download_cookie_controller.QMessageBox.information"
            ) as information,
            patch(
                "app.ui.download_cookie_controller.QMessageBox.warning"
            ) as warning,
        ):
            controller.open_login()

        self.assertEqual(source.currentData(), COOKIE_SOURCE_NONE)
        self.assertTrue(button.isEnabled())
        self.assertEqual(settings.sync_count, 0)
        information.assert_called_once()
        warning.assert_not_called()

    def test_unavailable_start_is_not_misreported_as_already_running(self) -> None:
        service = _PublishService(started=False, running=False)
        controller, source, button, settings, _statuses, _refreshed = (
            self._controller(service)
        )

        with (
            patch(
                "app.ui.download_cookie_controller.QMessageBox.information"
            ) as information,
            patch(
                "app.ui.download_cookie_controller.QMessageBox.warning"
            ) as warning,
        ):
            controller.open_login()

        self.assertEqual(source.currentData(), COOKIE_SOURCE_NONE)
        self.assertTrue(button.isEnabled())
        self.assertEqual(settings.sync_count, 0)
        information.assert_not_called()
        warning.assert_called_once()

    def test_inline_start_failure_does_not_show_a_second_dialog(self) -> None:
        service = _PublishService(started=False)
        controller, source, button, settings, statuses, _refreshed = (
            self._controller(service)
        )
        service.before_return = lambda: controller.login_result(
            "browser",
            "download",
            "login",
            False,
            "runtime preparation failed",
        )

        with (
            patch(
                "app.ui.download_cookie_controller.QMessageBox.information"
            ) as information,
            patch(
                "app.ui.download_cookie_controller.QMessageBox.warning"
            ) as warning,
        ):
            controller.open_login()

        self.assertEqual(source.currentData(), COOKIE_SOURCE_NONE)
        self.assertTrue(button.isEnabled())
        self.assertEqual(settings.sync_count, 0)
        self.assertEqual(len(statuses), 1)
        information.assert_not_called()
        warning.assert_called_once()
        self.assertIn("runtime preparation failed", str(warning.call_args.args[-1]))

    def test_unrelated_result_does_not_touch_login_state(self) -> None:
        service = _PublishService()
        controller, source, button, settings, statuses, refreshed = (
            self._controller(service)
        )
        button.setEnabled(False)
        button.setText("Signing in")

        controller.login_result(
            "youtube",
            "default",
            "check",
            True,
            "ok",
        )

        self.assertEqual(source.currentData(), COOKIE_SOURCE_NONE)
        self.assertFalse(button.isEnabled())
        self.assertEqual(button.text(), "Signing in")
        self.assertEqual(settings.sync_count, 0)
        self.assertEqual(statuses, [])
        self.assertEqual(refreshed, [])

    def test_failed_login_does_not_persist_embedded_cookie_source(self) -> None:
        service = _PublishService()
        controller, source, button, settings, statuses, refreshed = (
            self._controller(service)
        )
        button.setEnabled(False)

        with patch(
            "app.ui.download_cookie_controller.QMessageBox.warning"
        ) as warning:
            controller.login_result(
                "browser",
                "download",
                "login",
                False,
                "no cookies",
            )

        self.assertEqual(source.currentData(), COOKIE_SOURCE_NONE)
        self.assertTrue(button.isEnabled())
        self.assertEqual(settings.sync_count, 0)
        self.assertEqual(len(statuses), 1)
        self.assertEqual(refreshed, [])
        warning.assert_called_once()

    def test_success_persists_source_before_updating_controls(self) -> None:
        service = _PublishService()
        controller, source, button, settings, statuses, refreshed = (
            self._controller(service)
        )
        button.setEnabled(False)

        controller.login_result(
            "browser",
            "download",
            "login",
            True,
            "ok",
        )

        self.assertEqual(source.currentData(), COOKIE_SOURCE_EMBEDDED)
        self.assertTrue(button.isEnabled())
        self.assertEqual(
            settings.values["download_cookie_source"],
            COOKIE_SOURCE_EMBEDDED,
        )
        self.assertEqual(settings.sync_count, 1)
        self.assertEqual(statuses, [])
        self.assertEqual(refreshed, [True])

    def test_source_stays_unchanged_when_settings_write_fails(self) -> None:
        service = _PublishService()
        controller, source, button, settings, _statuses, refreshed = (
            self._controller(service)
        )
        settings.error = OSError("settings disk unavailable")
        button.setEnabled(False)

        with patch(
            "app.ui.download_cookie_controller.QMessageBox.warning"
        ) as warning:
            controller.login_result(
                "browser",
                "download",
                "login",
                True,
                "ok",
            )

        self.assertEqual(source.currentData(), COOKIE_SOURCE_NONE)
        self.assertTrue(button.isEnabled())
        self.assertEqual(settings.sync_count, 0)
        self.assertEqual(refreshed, [])
        warning.assert_called_once()
        self.assertIn("settings disk unavailable", warning.call_args.args[-1])


if __name__ == "__main__":
    unittest.main()

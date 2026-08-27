from __future__ import annotations

import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication, QComboBox, QWidget

from app.ui.github_route_presentation import github_route_display_name
from app.ui.github_routes_dialog import GithubMirrorDialog


class FakeSignal:
    def __init__(self, *, fail_first_disconnect: bool = False) -> None:
        self.callbacks = []
        self.disconnect_calls = 0
        self.fail_first_disconnect = fail_first_disconnect

    def connect(self, callback) -> None:
        self.callbacks.append(callback)

    def disconnect(self, callback) -> None:
        self.disconnect_calls += 1
        if self.fail_first_disconnect and self.disconnect_calls == 1:
            raise RuntimeError("already disconnected")
        self.callbacks = [item for item in self.callbacks if item != callback]


class FakeSettings:
    def __init__(self) -> None:
        self.values = {}
        self.sync_count = 0

    def set(self, key: str, value: str) -> None:
        self.values[key] = value

    def sync(self) -> None:
        self.sync_count += 1


class FakeUpdateService:
    def __init__(self) -> None:
        self.route_probe_finished = FakeSignal()
        self.route_probe_failed = FakeSignal()
        self.route_probe_results = {}
        self.active_runtimes: set[str] = set()
        self.probe_result = True
        self.probe_calls = 0
        self.route_settings = None

    def set_download_routes(self, mode, urls, profiles) -> None:
        self.route_settings = (mode, urls, profiles)

    def probe_download_routes(self) -> bool:
        self.probe_calls += 1
        if self.probe_result:
            self.active_runtimes.add("route_probe")
        return self.probe_result

    def runtime_active(self, *kinds: str) -> bool:
        return any(kind in self.active_runtimes for kind in kinds)

    def serialized_route_profiles(self) -> str:
        return '{"direct":{"usable":true}}'


class SettingsHost(QWidget):
    def __init__(self, service: FakeUpdateService) -> None:
        super().__init__()
        self.settings = FakeSettings()
        self.window = SimpleNamespace(
            update_service=service,
            app_settings=self.settings,
        )
        self.github_mirror_urls = ""
        self.github_route_profiles = "{}"
        self.github_download_route = QComboBox()
        self.github_download_route.addItem("Auto", "auto")
        self.selected_route = ""

    def refresh_github_route_combo(self, selected=None) -> None:
        if selected is not None:
            self.selected_route = str(selected)


class GithubRoutesDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        existing = QCoreApplication.instance()
        if existing is not None and not isinstance(existing, QApplication):
            raise unittest.SkipTest("a non-GUI QCoreApplication already exists")
        cls.app = existing or QApplication([])

    def create_dialog(self, service: FakeUpdateService | None = None):
        service = service or FakeUpdateService()
        host = SettingsHost(service)
        dialog = GithubMirrorDialog(host)
        return service, host, dialog

    def test_failed_start_restores_probe_button_instead_of_staying_stuck(self) -> None:
        service, host, dialog = self.create_dialog()
        service.probe_result = False
        try:
            dialog.probe_routes()

            self.assertEqual(service.probe_calls, 1)
            self.assertTrue(dialog.probe_button.isEnabled())
            self.assertIn("无法启动", dialog.status.text())
        finally:
            dialog.close()
            host.close()

    def test_route_names_and_truncated_addresses_have_stable_presentation(self) -> None:
        service, host, dialog = self.create_dialog()
        try:
            self.assertEqual(github_route_display_name("direct"), "GitHub 直连")
            self.assertEqual(
                github_route_display_name("custom:test", "自定义 · proxy.example"),
                "自定义 · proxy.example",
            )
            first = dialog.tree.topLevelItem(0)
            self.assertIn("https://github.com/", first.toolTip(1))
        finally:
            dialog.close()
            host.close()

    def test_dialog_opened_during_existing_probe_shows_busy_state(self) -> None:
        service = FakeUpdateService()
        service.active_runtimes.add("route_probe")
        service, host, dialog = self.create_dialog(service)
        try:
            self.assertFalse(dialog.probe_button.isEnabled())
            self.assertIn("已在进行", dialog.status.text())
        finally:
            dialog.close()
            host.close()

    def test_close_disconnects_each_service_signal_independently(self) -> None:
        service = FakeUpdateService()
        service.route_probe_finished = FakeSignal(fail_first_disconnect=True)
        service, host, dialog = self.create_dialog(service)

        dialog.close()

        self.assertEqual(service.route_probe_finished.disconnect_calls, 1)
        self.assertEqual(service.route_probe_failed.disconnect_calls, 1)
        host.close()

    def test_probe_result_profiles_are_persisted_and_rows_refresh(self) -> None:
        service, host, dialog = self.create_dialog()
        result = {
            "id": "direct",
            "name": "GitHub",
            "usable": True,
            "latency_ms": 18,
            "metadata_ok": True,
            "asset_ok": True,
            "status": "可用",
        }
        service.route_probe_results = {"direct": result}
        try:
            dialog._probe_finished([result])

            self.assertEqual(
                host.github_route_profiles,
                '{"direct":{"usable":true}}',
            )
            self.assertEqual(host.settings.sync_count, 1)
            self.assertIn("18", dialog.status.text())
            self.assertTrue(dialog.probe_button.isEnabled())
        finally:
            dialog.close()
            host.close()


if __name__ == "__main__":
    unittest.main()

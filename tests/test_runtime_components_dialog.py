from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QComboBox, QLabel, QWidget

from app.ui.i18n import text as ui_text
from app.ui.runtime_component_update_controller import RuntimeComponentUpdateController
from app.ui.runtime_components_dialog import UpdateDialog


class _Signal:
    def __init__(self) -> None:
        self.callbacks: list[object] = []

    def connect(self, callback) -> None:
        self.callbacks.append(callback)

    def disconnect(self, callback) -> None:
        if callback not in self.callbacks:
            raise RuntimeError("not connected")
        self.callbacks.remove(callback)


class _UpdateService:
    def __init__(self) -> None:
        self.download_finished = _Signal()
        self.download_failed = _Signal()
        self.install_finished = _Signal()
        self.install_failed = _Signal()
        self.active_runtimes: set[str] = set()
        self.tool_overrides: dict[str, str] = {}
        self.downloads: list[tuple[dict, str]] = []

    def download_asset(self, asset: dict, component: str = "") -> None:
        self.downloads.append((asset, component))

    def runtime_active(self, *kinds: str) -> bool:
        return any(kind in self.active_runtimes for kind in kinds)

    def available_download_routes(self) -> list[object]:
        return []


def _result(name: str) -> dict:
    return {
        "name": name,
        "current": "Not installed",
        "source": "",
        "runtime_path": "",
        "latest": "1.0.0",
        "assets": [{
            "name": f"{name}.zip",
            "size": 1024,
            "browser_download_url": f"https://github.com/example/{name}/releases/download/v1/{name}.zip",
        }],
        "auto_install_supported": True,
        "install_available": True,
        "has_update": False,
        "url": f"https://github.com/example/{name}/releases",
    }


class RuntimeComponentsDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_bulk_failure_is_attributed_to_active_component_not_selection(self) -> None:
        service = _UpdateService()
        dialog = UpdateDialog([_result("Deno"), _result("FFmpeg")], service)
        try:
            dialog._bulk_total = 2
            dialog._bulk_active_component = "Deno"
            dialog.tree.setCurrentItem(dialog.tree.topLevelItem(1))

            with patch("app.ui.runtime_components_dialog.QTimer.singleShot"):
                dialog.download_failed("network unavailable")

            self.assertEqual(len(dialog._bulk_errors), 1)
            self.assertIn("Deno", dialog._bulk_errors[0])
            self.assertNotIn("FFmpeg", dialog._bulk_errors[0])
            self.assertEqual(dialog._bulk_active_component, "")
        finally:
            dialog.close()
            self.app.processEvents()

    def test_closing_dialog_stops_owned_queue_and_disconnects_every_signal(self) -> None:
        service = _UpdateService()
        result = _result("Deno")
        dialog = UpdateDialog([result], service)
        asset = result["assets"][0]
        dialog._bulk_queue = [(result, asset)]
        dialog._bulk_total = 1
        dialog._bulk_active_component = "Deno"

        # Simulate one connection already being removed. The remaining three
        # must still be disconnected independently during close.
        service.download_finished.disconnect(dialog.download_finished)
        dialog.close()
        self.app.processEvents()
        dialog._start_next_bulk_install()

        self.assertTrue(dialog._closed)
        self.assertEqual(dialog._bulk_queue, [])
        self.assertEqual(dialog._bulk_total, 0)
        self.assertEqual(service.downloads, [])
        self.assertEqual(service.download_finished.callbacks, [])
        self.assertEqual(service.download_failed.callbacks, [])
        self.assertEqual(service.install_finished.callbacks, [])
        self.assertEqual(service.install_failed.callbacks, [])

    def test_install_success_refreshes_row_status_color_and_tooltips(self) -> None:
        service = _UpdateService()
        dialog = UpdateDialog([_result("Deno")], service)
        install_path = "D:/Huifa/tools/deno/x64/deno.exe"
        result = SimpleNamespace(
            component="Deno",
            location=install_path,
            paths=[install_path],
        )
        dialog._bulk_total = 1
        try:
            with patch(
                "app.ui.runtime_components_dialog.installed_component_details",
                return_value=("2.5.0", "程序目录 deno.exe", install_path),
            ), patch("app.ui.runtime_components_dialog.QTimer.singleShot"):
                dialog.install_finished(result)

            item = dialog.tree.topLevelItem(0)
            payload = item.data(0, Qt.UserRole)
            self.assertEqual(item.text(1), "2.5.0")
            self.assertEqual(item.text(2), "程序目录 deno.exe")
            self.assertEqual(item.text(3), install_path)
            self.assertEqual(item.foreground(6).color().name(), "#138a4b")
            self.assertIn("2.5.0", item.toolTip(1))
            self.assertIn(install_path, item.toolTip(3))
            self.assertEqual(item.toolTip(6), item.text(6))
            self.assertTrue(payload["installed"])
            self.assertFalse(payload["install_available"])
            self.assertFalse(payload["has_update"])
            self.assertTrue(dialog.results[0]["installed"])
            self.assertFalse(dialog.results[0]["install_available"])
        finally:
            dialog.close()
            self.app.processEvents()

    def test_bulk_install_blocks_the_single_component_download_action(self) -> None:
        service = _UpdateService()
        result = _result("Deno")
        dialog = UpdateDialog([result], service)
        try:
            dialog._bulk_total = 1
            dialog.update_detail()

            self.assertFalse(dialog.download_button.isEnabled())
            dialog.download_selected()
            self.assertEqual(service.downloads, [])
        finally:
            dialog.close()
            self.app.processEvents()

    def test_up_to_date_component_does_not_offer_reinstall(self) -> None:
        service = _UpdateService()
        result = _result("Deno")
        result.update({
            "current": "1.0.0",
            "installed": True,
            "install_available": False,
            "has_update": False,
        })
        dialog = UpdateDialog([result], service)
        try:
            self.assertEqual(
                dialog.tree.topLevelItem(0).text(6),
                ui_text("Up to date"),
            )
            self.assertFalse(dialog.download_button.isEnabled())
            self.assertFalse(dialog.install_all_button.isEnabled())
            self.assertEqual(dialog._bulk_install_candidates(), [])

            dialog.download_selected()

            self.assertEqual(service.downloads, [])
            self.assertEqual(
                dialog.detail.text(),
                ui_text("The local version is up to date."),
            )
        finally:
            dialog.close()
            self.app.processEvents()

    def test_installed_component_save_failure_releases_busy_state(self) -> None:
        parent = QWidget()
        refreshes: list[bool] = []
        renders: list[bool] = []
        parent.window = SimpleNamespace(
            update_service=SimpleNamespace(),
            settings_status=lambda _message: None,
        )
        parent.ffmpeg_build_channel = QComboBox()
        parent.ffmpeg_build_channel.addItem("latest", "latest")
        parent.update_status = QLabel()
        parent.refresh_local_core_versions = (
            lambda *, force=False: refreshes.append(force)
        )
        parent._render_runtime_component_statuses = lambda: renders.append(True)
        parent._render_runtime_component_status = lambda _component: None
        parent._apply_installed_runtime_paths = lambda _result: (_ for _ in ()).throw(
            OSError("settings disk unavailable")
        )
        controller = RuntimeComponentUpdateController(parent)
        controller.installing_component = "Deno"
        try:
            with patch(
                "app.ui.runtime_component_update_controller.QMessageBox.warning",
            ) as warning:
                controller.component_installed(SimpleNamespace(
                    component="Deno",
                    paths=("tools/deno/deno.exe",),
                ))

            self.assertEqual(controller.installing_component, "")
            self.assertEqual(refreshes, [True])
            self.assertEqual(renders, [True])
            warning.assert_called_once()
            self.assertIn("settings disk unavailable", warning.call_args.args[2])
        finally:
            parent.close()


if __name__ == "__main__":
    unittest.main()

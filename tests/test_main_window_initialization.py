from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from app.ui.main_window import MainWindow


class MainWindowInitializationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        existing = QCoreApplication.instance()
        if existing is not None and not isinstance(existing, QApplication):
            raise unittest.SkipTest("a non-GUI QCoreApplication already exists")
        cls.app = existing or QApplication([])

    def test_constructor_runs_explicit_lifecycle_phases_in_order(self) -> None:
        calls: list[object] = []
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            with patch(
                "app.ui.main_window.initialize_data_layout",
                side_effect=lambda: calls.append("data_layout") or data_dir,
            ), patch.object(
                MainWindow,
                "_initialize_window_geometry",
                side_effect=lambda: calls.append("window_geometry"),
            ), patch.object(
                MainWindow,
                "_initialize_core_services",
                side_effect=lambda path: calls.append(("core_services", path)),
            ), patch.object(
                MainWindow,
                "_initialize_main_pages",
                side_effect=lambda: calls.append("main_pages"),
            ), patch.object(
                MainWindow,
                "_initialize_status_summary",
                side_effect=lambda: calls.append("status_summary"),
            ), patch.object(
                MainWindow,
                "_connect_runtime_signals",
                side_effect=lambda: calls.append("runtime_signals"),
            ), patch.object(
                MainWindow,
                "_restore_startup_state",
                side_effect=lambda: calls.append("startup_state"),
            ):
                window = MainWindow()

        self.assertEqual(calls, [
            "window_geometry",
            "data_layout",
            ("core_services", data_dir),
            "main_pages",
            "status_summary",
            "runtime_signals",
            "startup_state",
        ])
        window.deleteLater()
        self.app.processEvents()

    def test_real_synchronous_startup_and_shutdown_keep_required_services_available(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "app.ui.main_window.initialize_data_layout",
            return_value=Path(directory),
        ), patch("app.ui.main_window.QTimer.singleShot"):
            window = MainWindow()
            self.assertEqual(Path(window.db.path), Path(directory) / "app.db")
            self.assertIs(window.dashboard.window, window)
            self.assertIs(window.settings.window, window)
            self.assertEqual(window.tabs.count(), 6)
            self.assertTrue(window.task_status_summary.running)

            window.shutdown_controller.begin()
            self.assertFalse(window.task_status_summary.running)
            window.shutdown_controller.poll()
            self.assertTrue(window.shutdown_controller.complete)

        window.deleteLater()
        self.app.processEvents()


if __name__ == "__main__":
    unittest.main()

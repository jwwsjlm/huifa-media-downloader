from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QWidget

from app.ui.diagnostics_export_controller import (
    collect_diagnostic_summary,
    export_diagnostics,
    normalized_diagnostics_target,
)


class _Settings:
    def __init__(self, **values: object) -> None:
        self.values = values

    def get(self, key: str) -> object:
        return self.values.get(key, "")


class _Logs:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.exports: list[tuple[Path, dict[str, object]]] = []
        self.error: Exception | None = None

    def export_bundle(self, target: Path, summary: dict[str, object]) -> Path:
        if self.error is not None:
            raise self.error
        self.exports.append((target, summary))
        return target


class DiagnosticsExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        existing = QCoreApplication.instance()
        if existing is not None and not isinstance(existing, QApplication):
            raise unittest.SkipTest("a non-GUI QCoreApplication already exists")
        cls.app = existing or QApplication([])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.parent = QWidget()
        self.logs = _Logs(self.root / "logs" / "downloads")
        self.database = SimpleNamespace(
            path=self.root / "app.db",
            recovery_report=SimpleNamespace(as_dict=lambda: {"status": "ok"}),
            last_backup_path="",
            last_backup_error="",
        )
        self.settings = _Settings(
            download_dir="D:/downloads",
            proxy="",
            ffmpeg_path="",
            ffprobe_path="",
            deno_path="",
            ytdlp_ejs_source="auto",
        )
    def tearDown(self) -> None:
        self.parent.close()
        self.parent.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()
        self.temporary.cleanup()

    def test_target_adds_zip_suffix_only_when_missing(self) -> None:
        self.assertEqual(
            normalized_diagnostics_target(self.root / "support"),
            self.root / "support.zip",
        )
        self.assertEqual(
            normalized_diagnostics_target(self.root / "support.ZIP"),
            self.root / "support.ZIP",
        )

    def test_optional_probe_failures_do_not_abort_manifest_collection(self) -> None:
        replacement_database = SimpleNamespace(
            path=self.root / "replacement.db",
            recovery_report=SimpleNamespace(as_dict=lambda: {"status": "new"}),
            last_backup_path="backup.db",
            last_backup_error="",
        )
        def runtime_probe(component: str, *_configured: object):
            if component == "Deno":
                raise OSError("broken runtime")
            return f"{component}-version", "managed", f"tools/{component}"

        with patch(
            "app.ui.diagnostics_export_controller.installed_component_details",
            return_value=("2026.08.26", "managed", "tools/yt-dlp.exe"),
        ), patch(
            "app.ui.diagnostics_export_controller.runtime_component_presence",
            side_effect=runtime_probe,
        ), patch(
            "app.ui.diagnostics_export_controller.publishing_core_status",
            side_effect=RuntimeError("broken publishing probe"),
        ), patch(
            "app.ui.diagnostics_export_controller.publishing_core_root",
            return_value=self.root / "publishing",
        ), patch(
            "app.ui.diagnostics_export_controller.resolve_chromium_executable",
            return_value=self.root / "chromium.exe",
        ):
            summary = collect_diagnostic_summary(
                self.settings,
                lambda: replacement_database,
            )

        self.assertEqual(summary["yt_dlp"], "2026.08.26")
        self.assertEqual(summary["deno"], "unavailable")
        self.assertEqual(summary["deno_runtime"]["error_type"], "OSError")
        self.assertEqual(summary["social_auto_upload"], "unavailable")
        self.assertEqual(
            summary["social_auto_upload_runtime"]["error_type"],
            "RuntimeError",
        )
        self.assertEqual(summary["publishing_chromium"], str(self.root / "chromium.exe"))
        self.assertEqual(summary["database"]["path"], str(replacement_database.path))

    def test_cancelled_dialog_does_not_collect_or_export(self) -> None:
        with patch(
            "app.ui.diagnostics_export_controller.QFileDialog.getSaveFileName",
            return_value=("", ""),
        ), patch(
            "app.ui.diagnostics_export_controller.collect_diagnostic_summary",
        ) as collect:
            export_diagnostics(
                self.parent,
                self.logs,
                self.settings,
                lambda: self.database,
            )

        collect.assert_not_called()
        self.assertEqual(self.logs.exports, [])

    def test_export_uses_normalized_target_and_reports_success(self) -> None:
        target = self.root / "support-bundle"
        summary = {"application": "test"}
        with patch(
            "app.ui.diagnostics_export_controller.QFileDialog.getSaveFileName",
            return_value=(str(target), "ZIP Archives (*.zip)"),
        ), patch(
            "app.ui.diagnostics_export_controller.collect_diagnostic_summary",
            return_value=summary,
        ), patch(
            "app.ui.diagnostics_export_controller.QMessageBox.information",
        ) as information, patch(
            "app.ui.diagnostics_export_controller.QMessageBox.warning",
        ) as warning:
            export_diagnostics(
                self.parent,
                self.logs,
                self.settings,
                lambda: self.database,
            )

        self.assertEqual(self.logs.exports, [(target.with_suffix(".zip"), summary)])
        warning.assert_not_called()
        information.assert_called_once()
        self.assertIn(str(target.with_suffix(".zip")), information.call_args.args[-1])

    def test_export_failure_is_reported_without_false_success(self) -> None:
        self.logs.error = OSError("disk unavailable")
        with patch(
            "app.ui.diagnostics_export_controller.QFileDialog.getSaveFileName",
            return_value=(str(self.root / "diagnostics.zip"), ""),
        ), patch(
            "app.ui.diagnostics_export_controller.collect_diagnostic_summary",
            return_value={},
        ), patch(
            "app.ui.diagnostics_export_controller.QMessageBox.information",
        ) as information, patch(
            "app.ui.diagnostics_export_controller.QMessageBox.warning",
        ) as warning:
            export_diagnostics(
                self.parent,
                self.logs,
                self.settings,
                lambda: self.database,
            )

        warning.assert_called_once()
        information.assert_not_called()
        self.assertIn("disk unavailable", warning.call_args.args[-1])


if __name__ == "__main__":
    unittest.main()

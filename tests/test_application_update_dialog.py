from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QMessageBox, QWidget

from app.core.application_update_service import ApplicationUpdateService
from app.core.application_updater import ApplicationUpdate
from app.core.update_receipt import UpdateInstallReceipt
from app.ui.application_update_dialog import ApplicationUpdateDialog
from app.ui.application_update_controller import (
    application_update_receipt_presentation,
)


def make_pending_update() -> ApplicationUpdate:
    return ApplicationUpdate(
        token="pending-0.3.0",
        current_version="0.2.0",
        version="0.3.0",
        package_id="Huifa.VideoDownloader",
        file_name="Huifa.VideoDownloader-0.3.0-full.nupkg",
        size_bytes=50 * 1024 * 1024,
        sha256="b" * 64,
        release_notes_markdown="# 更新内容\n\n- 修复下载恢复\n- 优化启动速度",
        is_downgrade=False,
        is_portable=True,
        downloaded=True,
    )


class UpdateHost(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.install_calls: list[tuple[ApplicationUpdate, bool]] = []

    def install_application_update(
        self,
        update: ApplicationUpdate,
        *,
        confirmed: bool = False,
    ) -> None:
        self.install_calls.append((update, confirmed))


class ApplicationUpdateDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        existing = QCoreApplication.instance()
        if existing is not None and not isinstance(existing, QApplication):
            raise unittest.SkipTest("a non-GUI QCoreApplication already exists")
        cls.app = existing or QApplication([])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.service = ApplicationUpdateService(Path(self.temporary.name) / "updates")
        self.host = UpdateHost()

    def tearDown(self) -> None:
        self.assertTrue(self.service.shutdown(timeout_ms=1000))
        self.host.close()
        self.app.processEvents()

    def test_pending_update_shows_version_notes_and_restart_install_action(
        self,
    ) -> None:
        update = make_pending_update()
        dialog = ApplicationUpdateDialog(update, self.service, self.host)

        self.assertIn("等待重启安装", dialog.windowTitle())
        self.assertIn("0.3.0", dialog.status.text())
        self.assertIn("已下载", dialog.status.text())
        self.assertIn("修复下载恢复", dialog.notes.toPlainText())
        labels = [label.text() for label in dialog.findChildren(QLabel)]
        self.assertIn("GitHub Releases 发布说明", labels)
        self.assertEqual(dialog.install_button.text(), "重启安装")
        self.assertTrue(dialog.install_button.isEnabled())
        self.assertEqual(dialog.download_button.text(), "已下载")
        self.assertFalse(dialog.download_button.isEnabled())

        dialog.close()

    def test_install_is_not_scheduled_until_user_explicitly_confirms(self) -> None:
        update = make_pending_update()
        dialog = ApplicationUpdateDialog(update, self.service, self.host)

        with patch.object(QMessageBox, "question", return_value=QMessageBox.No):
            dialog.install()
        self.assertEqual(self.host.install_calls, [])

        with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
            dialog.install()
        self.assertEqual(self.host.install_calls, [(update, True)])
        self.assertEqual(dialog.result(), QDialog.Accepted)

    def test_download_can_pause_and_continue_from_saved_range(self) -> None:
        update = ApplicationUpdate(
            token="download-0.4.0",
            current_version="0.3.0",
            version="0.4.0",
            package_id="Huifa.VideoDownloader",
            file_name="Huifa.VideoDownloader-0.4.0-full.nupkg",
            size_bytes=317 * 1024 * 1024,
            sha256="d" * 64,
            release_notes_markdown="- 支持断点续传",
            is_downgrade=False,
            is_portable=True,
            downloaded=False,
        )
        dialog = ApplicationUpdateDialog(update, self.service, self.host)

        with patch.object(self.service, "download", return_value=True):
            dialog.start_download()
        self.assertEqual(dialog.later_button.text(), "暂停下载")
        self.assertTrue(dialog.later_button.isEnabled())

        with patch.object(self.service, "cancel_download", return_value=True):
            dialog.defer_or_pause()
        self.assertIn("保存断点", dialog.status.text())
        self.assertFalse(dialog.later_button.isEnabled())

        dialog.on_download_cancelled()
        self.assertEqual(dialog.download_button.text(), "继续下载")
        self.assertEqual(dialog.later_button.text(), "稍后")
        self.assertIn("断点进度", dialog.status.text())

        dialog.on_failed("网络连接中断；已保留 1048576 字节，下次将从断点继续")
        self.assertEqual(dialog.download_button.text(), "继续下载")
        self.assertIn("已保留进度", dialog.status.text())
        dialog.close()

    def test_resume_state_does_not_depend_on_translated_button_text(self) -> None:
        update = ApplicationUpdate(
            token="localized-resume-0.4.0",
            current_version="0.3.0",
            version="0.4.0",
            package_id="Huifa.VideoDownloader",
            file_name="Huifa.VideoDownloader-0.4.0-full.nupkg",
            size_bytes=1024,
            sha256="e" * 64,
            release_notes_markdown="- Resume",
            is_downgrade=False,
            is_portable=True,
            downloaded=False,
        )
        dialog = ApplicationUpdateDialog(update, self.service, self.host)
        dialog.on_download_cancelled()
        dialog.download_button.setText("自定义语言包文本")

        with patch.object(self.service, "download", return_value=True):
            dialog.start_download()

        self.assertTrue(dialog._resuming_download)
        self.assertIn("已保存的断点", dialog.status.text())
        dialog.close()

    def test_download_completion_relabels_and_disables_download_action(self) -> None:
        update = replace(make_pending_update(), downloaded=False)
        dialog = ApplicationUpdateDialog(update, self.service, self.host)

        dialog.on_downloaded(make_pending_update())

        self.assertEqual(dialog.download_button.text(), "已下载")
        self.assertFalse(dialog.download_button.isEnabled())
        self.assertTrue(dialog.install_button.isEnabled())
        dialog.close()

    def test_download_start_failure_restores_controls_and_progress_is_bounded(
        self,
    ) -> None:
        update = ApplicationUpdate(
            token="start-error-0.4.0",
            current_version="0.3.0",
            version="0.4.0",
            package_id="Huifa.VideoDownloader",
            file_name="Huifa.VideoDownloader-0.4.0-full.nupkg",
            size_bytes=1024,
            sha256="f" * 64,
            release_notes_markdown="- Retry",
            is_downgrade=False,
            is_portable=True,
            downloaded=False,
        )
        dialog = ApplicationUpdateDialog(update, self.service, self.host)

        with patch.object(
            self.service, "download", side_effect=RuntimeError("not configured")
        ):
            dialog.start_download()

        self.assertTrue(dialog.download_button.isEnabled())
        self.assertIn("not configured", dialog.status.text())
        dialog.on_progress(150)
        self.assertEqual(dialog.progress.value(), 100)
        self.assertIn("100%", dialog.status.text())
        dialog.on_progress(-20)
        self.assertEqual(dialog.progress.value(), 0)
        self.assertIn("0%", dialog.status.text())
        dialog.close()

    def test_closed_dialog_ignores_queued_update_callbacks(self) -> None:
        update = make_pending_update()
        dialog = ApplicationUpdateDialog(update, self.service, self.host)
        original_status = dialog.status.text()
        dialog.close()
        self.app.processEvents()

        dialog.on_progress(12)
        dialog.on_failed("late failure")

        self.assertTrue(dialog._closed)
        self.assertEqual(dialog.status.text(), original_status)

    def test_restart_receipt_presentation_verifies_the_running_version(self) -> None:
        receipt = UpdateInstallReceipt(
            status="succeeded",
            from_version="0.2.0",
            to_version="0.3.0",
            current_version="0.3.0",
            message="SHA-256 verified",
            finished_at="2026-08-24T12:00:00+00:00",
        )

        succeeded, title, message = application_update_receipt_presentation(
            receipt, "0.3.0"
        )
        self.assertTrue(succeeded)
        self.assertEqual(title, "更新安装成功")
        self.assertIn("0.3.0", message)

        succeeded, title, message = application_update_receipt_presentation(
            receipt, "0.2.0"
        )
        self.assertFalse(succeeded)
        self.assertEqual(title, "更新结果需要确认")
        self.assertIn("目标版本：0.3.0", message)
        self.assertIn("当前版本：0.2.0", message)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QWidget

from app.ui.task_format_selection import (
    TaskFormatSelectionController,
)
from app.ui.i18n import text as ui_text


class _Service:
    def __init__(self) -> None:
        self.tasks = {
            "task": SimpleNamespace(
                title="Example",
                thumbnail_path="",
                options_json={"content_mode": "video", "audio_format": "mp3"},
            )
        }
        self.selection_result = True
        self.selection_error: Exception | None = None
        self.selections: list[tuple[str, dict[str, object]]] = []
        self.selectors: list[tuple[str, str]] = []
        self.discarded: list[str] = []

    def set_format_selector(self, task_id: str, selector: str) -> bool:
        self.selectors.append((task_id, selector))
        return self.selection_result

    def set_format_selection(
        self,
        task_id: str,
        selection: dict[str, object],
    ) -> bool:
        if self.selection_error is not None:
            raise self.selection_error
        self.selections.append((task_id, selection))
        return self.selection_result

    def discard_task(self, task_id: str) -> None:
        self.discarded.append(task_id)


class TaskFormatSelectionControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        existing = QCoreApplication.instance()
        if existing is not None and not isinstance(existing, QApplication):
            raise unittest.SkipTest("a non-GUI QCoreApplication already exists")
        cls.app = existing or QApplication([])

    def setUp(self) -> None:
        self.parent = QWidget()
        self.status = QLabel()
        self.service = _Service()
        self.controller = TaskFormatSelectionController(
            self.parent,
            self.service,
            self.status,
        )

    def tearDown(self) -> None:
        self.parent.close()
        self.parent.deleteLater()
        self.status.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()

    def test_missing_or_malformed_choices_cancel_the_waiting_selection(self) -> None:
        with patch(
            "app.ui.task_format_selection.QMessageBox.warning"
        ) as warning:
            self.controller.choose("task", {"choices": "not-a-choice-list"})

        self.assertEqual(self.service.selectors, [("task", "")])
        self.assertEqual(self.service.selections, [])
        warning.assert_called_once()

    def test_rejected_dialog_discards_preview_task(self) -> None:
        dialog = Mock()
        dialog.exec.return_value = QDialog.Rejected
        with patch(
            "app.ui.task_format_selection.FormatSelectionDialog",
            return_value=dialog,
        ):
            self.controller.choose("task", {
                "choices": [{"selector": "137+bestaudio", "height": 1080}],
            })

        self.assertEqual(self.service.discarded, ["task"])
        self.assertEqual(
            self.status.text(),
            ui_text('Quality preview closed; no download task was created'),
        )

    def test_accepted_video_choice_updates_service_and_status(self) -> None:
        choice = {
            "selector": "137+bestaudio",
            "height": 1080,
            "content_mode": "video",
        }
        dialog = Mock()
        dialog.exec.return_value = QDialog.Accepted
        dialog.selected_choice.return_value = choice
        with patch(
            "app.ui.task_format_selection.FormatSelectionDialog",
            return_value=dialog,
        ):
            self.controller.choose("task", {"choices": [choice]})

        self.assertEqual(self.service.selections, [("task", choice)])
        self.assertIn("1080p", self.status.text())

    def test_expired_worker_is_reported_instead_of_claiming_download_started(self) -> None:
        self.service.selection_result = False
        choice = {
            "selector": "137+bestaudio",
            "height": 1080,
            "content_mode": "video",
        }
        dialog = Mock()
        dialog.exec.return_value = QDialog.Accepted
        dialog.selected_choice.return_value = choice
        with (
            patch(
                "app.ui.task_format_selection.FormatSelectionDialog",
                return_value=dialog,
            ),
            patch(
                "app.ui.task_format_selection.QMessageBox.warning"
            ) as warning,
        ):
            self.controller.choose("task", {"choices": [choice]})

        self.assertEqual(self.status.text(), ui_text('Format selection expired'))
        self.assertNotEqual(
            self.status.text(),
            ui_text('Selected {height}p; starting download'),
        )
        warning.assert_called_once()

    def test_persistence_failure_is_shown_without_false_success_status(self) -> None:
        self.service.selection_error = OSError("database busy")
        choice = {
            "selector": "137+bestaudio",
            "height": 1080,
            "content_mode": "video",
        }
        dialog = Mock()
        dialog.exec.return_value = QDialog.Accepted
        dialog.selected_choice.return_value = choice
        with (
            patch(
                "app.ui.task_format_selection.FormatSelectionDialog",
                return_value=dialog,
            ),
            patch(
                "app.ui.task_format_selection.QMessageBox.warning"
            ) as warning,
        ):
            self.controller.choose("task", {"choices": [choice]})

        self.assertEqual(self.status.text(), "")
        self.assertIn("database busy", str(warning.call_args.args[-1]))


if __name__ == "__main__":
    unittest.main()

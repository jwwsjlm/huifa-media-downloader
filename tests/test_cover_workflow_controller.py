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

from app.storage.models import MediaItem
from app.ui.cover_export_paths import (
    default_cover_export_path,
    normalized_jpeg_target,
    safe_cover_stem,
)
from app.ui.cover_workflow_controller import (
    CoverWorkflowController,
    cover_options_from_settings,
)


class _Settings:
    values = {
        "cover_preset": "landscape_16_9",
        "cover_fit_mode": "crop",
        "cover_jpeg_quality": "88",
        "cover_focus_x": "50",
        "cover_focus_y": "50",
    }

    def get(self, key: str) -> str:
        return self.values.get(key, "")

    def get_int(
        self,
        key: str,
        default: int,
        minimum: int,
        maximum: int,
    ) -> int:
        return max(minimum, min(maximum, int(self.values.get(key, default))))


class _BrokenSettings(_Settings):
    values = {
        **_Settings.values,
        "cover_preset": "removed-preset",
        "cover_fit_mode": "invalid-fit",
    }


class _CoverService:
    def __init__(self) -> None:
        self.saved_targets: list[Path] = []

    def load_local(self, path: str):
        return {"path": path}

    def save_jpeg(self, _source, target, options):
        path = Path(target)
        self.saved_targets.append(path)
        return SimpleNamespace(
            path=path,
            width=options.width,
            height=options.height,
            byte_size=1024,
        )


class CoverWorkflowControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        existing = QCoreApplication.instance()
        if existing is not None and not isinstance(existing, QApplication):
            raise unittest.SkipTest("a non-GUI QCoreApplication already exists")
        cls.app = existing or QApplication([])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.cover = self.root / "cover.webp"
        self.cover.write_bytes(b"cover")
        self.parent = QWidget()
        self.service = _CoverService()
        self.controller = CoverWorkflowController(
            self.parent,
            _Settings(),
            self.service,
        )

    def tearDown(self) -> None:
        self.parent.close()
        self.parent.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()
        self.temporary.cleanup()

    def test_windows_invalid_and_reserved_default_names_are_sanitized(self) -> None:
        invalid = MediaItem(title='CON: demo?/"name"', thumbnail_path=str(self.cover))
        reserved = MediaItem(title="CON", thumbnail_path=str(self.cover))
        reserved_with_suffix = MediaItem(title="CON.txt", thumbnail_path=str(self.cover))

        self.assertEqual(safe_cover_stem(invalid), 'CON_ demo___name_')
        self.assertEqual(safe_cover_stem(reserved), "_CON")
        self.assertEqual(safe_cover_stem(reserved_with_suffix), "_CON.txt")
        target = default_cover_export_path(invalid, width=1280, height=720)
        self.assertEqual(target.parent, self.root)
        self.assertEqual(target.suffix, ".jpg")
        self.assertNotIn(":", target.name)
        self.assertNotIn("?", target.name)

    def test_jpeg_target_keeps_valid_extensions_and_replaces_other_suffixes(self) -> None:
        self.assertEqual(
            normalized_jpeg_target(self.root / "cover"),
            self.root / "cover.jpg",
        )
        self.assertEqual(
            normalized_jpeg_target(self.root / "cover.PNG"),
            self.root / "cover.jpg",
        )
        self.assertEqual(
            normalized_jpeg_target(self.root / "cover.JPEG"),
            self.root / "cover.JPEG",
        )

    def test_invalid_saved_cover_choices_fall_back_to_safe_defaults(self) -> None:
        options = cover_options_from_settings(_BrokenSettings())

        self.assertEqual((options.width, options.height), (1280, 720))
        self.assertEqual(options.fit_mode.value, "crop")
        self.assertEqual(options.quality, 88)

    def test_save_without_extension_writes_to_normalized_jpg_path(self) -> None:
        media = MediaItem(
            title="demo",
            thumbnail_path=str(self.cover),
            video_path=str(self.root / "video.mp4"),
        )
        selected = self.root / "exported-cover"
        with patch(
            "app.ui.cover_workflow_controller.QFileDialog.getSaveFileName",
            return_value=(str(selected), "JPG Images (*.jpg *.jpeg)"),
        ), patch(
            "app.ui.cover_workflow_controller.QMessageBox.information",
        ) as information:
            self.controller.save_as_jpeg(media)

        self.assertEqual(self.service.saved_targets, [selected.with_suffix(".jpg")])
        information.assert_called_once()
        self.assertIn(str(selected.with_suffix(".jpg")), information.call_args.args[-1])

    def test_missing_cover_stops_before_opening_save_dialog(self) -> None:
        media = MediaItem(title="missing", thumbnail_path=str(self.root / "missing.webp"))
        with patch(
            "app.ui.cover_workflow_controller.QFileDialog.getSaveFileName",
        ) as save_dialog, patch(
            "app.ui.cover_workflow_controller.QMessageBox.information",
        ) as information:
            self.controller.save_as_jpeg(media)

        save_dialog.assert_not_called()
        information.assert_called_once()


if __name__ == "__main__":
    unittest.main()

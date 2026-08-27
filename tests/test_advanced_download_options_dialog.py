from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from app.ui.download_options import AdvancedDownloadOptionsDialog


class AdvancedDownloadOptionsDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        existing = QCoreApplication.instance()
        if existing is not None and not isinstance(existing, QApplication):
            raise unittest.SkipTest("a non-GUI QCoreApplication already exists")
        cls.app = existing or QApplication([])

    def tearDown(self) -> None:
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()

    def test_initial_options_round_trip_and_drive_dependent_controls(self) -> None:
        dialog = AdvancedDownloadOptionsDialog({
            "content_mode": "audio",
            "audio_format": "flac",
            "container": "mkv",
            "collection_mode": "all",
            "collection_order": "reverse",
            "first_n": 12,
            "wait_for_live": True,
            "wait_min": 30,
            "wait_max": 90,
            "write_thumbnail": True,
            "sponsorblock_mode": "mark",
            "sponsorblock_categories": ["sponsor", "intro"],
            "rate_limit": "10M",
        })
        self.addCleanup(dialog.deleteLater)

        options = dialog.options()

        self.assertEqual(options["content_mode"], "audio")
        self.assertEqual(options["audio_format"], "flac")
        self.assertEqual(options["container"], "mkv")
        self.assertEqual(options["collection_mode"], "all")
        self.assertEqual(options["collection_order"], "reverse")
        self.assertEqual(options["first_n"], 12)
        self.assertEqual(options["wait_min"], 30)
        self.assertEqual(options["wait_max"], 90)
        self.assertTrue(options["write_thumbnail"])
        self.assertEqual(
            set(options["sponsorblock_categories"]),
            {"sponsor", "intro"},
        )
        self.assertEqual(options["rate_limit"], "10M")
        self.assertTrue(dialog.content_mode.isHidden())
        self.assertTrue(dialog.container.isHidden())
        self.assertTrue(dialog.audio_format.isEnabled())
        self.assertFalse(dialog.video_fps.isEnabled())
        self.assertTrue(dialog.wait_min.isEnabled())
        self.assertTrue(dialog.sponsor_categories["sponsor"].isEnabled())

    def test_control_state_updates_after_user_changes(self) -> None:
        dialog = AdvancedDownloadOptionsDialog({
            "content_mode": "audio",
            "wait_for_live": True,
            "sponsorblock_mode": "mark",
        })
        self.addCleanup(dialog.deleteLater)

        dialog.content_mode.setCurrentIndex(dialog.content_mode.findData("video"))
        dialog.wait_for_live.setChecked(False)
        dialog.sponsorblock_mode.setCurrentIndex(
            dialog.sponsorblock_mode.findData("off"),
        )

        self.assertFalse(dialog.audio_format.isEnabled())
        self.assertTrue(dialog.video_fps.isEnabled())
        self.assertFalse(dialog.wait_min.isEnabled())
        self.assertTrue(all(
            not checkbox.isEnabled()
            for checkbox in dialog.sponsor_categories.values()
        ))


if __name__ == "__main__":
    unittest.main()

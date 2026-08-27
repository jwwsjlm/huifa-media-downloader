from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from app.ui.supported_sites_dialog import (
    SupportedSitesDialog,
    installed_extractor_names,
)


class SupportedSitesDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        existing = QCoreApplication.instance()
        if existing is not None and not isinstance(existing, QApplication):
            raise unittest.SkipTest("a non-GUI QCoreApplication already exists")
        cls.app = existing or QApplication([])

    def test_loader_results_are_cleaned_sorted_and_deduplicated(self) -> None:
        dialog = SupportedSitesDialog(
            extractor_loader=lambda: [
                "TikTok",
                "  bilibili  ",
                "TikTok",
                "YouTube",
                "",
                None,
            ]
        )
        try:
            self.assertEqual(dialog.list.count(), 3)
            self.assertEqual(
                [dialog.list.item(index).text() for index in range(3)],
                ["bilibili", "TikTok", "YouTube"],
            )
            self.assertIn("3", dialog.count.text())
        finally:
            dialog.close()

    def test_installed_names_read_classes_without_instantiating_extractors(self) -> None:
        class AlphaExtractor:
            IE_NAME = "Alpha"

            def __init__(self):
                raise AssertionError("extractor instances must not be created")

        class BetaExtractor:
            IE_NAME = "beta"

            def __init__(self):
                raise AssertionError("extractor instances must not be created")

        with patch(
            "yt_dlp.list_extractor_classes",
            return_value=[BetaExtractor, AlphaExtractor],
        ):
            self.assertEqual(installed_extractor_names(), ["Alpha", "beta"])

    def test_clearing_search_restores_total_count_and_all_rows(self) -> None:
        dialog = SupportedSitesDialog(
            extractor_loader=lambda: ["bilibili", "TikTok", "YouTube"]
        )
        try:
            dialog.search.setText("tik")
            dialog.apply_filter()
            self.assertIn("1", dialog.count.text())
            self.assertEqual(
                sum(
                    not dialog.list.item(index).isHidden()
                    for index in range(dialog.list.count())
                ),
                1,
            )

            filtered_text = dialog.count.text()
            dialog.search.clear()
            dialog.apply_filter()

            self.assertNotEqual(dialog.count.text(), filtered_text)
            self.assertIn("3", dialog.count.text())
            self.assertTrue(
                all(
                    not dialog.list.item(index).isHidden()
                    for index in range(dialog.list.count())
                )
            )
        finally:
            dialog.close()

    def test_loader_failure_is_visible_without_creating_fake_rows(self) -> None:
        def fail():
            raise RuntimeError("extractor registry unavailable")

        dialog = SupportedSitesDialog(extractor_loader=fail)
        try:
            self.assertEqual(dialog.list.count(), 0)
            self.assertIn("extractor registry unavailable", dialog.count.text())
        finally:
            dialog.close()


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import os
import unittest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication, QLabel

from app.ui.about_page import AboutPage, THIRD_PARTY_ACKNOWLEDGEMENTS


class AboutPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        existing = QCoreApplication.instance()
        if existing is not None and not isinstance(existing, QApplication):
            raise unittest.SkipTest("a non-GUI QCoreApplication already exists")
        cls.app = existing or QApplication([])

    def test_acknowledgements_cover_the_shared_browser_and_packaged_runtime(self) -> None:
        entries = {
            name: {"purpose": purpose, "license": license_name, "url": url}
            for name, purpose, license_name, url in THIRD_PARTY_ACKNOWLEDGEMENTS
        }

        self.assertEqual(
            entries["social-auto-upload"]["url"],
            "https://github.com/dreammis/social-auto-upload",
        )
        self.assertEqual(
            entries["Playwright / Chromium"]["url"],
            "https://github.com/microsoft/playwright-python",
        )
        self.assertEqual(entries["CPython"]["url"], "https://github.com/python/cpython")
        self.assertIn("single app-local Chromium", entries["Playwright / Chromium"]["purpose"])
        self.assertIn("QtWebEngine is intentionally excluded", entries["PySide6 / Qt"]["purpose"])

    def test_about_page_exposes_clickable_project_links_and_single_browser_explanation(self) -> None:
        page = AboutPage()
        try:
            labels = page.findChildren(QLabel)
            link_texts = [label.text() for label in labels if label.openExternalLinks()]
            self.assertEqual(len(link_texts), len(THIRD_PARTY_ACKNOWLEDGEMENTS))
            self.assertTrue(any("dreammis/social-auto-upload" in text for text in link_texts))
            visible_text = "\n".join(label.text() for label in labels)
            self.assertIn("social-auto-upload", visible_text)
            self.assertIn("QtWebEngine", visible_text)
        finally:
            page.close()


if __name__ == "__main__":
    unittest.main()

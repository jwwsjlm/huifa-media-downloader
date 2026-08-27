from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from windows_version_info import build_windows_version_info, normalize_windows_version

from app.core.version import (
    APP_COPYRIGHT,
    APP_DESCRIPTION,
    APP_NAME,
    APP_PUBLISHER,
    APP_VERSION,
)


class WindowsVersionInfoTests(unittest.TestCase):
    def test_pep440_versions_are_normalized_to_four_windows_fields(self) -> None:
        self.assertEqual(normalize_windows_version("0.1.0"), ((0, 1, 0, 0), "0.1.0", False))
        self.assertEqual(normalize_windows_version("v1.2.3-beta.2"), ((1, 2, 3, 0), "1.2.3-beta.2", True))

    def test_invalid_or_unrepresentable_versions_are_rejected(self) -> None:
        for value in ("", "not-a-version", "1.2.3.4.5", "1.70000"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_windows_version(value)

    def test_product_fields_are_embedded_in_version_resource(self) -> None:
        version_info = build_windows_version_info(
            APP_VERSION,
            APP_NAME,
            APP_PUBLISHER,
            APP_DESCRIPTION,
            APP_COPYRIGHT,
        )
        rendered = str(version_info)
        for expected in (
            APP_NAME,
            APP_PUBLISHER,
            APP_DESCRIPTION,
            APP_COPYRIGHT,
            "HuifaVideoDownloader.exe",
            "ProductVersion",
            APP_VERSION,
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, rendered)

    def test_release_spec_attaches_shared_version_resource(self) -> None:
        source = (ROOT / "build" / "HuifaVideoDownloader.velopack.spec").read_text(
            encoding="utf-8"
        )
        self.assertIn("build_windows_version_info", source)
        self.assertIn("version=windows_version_info", source)

    def test_release_script_validates_windows_product_identity(self) -> None:
        spec = (ROOT / "build" / "HuifaVideoDownloader.velopack.spec").read_text(
            encoding="utf-8"
        )
        version_helper = (ROOT / "scripts" / "windows_version_info.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("APP_NAME", spec)
        self.assertIn("APP_PUBLISHER", spec)
        for field in ("ProductVersion", "FileVersion", "ProductName", "CompanyName", "OriginalFilename"):
            with self.subTest(field=field):
                self.assertIn(field, version_helper)


if __name__ == "__main__":
    unittest.main()

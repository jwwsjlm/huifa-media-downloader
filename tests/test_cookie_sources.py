from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.cookie_sources import (
    COOKIE_BROWSER_LABELS,
    COOKIE_SOURCE_BROWSER, COOKIE_SOURCE_EMBEDDED, COOKIE_SOURCE_FILE, COOKIE_SOURCE_NONE,
    CookieSource, browser_cookie_spec, materialize_cookie_source,
    normalize_cookie_browser, normalize_cookie_source,
)


class CookieSourceTests(unittest.TestCase):
    def test_browser_spec_matches_ytdlp_python_api_order(self):
        self.assertEqual(browser_cookie_spec("EDGE"), ("edge", None, None, None))
        self.assertEqual(browser_cookie_spec("firefox", "default", "basictext", "Work"), ("firefox", "default", "BASICTEXT", "Work"))

    def test_source_options_never_include_cookie_values(self):
        self.assertEqual(CookieSource(source=COOKIE_SOURCE_NONE).ytdlp_options(), {})
        self.assertEqual(CookieSource(source=COOKIE_SOURCE_FILE, file="C:/cookies.txt").ytdlp_options(), {"cookiefile": "C:/cookies.txt"})
        self.assertEqual(CookieSource(source=COOKIE_SOURCE_BROWSER, browser="edge").ytdlp_options(), {"cookiesfrombrowser": ("edge", None, None, None)})

    def test_invalid_values_are_normalized(self):
        self.assertEqual(normalize_cookie_source("invalid"), COOKIE_SOURCE_NONE)
        self.assertEqual(normalize_cookie_source("EMBEDDED"), COOKIE_SOURCE_EMBEDDED)
        self.assertEqual(normalize_cookie_browser("invalid"), "chrome")

    def test_browser_brand_names_are_native_strings(self):
        self.assertEqual(COOKIE_BROWSER_LABELS, {
            "chrome": "Chrome",
            "edge": "Edge",
            "firefox": "Firefox",
            "brave": "Brave",
        })

    def test_embedded_source_owns_and_cleans_temporary_export(self):
        with tempfile.TemporaryDirectory() as directory:
            cookie_path = Path(directory) / "cookies.txt"

            def export(_profile: str) -> Path:
                cookie_path.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
                return cookie_path

            with patch(
                "app.core.browser_cookies.CookieVault.create_temporary_netscape_file",
                side_effect=export,
            ):
                materialized = materialize_cookie_source(
                    CookieSource(source=COOKIE_SOURCE_EMBEDDED),
                )

            self.assertEqual(materialized.options, {"cookiefile": str(cookie_path)})
            self.assertEqual(materialized.temporary_file, cookie_path)
            self.assertTrue(cookie_path.exists())
            self.assertTrue(materialized.cleanup())
            self.assertFalse(cookie_path.exists())
            self.assertIsNone(materialized.temporary_file)


if __name__ == "__main__":
    unittest.main()

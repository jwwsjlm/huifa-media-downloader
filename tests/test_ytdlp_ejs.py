from __future__ import annotations

import unittest
from unittest.mock import patch

from app.core.ytdlp_ejs import (
    required_ytdlp_ejs_version,
    ytdlp_ejs_version_compatible,
)
from app.core.update_service import UpdateWorker


class YtDlpEjsCompatibilityTests(unittest.TestCase):
    def tearDown(self) -> None:
        required_ytdlp_ejs_version.cache_clear()

    def test_exact_default_extra_pin_is_detected(self) -> None:
        required_ytdlp_ejs_version.cache_clear()
        with patch(
            "app.core.ytdlp_ejs.requires",
            return_value=(
                "requests>=2.32.2,<3",
                "yt-dlp-ejs==0.8.0; extra == 'default'",
            ),
        ):
            self.assertEqual(required_ytdlp_ejs_version(), "0.8.0")
            self.assertTrue(ytdlp_ejs_version_compatible("0.8.0"))
            self.assertFalse(ytdlp_ejs_version_compatible("0.9.0"))

    def test_missing_metadata_keeps_remote_fallback_available(self) -> None:
        required_ytdlp_ejs_version.cache_clear()
        with patch("app.core.ytdlp_ejs.requires", return_value=None):
            self.assertEqual(required_ytdlp_ejs_version(), "")
            self.assertTrue(ytdlp_ejs_version_compatible("0.9.0"))

    def test_update_check_uses_pinned_release_instead_of_newer_ejs(self) -> None:
        worker = UpdateWorker({"yt-dlp-ejs": "yt-dlp/ejs"})
        latest = {
            "tag_name": "0.9.0",
            "assets": [{"name": "yt_dlp_ejs-0.9.0-py3-none-any.whl"}],
        }
        compatible_assets = [{
            "name": "yt_dlp_ejs-0.8.0-py3-none-any.whl",
            "browser_download_url": (
                "https://github.com/yt-dlp/ejs/releases/download/0.8.0/"
                "yt_dlp_ejs-0.8.0-py3-none-any.whl"
            ),
        }]
        with patch(
            "app.core.update_service.required_ytdlp_ejs_version", return_value="0.8.0"
        ), patch.object(
            worker,
            "_release_assets_for_tag",
            return_value=(compatible_assets, "0.8.0"),
        ) as release:
            payload = worker._compatible_ejs_release_payload(
                "yt-dlp/ejs", latest, {"User-Agent": "test"}
            )

        self.assertEqual(payload["tag_name"], "0.8.0")
        self.assertEqual(payload["assets"], compatible_assets)
        release.assert_called_once_with("yt-dlp/ejs", "0.8.0", {"User-Agent": "test"})


if __name__ == "__main__":
    unittest.main()

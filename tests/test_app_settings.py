from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.app_settings import AppSettings, default_settings


EXPECTED_SETTING_KEYS = frozenset({
    "download_dir", "processing_temp_dir", "quality", "transcode_codec",
    "transcode_device", "transcode_encoder", "subtitle_language", "playlist_mode",
    "download_options_json", "filename_template", "organize_task_folder",
    "download_performance_mode", "max_concurrent", "fragment_concurrent",
    "request_delay", "proxy", "download_cookie_file", "download_cookie_source",
    "download_cookie_browser", "download_cookie_profile", "download_cookie_keyring",
    "download_cookie_container", "ffmpeg_path", "ffprobe_path",
    "ffmpeg_build_channel", "deno_path", "ytdlp_ejs_source", "ytdlp_core_mode",
    "github_download_route", "github_mirror_urls", "github_route_profiles",
    "ui_language", "desktop_notifications", "appearance_theme",
    "publish_target_platforms", "auto_check_updates",
    "update_prerelease", "update_channel", "cover_preset", "cover_fit_mode",
    "cover_focus_x", "cover_focus_y", "download_cover_convert_jpeg",
    "cover_jpeg_quality", "prepend_cover_enabled", "prepend_cover_frames",
    "cover_ai_model", "cover_ai_api_url",
})


class AppSettingsTests(unittest.TestCase):
    def _settings(self, root: Path) -> AppSettings:
        data_root = root / "data"
        downloads = data_root / "downloads"
        data_root.mkdir(parents=True, exist_ok=True)
        downloads.mkdir(parents=True, exist_ok=True)
        patches = (
            patch("app.core.app_settings.application_dir", return_value=root),
            patch("app.core.app_settings.data_dir", return_value=data_root),
            patch("app.core.app_settings.downloads_dir", return_value=downloads),
        )
        with patches[0], patches[1], patches[2]:
            return AppSettings()

    def test_default_snapshot_is_complete_and_fresh_for_each_call(self) -> None:
        first = default_settings("data/downloads")
        second = default_settings("data/downloads")

        self.assertEqual(frozenset(first), EXPECTED_SETTING_KEYS)
        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        first["quality"] = "360p"
        self.assertEqual(second["quality"], "best")

    def test_non_finite_float_values_fall_back_before_reaching_qt_controls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = self._settings(Path(directory))
            for raw in ("nan", "inf", "-inf"):
                with self.subTest(raw=raw):
                    settings._settings.setValue("request_delay", raw)
                    self.assertEqual(
                        settings.get_float("request_delay", 1.5, 0.0, 60.0),
                        1.5,
                    )

    def test_portable_paths_are_trimmed_and_stored_relative(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tool = root / "tools" / "deno" / "deno.exe"
            settings = self._settings(root)

            with patch("app.core.app_settings.application_dir", return_value=root), patch(
                "app.core.app_settings.data_dir",
                return_value=root / "data",
            ):
                self.assertEqual(
                    settings.normalize_value("deno_path", f"  {tool}  "),
                    "tools/deno/deno.exe",
                )
                self.assertEqual(settings.normalize_value("deno_path", "  deno  "), "deno")


if __name__ == "__main__":
    unittest.main()

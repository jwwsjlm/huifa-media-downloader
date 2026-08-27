from __future__ import annotations

import unittest
from unittest.mock import patch

from app.core.download_submission import (
    DownloadSubmissionDebouncer,
    DownloadSubmissionSettingsError,
    build_download_request_context,
    service_task_arguments,
    submission_playlist_mode,
)


class _Settings:
    def __init__(self, **overrides: str) -> None:
        self.values = {
            "download_dir": "D:/downloads",
            "proxy": "",
            "download_cookie_file": "",
            "download_cookie_source": "none",
            "download_cookie_browser": "chrome",
            "download_cookie_profile": "",
            "download_cookie_keyring": "",
            "download_cookie_container": "",
            "quality": "best",
            "filename_template": "%(title)s.%(ext)s",
            "organize_task_folder": "false",
            "ffmpeg_path": "",
            "playlist_mode": "auto",
            "transcode_encoder": "original",
            "subtitle_language": "none",
            "prepend_cover_enabled": "false",
            "prepend_cover_frames": "3",
            **overrides,
        }

    def get(self, key: str) -> str:
        return self.values.get(key, "")

    def get_resolved_path(self, key: str) -> str:
        return self.values.get(key, "")

    def get_bool(self, key: str, default: bool = False) -> bool:
        value = str(self.values.get(key, "")).strip().casefold()
        return value in {"1", "true", "yes", "on"} if value else default

    def get_int(
        self,
        key: str,
        default: int,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int:
        try:
            value = int(self.values.get(key, default))
        except (TypeError, ValueError):
            value = default
        if minimum is not None:
            value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return value


class DownloadSubmissionTests(unittest.TestCase):
    def test_context_is_a_snapshot_without_touching_the_output_directory(self) -> None:
        settings = _Settings(
            download_dir="D:/media",
            playlist_mode="playlist",
            organize_task_folder="true",
            prepend_cover_frames="7",
        )
        with patch("pathlib.Path.mkdir") as mkdir:
            context = build_download_request_context(
                settings,
                options_json={"processing_temp_dir": "data/temp"},
            )

        mkdir.assert_not_called()
        self.assertEqual(context["output_dir"], "D:\\media")
        self.assertEqual(context["playlist_mode"], "playlist")
        self.assertTrue(context["download_album"])
        self.assertTrue(context["organize_task_folder"])
        self.assertEqual(context["prepend_cover_frames"], 7)
        self.assertEqual(
            context["options_json"],
            {"processing_temp_dir": "data/temp"},
        )

    def test_missing_folder_and_invalid_proxy_have_stable_error_codes(self) -> None:
        for settings, code in (
            (_Settings(download_dir=""), "missing_download_dir"),
            (_Settings(proxy="127.0.0.1:7890"), "invalid_proxy"),
        ):
            with self.subTest(code=code), self.assertRaises(
                DownloadSubmissionSettingsError,
            ) as raised:
                build_download_request_context(settings, options_json={})
            self.assertEqual(raised.exception.code, code)

    def test_cookie_file_does_not_override_explicit_none_source(self) -> None:
        context = build_download_request_context(
            _Settings(
                download_cookie_file="data/browser/cookies.txt",
                download_cookie_source="none",
            ),
            options_json={},
        )
        self.assertEqual(context["cookie_source"], "none")

    def test_playlist_mode_is_shared_by_dedupe_and_service_arguments(self) -> None:
        context = build_download_request_context(
            _Settings(playlist_mode="playlist"),
            options_json={"collection_mode": "single"},
        )
        mode = submission_playlist_mode(context, collection_mode="single")
        arguments = service_task_arguments(context, playlist_mode=mode)
        self.assertEqual(mode, "single")
        self.assertEqual(arguments["playlist_mode"], "single")
        self.assertFalse(arguments["download_album"])

    def test_debouncer_is_deterministic_and_allows_later_intentional_retry(self) -> None:
        guard = DownloadSubmissionDebouncer(interval_seconds=1.2)
        links = ["https://example.com/video"]
        self.assertFalse(guard.rejects(links, now=10.0))
        self.assertTrue(guard.rejects(links, now=10.5))
        self.assertTrue(guard.suppresses_empty_followup(now=11.1))
        self.assertFalse(guard.rejects(links, now=11.3))
        self.assertFalse(guard.suppresses_empty_followup(now=12.6))


if __name__ == "__main__":
    unittest.main()

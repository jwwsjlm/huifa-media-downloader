from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.core.download_service import DownloadService, DownloadTask
from app.core.subtitles import (
    DEFAULT_SUBTITLE_FORMAT,
    normalize_subtitle_language,
    subtitle_ytdlp_options,
)
from app.storage.database import Database


class SubtitleDownloadTests(unittest.TestCase):
    def test_subtitle_options_prefer_uploaded_and_enable_automatic_fallback(self) -> None:
        self.assertEqual(subtitle_ytdlp_options("none"), {
            "writesubtitles": False,
            "writeautomaticsub": False,
        })
        self.assertEqual(subtitle_ytdlp_options("zh-Hans"), {
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": ["zh-Hans"],
            "subtitlesformat": DEFAULT_SUBTITLE_FORMAT,
        })

    def test_language_normalization_preserves_ytdlp_identifier(self) -> None:
        self.assertEqual(normalize_subtitle_language(" EN-us\n"), "EN-us")
        self.assertEqual(normalize_subtitle_language("off"), "none")

    def test_task_subtitle_language_is_persisted_and_restored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "data.db")
            try:
                task = DownloadTask(
                    "subtitle-task",
                    "https://example.com/video",
                    directory,
                    subtitle_language="ja",
                    status="paused",
                )
                database.upsert_download_task(task)
                service = DownloadService(database)
                restored = service.restore_tasks()
                self.assertEqual(restored[0].subtitle_language, "ja")
            finally:
                database.close()


if __name__ == "__main__":
    unittest.main()

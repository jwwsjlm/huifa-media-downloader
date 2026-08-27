from __future__ import annotations

import unittest

from app.core.ytdlp_core_selection import (
    YtdlpCoreSelectionError,
    normalize_ytdlp_core_mode,
    select_ytdlp_core,
)


class YtdlpCoreSelectionTests(unittest.TestCase):
    def test_invalid_mode_normalizes_to_auto(self) -> None:
        self.assertEqual(normalize_ytdlp_core_mode("unexpected"), "auto")
        self.assertEqual(normalize_ytdlp_core_mode(None), "auto")

    def test_auto_optimistically_uses_unchecked_external_core(self) -> None:
        selection = select_ytdlp_core(
            "auto",
            external_executable="tools/yt-dlp.exe",
            external_version=None,
            builtin_available=True,
        )

        self.assertTrue(selection.uses_external)
        self.assertEqual(selection.executable, "tools/yt-dlp.exe")

    def test_auto_rejects_failed_external_probe_and_falls_back_to_builtin(self) -> None:
        selection = select_ytdlp_core(
            "auto",
            external_executable="tools/yt-dlp.exe",
            external_version="",
            builtin_available=True,
        )

        self.assertFalse(selection.uses_external)
        self.assertTrue(selection.external_rejected)

    def test_external_mode_does_not_silently_fall_back_after_failed_probe(self) -> None:
        with self.assertRaises(YtdlpCoreSelectionError) as raised:
            select_ytdlp_core(
                "external",
                external_executable="tools/yt-dlp.exe",
                external_version="",
                builtin_available=True,
            )

        self.assertEqual(raised.exception.reason, "external_probe_failed")
        self.assertIn("没有找到可运行", str(raised.exception))

    def test_builtin_mode_ignores_external_core_and_requires_builtin(self) -> None:
        selection = select_ytdlp_core(
            "builtin",
            external_executable="tools/yt-dlp.exe",
            external_version="2026.08.25",
            builtin_available=True,
        )
        self.assertFalse(selection.uses_external)

        with self.assertRaises(YtdlpCoreSelectionError) as raised:
            select_ytdlp_core(
                "builtin",
                external_executable="tools/yt-dlp.exe",
                external_version="2026.08.25",
                builtin_available=False,
            )
        self.assertEqual(raised.exception.reason, "builtin_missing")

    def test_auto_reports_packaged_all_unavailable_message(self) -> None:
        with self.assertRaises(YtdlpCoreSelectionError) as raised:
            select_ytdlp_core(
                "auto",
                external_executable="",
                external_version=None,
                builtin_available=False,
                packaged=True,
            )

        self.assertIn("内置 yt-dlp 下载核心加载失败", str(raised.exception))


if __name__ == "__main__":
    unittest.main()

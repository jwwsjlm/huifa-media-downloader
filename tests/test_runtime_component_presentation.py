from __future__ import annotations

import unittest

from app.ui.i18n import text as ui_text
from app.ui.runtime_component_presentation import (
    build_runtime_component_presentation,
    compact_runtime_version,
)


class RuntimeComponentPresentationTests(unittest.TestCase):
    def test_available_update_is_clickable_and_escaped(self) -> None:
        result = {
            "latest": "2.6.0",
            "has_update": True,
            "auto_install_supported": True,
            "assets": [{
                "name": "deno-x86_64-pc-windows-msvc.zip",
                "browser_download_url": (
                    "https://github.com/denoland/deno/releases/download/"
                    "v2.6.0/deno-x86_64-pc-windows-msvc.zip"
                ),
            }],
        }

        presentation = build_runtime_component_presentation(
            "Deno",
            ("2.5.0 <local>", "App local", "tools/deno/deno.exe"),
            result,
            remote_checking=False,
            remote_error="",
            installing_component="",
        )

        self.assertTrue(presentation.label_clickable)
        self.assertIn("&lt;local&gt;", presentation.label_text)
        self.assertEqual(presentation.button_text, ui_text('Update'))
        self.assertTrue(presentation.button_enabled)

    def test_ffprobe_uses_ffmpeg_switch_action(self) -> None:
        result = {
            "latest": "N-124716-g054dffd133-20260531",
            "has_update": True,
            "channel_switch_required": True,
            "auto_install_supported": True,
            "assets": [{
                "name": "ffmpeg-N-124716-g054dffd133-win64-gpl.zip",
                "browser_download_url": (
                    "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/"
                    "autobuild-2026-05-31-15-28/"
                    "ffmpeg-N-124716-g054dffd133-win64-gpl.zip"
                ),
            }],
        }

        presentation = build_runtime_component_presentation(
            "FFprobe",
            ("8.0", "App local", "tools/ffmpeg/ffprobe.exe"),
            result,
            remote_checking=False,
            remote_error="",
            installing_component="",
        )

        self.assertTrue(presentation.label_clickable)
        self.assertEqual(presentation.button_text, ui_text('Switch'))

    def test_checking_and_error_states_are_deterministic(self) -> None:
        checking = build_runtime_component_presentation(
            "Deno",
            ("2.5.0", "", ""),
            {},
            remote_checking=True,
            remote_error="",
            installing_component="",
        )
        failed = build_runtime_component_presentation(
            "Deno",
            ("2.5.0", "", ""),
            {},
            remote_checking=False,
            remote_error="network unavailable",
            installing_component="",
        )

        self.assertEqual(checking.button_text, ui_text('Checking…'))
        self.assertFalse(checking.button_enabled)
        self.assertEqual(failed.button_text, ui_text('Retry'))
        self.assertTrue(failed.button_enabled)
        self.assertEqual(failed.button_tooltip, "network unavailable")

    def test_long_ffmpeg_version_keeps_release_and_build_date(self) -> None:
        self.assertEqual(
            compact_runtime_version(
                "FFmpeg",
                "FFmpeg n9.0.1-6-g9d4ca21220-20260822 very long build text",
            ),
            "n9.0.1-6 · 20260822",
        )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.core.download_readiness import download_readiness_report


def _component_details(name: str, _configured: str = "") -> tuple[str, str, str]:
    if name == "yt-dlp":
        return "2026.08.19", "程序内置 yt-dlp 模块", "内置 Python 模块"
    raise AssertionError(f"版本探测不应在本地预检中执行：{name}")


def _component_presence(name: str, _configured: str = "") -> tuple[str, str, str]:
    values = {
        "yt-dlp": ("2026.08.19", "程序内置 yt-dlp 模块", "内置 Python 模块"),
        "FFmpeg": ("已找到", "程序目录 ffmpeg.exe", "C:/app/ffmpeg.exe"),
        "FFprobe": ("已找到", "程序目录 ffmpeg.exe 配套 ffprobe.exe", "C:/app/ffprobe.exe"),
        "Deno": ("未安装", "", ""),
        "yt-dlp-ejs": ("未安装", "未检测到软件本地 yt-dlp-ejs wheel", ""),
    }
    return values[name]


class DownloadReadinessTests(unittest.TestCase):
    def test_ready_when_core_and_directory_are_available_without_cookie(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "app.core.download_readiness.runtime_component_presence",
                side_effect=_component_presence,
            ):
                ready, rows = download_readiness_report(directory)

        self.assertTrue(ready)
        states = {row["name"]: row["state"] for row in rows}
        self.assertEqual(states["下载核心（yt-dlp）"], "可用")
        self.assertEqual(states["下载保存目录"], "可用")
        self.assertEqual(states["下载 Cookie"], "未配置")
        self.assertEqual(states["JavaScript 运行时（Deno）"], "建议安装")
        self.assertEqual(states["YouTube JS 支持（yt-dlp-ejs）"], "建议安装")

    def test_missing_core_or_configured_cookie_blocks_new_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing_cookie = str(Path(directory) / "missing-cookies.txt")
            with patch(
                "app.core.download_readiness.runtime_component_presence",
                side_effect=_component_presence,
            ):
                ready, rows = download_readiness_report(directory, missing_cookie)
            self.assertFalse(ready)
            cookie = next(row for row in rows if row["name"] == "下载 Cookie")
            self.assertEqual(cookie["state"], "不可用")

            def no_core(name: str, configured: str = "") -> tuple[str, str, str]:
                if name == "yt-dlp":
                    return "未安装", "未检测到内置 yt-dlp 模块", ""
                return _component_details(name, configured)

            def no_core_presence(name: str, configured: str = "") -> tuple[str, str, str]:
                if name == "yt-dlp":
                    return "未安装", "未检测到内置 yt-dlp 模块", ""
                return _component_presence(name, configured)

            with patch(
                "app.core.download_readiness.runtime_component_presence",
                side_effect=no_core_presence,
            ):
                ready, rows = download_readiness_report(directory)
            self.assertFalse(ready)
            core = next(row for row in rows if row["name"] == "下载核心（yt-dlp）")
            self.assertEqual(core["state"], "不可用")

    def test_missing_ffmpeg_is_a_blocking_preflight_item(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            def missing_ffmpeg(name: str, _configured: str = "") -> tuple[str, str, str]:
                if name == "FFmpeg":
                    return "未安装", "", ""
                return _component_presence(name, _configured)

            with patch(
                "app.core.download_readiness.runtime_component_presence",
                side_effect=missing_ffmpeg,
            ):
                ready, rows = download_readiness_report(directory)

        self.assertFalse(ready)
        ffmpeg = next(row for row in rows if row["name"] == "合并工具（FFmpeg）")
        self.assertEqual(ffmpeg["state"], "不可用")

    def test_selected_core_mode_is_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "app.core.download_readiness.runtime_component_presence",
                return_value=("2026.08.19", "程序内置 yt-dlp 模块", "内置 Python 模块"),
            ), patch(
                "app.core.download_readiness.ytdlp_python_core_available",
                return_value=True,
            ):
                external_ready, external_rows = download_readiness_report(
                    directory, ytdlp_core_mode="external"
                )
                builtin_ready, builtin_rows = download_readiness_report(
                    directory, ytdlp_core_mode="builtin"
                )
        self.assertFalse(external_ready)
        self.assertTrue(builtin_ready)
        self.assertIn("外置核心", external_rows[0]["detail"])
        self.assertIn("内置核心", builtin_rows[0]["detail"])

    def test_empty_embedded_cookie_profile_does_not_block_public_downloads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "app.core.download_readiness.runtime_component_presence",
                side_effect=_component_presence,
            ), patch("app.core.browser_cookies.CookieVault") as vault_type:
                vault_type.return_value.count.return_value = 0
                ready, rows = download_readiness_report(
                    directory,
                    cookie_source="embedded",
                )

        self.assertTrue(ready)
        cookie = next(row for row in rows if row["name"] == "下载 Cookie")
        self.assertEqual(cookie["state"], "未获取")
        self.assertIn("公开内容仍可下载", cookie["detail"])

    def test_remote_ejs_source_is_ready_with_deno_even_without_local_wheel(self) -> None:
        def deno_without_local_ejs(
            name: str,
            configured: str = "",
            *_extra: str,
        ) -> tuple[str, str, str]:
            if name == "Deno":
                return "2.4.0", "程序本地 Deno", "C:/app/tools/deno.exe"
            return _component_presence(name, configured)

        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "app.core.download_readiness.runtime_component_presence",
                side_effect=deno_without_local_ejs,
            ):
                ready, rows = download_readiness_report(
                    directory,
                    ytdlp_ejs_source="npm",
                )

        self.assertTrue(ready)
        ejs = next(
            row for row in rows
            if row["name"] == "YouTube JS 支持（yt-dlp-ejs）"
        )
        self.assertEqual(ejs["state"], "可用")


if __name__ == "__main__":
    unittest.main()

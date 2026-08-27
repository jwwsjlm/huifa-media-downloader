from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app import main as app_main
from app.core.version import APP_PUBLISHER, APP_VERSION


class _FakeApplication:
    @staticmethod
    def platformName() -> str:
        return "offscreen"

    @staticmethod
    def applicationVersion() -> str:
        return APP_VERSION

    @staticmethod
    def organizationName() -> str:
        return APP_PUBLISHER


class _FakeSettings:
    @staticmethod
    def get(_key: str) -> str:
        return ""


class _FakeWindow:
    app_settings = _FakeSettings()
    secure_store = SimpleNamespace(backend_name="keyring.backends.Windows.WinVaultKeyring")

    def __init__(self, update_mode: str = "velopack") -> None:
        self.application_update_mode = update_mode


class PackagedRuntimeSmokeTests(unittest.TestCase):
    def test_report_proves_real_download_core_gui_and_packaged_update_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "HuifaVideoDownloader.exe"
            executable.write_bytes(b"MZ")

            def component_details(name: str, _configured: str = "") -> tuple[str, str, str]:
                if name.casefold() == "yt-dlp":
                    return "2026.08.22", "程序内置 yt-dlp 模块", "内置 Python 模块"
                return "7.1", "程序内置文件 ffmpeg.exe", "ffmpeg.exe"

            with (
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "executable", str(executable)),
                patch.object(
                    app_main,
                    "download_ytdlp",
                    SimpleNamespace(YoutubeDL=lambda *_args, **_kwargs: None),
                ),
                patch.object(
                    app_main,
                    "installed_component_details",
                    side_effect=component_details,
                ),
            ):
                report = app_main._build_packaged_smoke_report(
                    _FakeApplication(),
                    _FakeWindow(),
                )

            self.assertTrue(report["ok"])
            self.assertTrue(report["frozen"])
            self.assertEqual(report["application_update_mode"], "velopack")
            self.assertEqual(report["application_version"], APP_VERSION)
            self.assertEqual(report["organization_name"], APP_PUBLISHER)
            self.assertEqual(report["executable"], str(executable.resolve()))
            self.assertTrue(report["yt_dlp"]["core_ready"])
            self.assertEqual(report["yt_dlp"]["version"], "2026.08.22")
            self.assertEqual(report["ffmpeg"]["version"], "7.1")
            self.assertEqual(report["ffprobe"]["version"], "7.1")
            self.assertTrue(report["pyside6_version"])
            self.assertEqual(report["qt_platform"], "offscreen")
            self.assertEqual(
                report["secure_store_backend"],
                "keyring.backends.Windows.WinVaultKeyring",
            )

    def test_report_rejects_non_packaged_update_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "HuifaVideoDownloader.exe"
            executable.write_bytes(b"MZ")
            with (
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "executable", str(executable)),
                patch.object(
                    app_main,
                    "download_ytdlp",
                    SimpleNamespace(YoutubeDL=lambda *_args, **_kwargs: None),
                ),
                patch.object(
                    app_main,
                    "installed_component_details",
                    return_value=("2026.08.22", "程序内置组件", "内置组件"),
                ),
            ):
                report = app_main._build_packaged_smoke_report(
                    _FakeApplication(),
                    _FakeWindow("source"),
                )

            self.assertFalse(report["ok"])

    def test_report_is_unhealthy_when_embedded_ytdlp_entry_point_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "HuifaVideoDownloader.exe"
            executable.write_bytes(b"MZ")
            with (
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "executable", str(executable)),
                patch.object(app_main, "download_ytdlp", SimpleNamespace()),
                patch.object(
                    app_main,
                    "installed_component_details",
                    return_value=("2026.08.22", "程序内置 yt-dlp 模块", "内置 Python 模块"),
                ),
            ):
                report = app_main._build_packaged_smoke_report(
                    _FakeApplication(),
                    _FakeWindow(),
                )

            self.assertFalse(report["ok"])
            self.assertFalse(report["yt_dlp"]["core_ready"])

    def test_json_report_is_atomic_utf8_and_leaves_no_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "nested" / "report.json"
            app_main.write_json_atomic(target, {"ok": True, "message": "内置下载核心可用"})

            self.assertEqual(
                json.loads(target.read_text(encoding="utf-8")),
                {"ok": True, "message": "内置下载核心可用"},
            )
            self.assertFalse(target.with_name(target.name + ".tmp").exists())

    def test_release_build_requires_packaged_runtime_report(self) -> None:
        script = (app_main.PROJECT_ROOT / "scripts" / "build_velopack_release.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("HUIFA_PACKAGED_SMOKE_OUTPUT", script)
        self.assertIn("WaitForExit(90000)", script)
        self.assertIn("application_update_mode -ne 'velopack'", script)
        self.assertIn("application_version -ne $Version", script)
        self.assertIn("organization_name -ne $ExpectedPublisher", script)
        self.assertIn("yt_dlp.core_ready", script)
        self.assertIn("ffmpeg.version", script)
        self.assertIn("ffprobe.version", script)
        self.assertIn("pyside6_version", script)
        self.assertIn("secure_store_backend", script)

    def test_velopack_spec_does_not_package_removed_sau_directories(self) -> None:
        spec = (app_main.PROJECT_ROOT / "build" / "HuifaVideoDownloader.velopack.spec").read_text(
            encoding="utf-8"
        )

        self.assertNotIn('(\"myUtils\", \"third_party/social_auto_upload/myUtils\")', spec)


if __name__ == "__main__":
    unittest.main()

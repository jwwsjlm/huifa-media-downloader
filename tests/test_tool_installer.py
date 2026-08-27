from __future__ import annotations

import io
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication

from app.core.tool_installer import ToolInstallError, install_tool_component
from app.core.local_components import activate_local_ejs, local_ejs_component
from app.core.external_ytdlp import (
    cached_external_ytdlp_version,
    remember_external_ytdlp_version,
)
from app.core.tool_resolver import resolve_ffprobe_tool, resolve_runtime_tool
from app.core.update_service import UpdateService, component_auto_install_supported


def write_fake_exe(path: Path, payload: bytes = b"runtime") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ" + payload)


class ToolInstallerTests(unittest.TestCase):
    @staticmethod
    def _write_ejs_wheel(path: Path, version: str = "0.8.0") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("yt_dlp_ejs/__init__.py", f'__version__ = "{version}"\n')
            archive.writestr("yt_dlp_ejs/solver.js", "export const ready = true;\n")
            archive.writestr(
                f"yt_dlp_ejs-{version}.dist-info/METADATA",
                f"Metadata-Version: 2.4\nName: yt-dlp-ejs\nVersion: {version}\n",
            )

    def test_ejs_wheel_is_installed_as_app_local_versioned_core(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "updates" / "yt_dlp_ejs-0.8.0-py3-none-any.whl"
            install_root = root / "portable"
            self._write_ejs_wheel(source)

            with patch("app.core.tool_installer._install_roots", return_value=(install_root,)):
                result = install_tool_component("yt-dlp-ejs", source)

            target = install_root / "tools" / "yt-dlp-ejs" / "yt_dlp_ejs-0.8.0-py3-none-any.whl"
            self.assertEqual(result.paths, (str(target),))
            self.assertTrue(target.is_file())
            self.assertFalse(source.exists())
            self.assertTrue(component_auto_install_supported("yt-dlp-ejs"))
            with patch("app.core.local_components.application_dir", return_value=install_root), patch(
                "app.core.local_components.tool_runtime_roots", return_value=[install_root]
            ):
                component = local_ejs_component()
                activated = activate_local_ejs()
            self.assertIsNotNone(component)
            self.assertEqual(component.version, "0.8.0")
            self.assertEqual(activated.path, str(target.resolve()))
            if str(target.resolve()) in sys.path:
                sys.path.remove(str(target.resolve()))

    def test_fresh_pc_resolves_every_download_core_from_app_local_tools(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            updates = root / "updates"
            install_root = root / "portable"
            updates.mkdir()
            ytdlp = updates / "yt-dlp.exe"
            write_fake_exe(ytdlp, b"ytdlp")
            ffmpeg = updates / "ffmpeg-master-latest-win64-gpl.zip"
            with zipfile.ZipFile(ffmpeg, "w") as archive:
                archive.writestr("ffmpeg-build/bin/ffmpeg.exe", b"MZffmpeg")
                archive.writestr("ffmpeg-build/bin/ffprobe.exe", b"MZffprobe")
            deno = updates / "deno-x86_64-pc-windows-msvc.zip"
            with zipfile.ZipFile(deno, "w") as archive:
                archive.writestr("deno.exe", b"MZdeno")
            ejs = updates / "yt_dlp_ejs-0.8.0-py3-none-any.whl"
            self._write_ejs_wheel(ejs)

            with patch("app.core.tool_installer._install_roots", return_value=(install_root,)):
                for component, source in (
                    ("yt-dlp", ytdlp), ("FFmpeg", ffmpeg), ("Deno", deno), ("yt-dlp-ejs", ejs)
                ):
                    install_tool_component(component, source)

            resolver_kwargs = {
                "application_root": install_root,
                "runtime_roots": (install_root,),
                "environment": {},
                "which": lambda _command: None,
            }
            yt_resolution = resolve_runtime_tool("yt-dlp", **resolver_kwargs)
            ffmpeg_resolution = resolve_runtime_tool("FFmpeg", **resolver_kwargs)
            deno_resolution = resolve_runtime_tool("Deno", **resolver_kwargs)
            probe_resolution = resolve_ffprobe_tool("", application_root=install_root, runtime_roots=(install_root,), environment={}, which=lambda _command: None)
            with patch("app.core.local_components.application_dir", return_value=install_root), patch(
                "app.core.local_components.tool_runtime_roots", return_value=[install_root]
            ):
                ejs_component = local_ejs_component()

        self.assertTrue(yt_resolution.found)
        self.assertTrue(ffmpeg_resolution.found)
        self.assertTrue(deno_resolution.found)
        self.assertTrue(probe_resolution.found)
        self.assertIsNotNone(ejs_component)

    def test_external_ytdlp_is_installed_as_independently_updatable_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "updates" / "yt-dlp.exe"
            install_root = root / "portable"
            write_fake_exe(source, b"new")
            target = install_root / "tools" / "yt-dlp" / "x64" / "yt-dlp.exe"
            write_fake_exe(target, b"old")

            with patch("app.core.tool_installer._install_roots", return_value=(install_root,)):
                result = install_tool_component("yt-dlp", source)

            self.assertEqual(result.paths, (str(target),))
            self.assertEqual(target.read_bytes(), b"MZnew")
            self.assertFalse(source.exists())
            self.assertFalse(target.with_name("yt-dlp.exe.new").exists())
            self.assertFalse(target.with_name("yt-dlp.exe.previous").exists())

    def test_ffmpeg_zip_installs_only_ffmpeg_and_ffprobe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "ffmpeg-win64.zip"
            install_root = root / "portable"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("ffmpeg-build/bin/ffmpeg.exe", b"MZffmpeg")
                archive.writestr("ffmpeg-build/bin/ffprobe.exe", b"MZffprobe")
                archive.writestr("ffmpeg-build/bin/ffplay.exe", b"MZffplay")
                archive.writestr("ffmpeg-build/doc/manual.html", b"not shipped")

            with patch("app.core.tool_installer._install_roots", return_value=(install_root,)):
                result = install_tool_component("FFmpeg", source)

            installed = {Path(path).name for path in result.paths}
            self.assertEqual(installed, {"ffmpeg.exe", "ffprobe.exe"})
            self.assertFalse(any(path.name == "ffplay.exe" for path in install_root.rglob("*")))
            self.assertFalse(any(path.name == "manual.html" for path in install_root.rglob("*")))
            self.assertFalse(source.exists())

    def test_deno_zip_uses_portable_deno_executable_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "deno-x86_64-pc-windows-msvc.zip"
            install_root = root / "portable"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("deno.exe", b"MZdeno")
                archive.writestr("deno.exe.sha256", b"not required")

            with patch("app.core.tool_installer._install_roots", return_value=(install_root,)):
                result = install_tool_component("Deno", source)

            self.assertEqual(len(result.paths), 1)
            self.assertEqual(Path(result.paths[0]).name, "deno.exe")
            self.assertFalse(any(path.name == "deno.exe.sha256" for path in install_root.rglob("*")))

    def test_invalid_or_incomplete_archive_never_replaces_existing_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "ffmpeg.zip"
            install_root = root / "portable"
            existing = install_root / "tools" / "ffmpeg" / "x64" / "ffmpeg.exe"
            write_fake_exe(existing, b"old")
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("../ffmpeg.exe", b"MZuntrusted")
                archive.writestr("bin/ffprobe.exe", b"MZprobe")

            with patch("app.core.tool_installer._install_roots", return_value=(install_root,)):
                with self.assertRaisesRegex(ToolInstallError, "缺少必需文件"):
                    install_tool_component("FFmpeg", source)

            self.assertEqual(existing.read_bytes(), b"MZold")
            self.assertTrue(source.exists())

    def test_vendored_sau_is_not_treated_as_an_external_installable_core(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "social-auto-upload-20260825-abcdef123456-win64.zip"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("social-auto-upload-abcdef123456/sau_cli.py", "def main(): pass\n")

            with self.assertRaisesRegex(ToolInstallError, "不支持"):
                install_tool_component("social-auto-upload", source)
            self.assertTrue(source.exists())
            self.assertFalse(component_auto_install_supported("social-auto-upload"))

    def test_update_service_downloads_then_installs_without_blocking_main_thread(self) -> None:
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("deno.exe", b"MZportable-deno")

        class FakeResponse:
            url = "https://release-assets.githubusercontent.com/runtime/deno-x86_64-pc-windows-msvc.zip"

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def raise_for_status(self) -> None:
                return None

            def iter_content(self, chunk_size=0):
                yield payload.getvalue()

        app = QCoreApplication.instance() or QCoreApplication([])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install_root = root / "portable"
            service = UpdateService(root / "updates")
            installed = []
            errors = []
            service.install_finished.connect(installed.append)
            service.install_failed.connect(errors.append)
            installed_path = install_root / "tools" / "yt-dlp" / "x64" / "yt-dlp.exe"
            remember_external_ytdlp_version(installed_path, "")
            asset = {
                "name": "deno-x86_64-pc-windows-msvc.zip",
                "browser_download_url": "https://github.com/denoland/deno/releases/download/v99/deno-x86_64-pc-windows-msvc.zip",
            }
            with patch("app.core.update_service.requests.get", return_value=FakeResponse()), patch(
                "app.core.tool_installer._install_roots", return_value=(install_root,)
            ):
                service.download_asset(asset, "Deno")
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and not installed and not errors:
                    app.processEvents()
                    time.sleep(0.01)
                for _ in range(10):
                    app.processEvents()

            self.assertFalse(errors)
            self.assertEqual(len(installed), 1)
            deno = next(install_root.rglob("deno.exe"))
            self.assertEqual(deno.read_bytes(), b"MZportable-deno")
            self.assertTrue(service.shutdown(timeout_ms=1000))

    def test_update_service_downloads_and_installs_external_ytdlp(self) -> None:
        class FakeResponse:
            url = "https://release-assets.githubusercontent.com/runtime/yt-dlp.exe"

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def raise_for_status(self) -> None:
                return None

            def iter_content(self, chunk_size=0):
                yield b"MZstandalone-ytdlp"

        app = QCoreApplication.instance() or QCoreApplication([])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install_root = root / "portable"
            service = UpdateService(root / "updates")
            installed = []
            errors: list[str] = []
            service.install_finished.connect(installed.append)
            service.install_failed.connect(errors.append)
            asset = {
                "name": "yt-dlp.exe",
                "browser_download_url": "https://github.com/yt-dlp/yt-dlp/releases/download/v1/yt-dlp.exe",
            }
            with patch("app.core.update_service.requests.get", return_value=FakeResponse()), patch(
                "app.core.tool_installer._install_roots", return_value=(install_root,)
            ):
                service.download_asset(asset, "yt-dlp")
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and not installed and not errors:
                    app.processEvents()
                    time.sleep(0.01)
                for _ in range(10):
                    app.processEvents()

            self.assertFalse(errors)
            self.assertEqual(len(installed), 1)
            executable = next(install_root.rglob("yt-dlp.exe"))
            self.assertEqual(executable.read_bytes(), b"MZstandalone-ytdlp")
            self.assertIsNone(cached_external_ytdlp_version(executable))
            self.assertTrue(service.shutdown(timeout_ms=1000))

    def test_update_service_downloads_ffmpeg_release_and_updates_both_local_tools(self) -> None:
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("ffmpeg-build/bin/ffmpeg.exe", b"MZnew-ffmpeg")
            archive.writestr("ffmpeg-build/bin/ffprobe.exe", b"MZnew-ffprobe")

        class FakeResponse:
            url = "https://release-assets.githubusercontent.com/runtime/ffmpeg-master-latest-win64-gpl.zip"

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def raise_for_status(self) -> None:
                return None

            def iter_content(self, chunk_size=0):
                yield payload.getvalue()

        app = QCoreApplication.instance() or QCoreApplication([])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install_root = root / "portable"
            old_ffmpeg = install_root / "tools" / "ffmpeg" / "x64" / "ffmpeg.exe"
            old_ffprobe = install_root / "tools" / "ffmpeg" / "x64" / "ffprobe.exe"
            write_fake_exe(old_ffmpeg, b"old-ffmpeg")
            write_fake_exe(old_ffprobe, b"old-ffprobe")
            service = UpdateService(root / "updates")
            installed = []
            errors: list[str] = []
            service.install_finished.connect(installed.append)
            service.install_failed.connect(errors.append)
            asset = {
                "name": "ffmpeg-master-latest-win64-gpl.zip",
                "browser_download_url": (
                    "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/"
                    "ffmpeg-master-latest-win64-gpl.zip"
                ),
            }
            with patch("app.core.update_service.requests.get", return_value=FakeResponse()), patch(
                "app.core.tool_installer._install_roots", return_value=(install_root,)
            ):
                service.download_asset(asset, "FFmpeg")
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and not installed and not errors:
                    app.processEvents()
                    time.sleep(0.01)
                for _ in range(10):
                    app.processEvents()

            self.assertFalse(errors)
            self.assertEqual(len(installed), 1)
            self.assertEqual(old_ffmpeg.read_bytes(), b"MZnew-ffmpeg")
            self.assertEqual(old_ffprobe.read_bytes(), b"MZnew-ffprobe")
            self.assertFalse(old_ffmpeg.with_name("ffmpeg.exe.previous").exists())
            self.assertFalse(old_ffprobe.with_name("ffprobe.exe.previous").exists())
            self.assertTrue(service.shutdown(timeout_ms=1000))


if __name__ == "__main__":
    unittest.main()

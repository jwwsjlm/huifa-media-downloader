from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from verify_single_exe_release import ReleaseLayoutError, validate_single_exe_release

from app.core.tool_resolver import resolve_ffprobe_tool, resolve_runtime_tool


class ReleaseDeliveryContractTests(unittest.TestCase):
    def test_single_exe_verifier_accepts_only_exe_and_ignores_runtime_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            release = Path(directory)
            executable = release / "HuifaVideoDownloader.exe"
            executable.write_bytes(b"MZ" + b"release")
            runtime_data = release / "data"
            runtime_data.mkdir()
            # A diagnostic archive is user data, not a top-level delivery
            # artifact. The build validator must neither reject nor delete it.
            (runtime_data / "diagnostics.zip").write_bytes(b"local")

            validated = validate_single_exe_release(release)

            self.assertEqual(validated, executable.resolve())
            self.assertTrue((runtime_data / "diagnostics.zip").is_file())

    def test_single_exe_verifier_rejects_top_level_archive_or_extra_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            release = Path(directory)
            (release / "HuifaVideoDownloader.exe").write_bytes(b"MZrelease")
            (release / "HuifaVideoDownloader-win-x64.zip").write_bytes(b"archive")
            with self.assertRaisesRegex(ReleaseLayoutError, "顶层只能包含"):
                validate_single_exe_release(release)

    def test_single_exe_verifier_rejects_empty_or_non_windows_binary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            release = Path(directory)
            executable = release / "HuifaVideoDownloader.exe"
            executable.write_bytes(b"")
            with self.assertRaisesRegex(ReleaseLayoutError, "为空"):
                validate_single_exe_release(release)
            executable.write_bytes(b"not-a-pe")
            with self.assertRaisesRegex(ReleaseLayoutError, "Windows EXE"):
                validate_single_exe_release(release)

    def test_primary_build_never_invokes_archive_or_velopack_pipeline(self) -> None:
        script = (ROOT / "scripts" / "build_release.ps1").read_text(encoding="utf-8")
        spec = (ROOT / "build" / "HuifaVideoDownloader.lean.spec").read_text(encoding="utf-8")
        self.assertNotIn("Compress-Archive", script)
        self.assertNotIn("System.IO.Compression", script)
        self.assertNotIn("build_velopack_release.ps1", script)
        self.assertNotIn("HuifaVideoDownloader.velopack.spec", script)
        self.assertNotIn("vpk pack", script)
        self.assertIn("verify_single_exe_release.py", script)
        self.assertIn("--release-dir", script)
        self.assertNotIn("COLLECT(", spec)

    def test_user_portable_is_velopack_managed_and_stages_visible_tools(self) -> None:
        build = (ROOT / "scripts" / "build_velopack_release.ps1").read_text(
            encoding="utf-8"
        )
        package = (ROOT / "scripts" / "package_github_release.ps1").read_text(
            encoding="utf-8"
        )
        spec = (ROOT / "build" / "HuifaVideoDownloader.velopack.spec").read_text(
            encoding="utf-8"
        )

        self.assertIn("Stage-PortableRuntimeTools", build)
        for relative in (
            "ffmpeg\\x64\\ffmpeg.exe",
            "ffmpeg\\x64\\ffprobe.exe",
            "yt-dlp\\x64\\yt-dlp.exe",
            "deno\\x64\\deno.exe",
            "yt-dlp-ejs",
            "chromium\\chrome-win64",
        ):
            self.assertIn(relative, build)
        self.assertIn("application_update_mode -ne 'velopack'", build)
        self.assertIn("portable-update-preserve.txt", build)
        self.assertIn("Velopack update did not restore the bundled Deno runtime", build)
        self.assertIn("ArgumentList.Add", build)
        self.assertIn("Huifa.VideoDownloader*-Portable.zip", package)
        self.assertIn("current/sq.version", package)
        self.assertIn("^\\.portable$", package)
        self.assertNotIn('ffmpeg_dir = PROJECT_ROOT / "tools"', spec)
        self.assertNotIn('chromium_dir = PROJECT_ROOT / "tools"', spec)

    def test_portable_external_tools_always_prefer_exe_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fallback_root = root / "persistent"
            configured_root = root / "configured"
            path_root = root / "path"
            for folder in (fallback_root, configured_root, path_root):
                folder.mkdir()

            for component, filename, command in (
                ("FFmpeg", "ffmpeg.exe", "ffmpeg"),
                ("Deno", "deno.exe", "deno"),
            ):
                local = root / filename
                configured = configured_root / filename
                bundled = fallback_root / filename
                system_path = path_root / filename
                for executable in (local, configured, bundled, system_path):
                    executable.write_bytes(b"placeholder")

                resolution = resolve_runtime_tool(
                    component,
                    str(configured),
                    application_root=root,
                    runtime_roots=(fallback_root,),
                    which=lambda requested, expected=command, path=system_path: (
                        str(path) if requested == expected else None
                    ),
                )

                self.assertEqual(Path(resolution.executable), local.resolve())
                self.assertEqual(resolution.source, f"程序目录 {filename}")

    def test_native_windows_arm64_uses_arm64_runtime_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arm64 = root / "tools" / "deno" / "arm64" / "deno.exe"
            x64 = root / "tools" / "deno" / "x64" / "deno.exe"
            for executable in (arm64, x64):
                executable.parent.mkdir(parents=True, exist_ok=True)
                executable.write_bytes(b"placeholder")

            with patch(
                "app.core.tool_resolver.platform.machine",
                return_value="ARM64",
            ):
                resolution = resolve_runtime_tool(
                    "Deno",
                    application_root=root,
                    runtime_roots=(),
                    environment={},
                    which=lambda _command: None,
                )

        self.assertEqual(Path(resolution.executable), arm64.resolve())
        self.assertEqual(
            resolution.source,
            "程序目录 tools/deno/arm64/deno.exe",
        )

    def test_configured_ffmpeg_uses_its_sibling_ffprobe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            custom = root / "custom"
            custom.mkdir()
            ffmpeg = custom / "ffmpeg.exe"
            ffprobe = custom / "ffprobe.exe"
            ffmpeg.write_bytes(b"MZffmpeg")
            ffprobe.write_bytes(b"MZffprobe")

            resolution = resolve_ffprobe_tool(
                str(ffmpeg),
                application_root=root,
                runtime_roots=(root,),
                which=lambda _command: None,
            )

        self.assertEqual(Path(resolution.executable), ffprobe)
        self.assertIn("配套 ffprobe.exe", resolution.source)

    def test_explicit_ffprobe_path_overrides_ffmpeg_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ffmpeg_dir = root / "ffmpeg"
            probe_dir = root / "probe"
            ffmpeg_dir.mkdir()
            probe_dir.mkdir()
            ffmpeg = ffmpeg_dir / "ffmpeg.exe"
            sibling = ffmpeg_dir / "ffprobe.exe"
            explicit = probe_dir / "ffprobe.exe"
            for executable in (ffmpeg, sibling, explicit):
                executable.write_bytes(b"MZtool")

            resolution = resolve_ffprobe_tool(
                str(ffmpeg),
                str(explicit),
                application_root=root,
                runtime_roots=(root,),
                which=lambda _command: None,
            )

        self.assertEqual(Path(resolution.executable), explicit)
        self.assertIn("设置路径", resolution.source)


if __name__ == "__main__":
    unittest.main()

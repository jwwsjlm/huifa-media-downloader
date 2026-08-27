from __future__ import annotations

import sys
import unittest
from pathlib import Path

from app.core.app_settings import default_settings
from app.core.version import APP_VERSION


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from create_github_release_body import build_release_body


class GithubReleaseWorkflowTests(unittest.TestCase):
    def test_default_application_update_repository_is_the_public_project(self) -> None:
        self.assertEqual(
            default_settings("data/downloads")["update_repo"],
            "https://github.com/jwwsjlm/huifa-media-downloader",
        )

    def test_workflows_use_current_official_action_major_versions(self) -> None:
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        release = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        for source in (ci, release):
            self.assertIn("actions/checkout@v7", source)
            self.assertIn("actions/setup-python@v7", source)
        self.assertIn("actions/upload-artifact@v7", release)

    def test_tag_release_builds_both_zip_editions_and_update_assets(self) -> None:
        release = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("tags:", release)
        self.assertIn("v[0-9]+.[0-9]+.[0-9]+*", release)
        self.assertIn("contents: write", release)
        self.assertIn("prepare_release_runtime.ps1", release)
        self.assertIn("build_release.ps1", release)
        self.assertIn("build_velopack_release.ps1", release)
        self.assertIn("package_github_release.ps1", release)
        self.assertIn("create_github_release_body.py", release)
        self.assertIn("GITHUB_RELEASE.md", release)
        self.assertIn("release-notes\\$Version.md", release)
        self.assertIn("--notes-file", release)
        self.assertNotIn("--generate-notes", release)
        self.assertIn("release-assets/*", release)
        self.assertIn("gh release create", release)
        self.assertIn("gh release upload", release)
        self.assertIn("--clobber", release)
        self.assertIn("--verify-tag", release)
        self.assertIn("--latest", release)

    def test_release_page_puts_direct_user_downloads_before_notes(self) -> None:
        body = build_release_body(
            "jwwsjlm/huifa-media-downloader",
            "v0.1.1",
            "0.1.1",
            "# Huifa Media Downloader 0.1.1\n\n- Notes",
        )
        portable = (
            "https://github.com/jwwsjlm/huifa-media-downloader/releases/download/"
            "v0.1.1/HuifaMediaDownloader-0.1.1-portable-win-x64.zip"
        )
        self.assertIn("Huifa Media Downloader.exe", body)
        installer = (
            "https://github.com/jwwsjlm/huifa-media-downloader/releases/download/"
            "v0.1.1/HuifaMediaDownloader-0.1.1-installer-win-x64.zip"
        )
        self.assertIn("## 直接下载 / Direct downloads", body)
        self.assertIn(portable, body)
        self.assertIn(installer, body)
        self.assertIn("普通用户只需选择下面一种版本", body)
        self.assertLess(body.index(portable), body.index("# Huifa Media Downloader"))

    def test_release_packager_keeps_two_user_zip_files_and_both_update_protocols(self) -> None:
        source = (ROOT / "scripts" / "package_github_release.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("portable-win-x64.zip", source)
        self.assertIn("installer-win-x64.zip", source)
        self.assertIn("Huifa.VideoDownloader*-Portable.zip", source)
        self.assertIn("current/tools/ffmpeg/x64/ffmpeg.exe", source)
        self.assertIn("current/tools/yt-dlp/x64/yt-dlp.exe", source)
        self.assertIn("current/tools/deno/x64/deno.exe", source)
        self.assertIn("current/tools/yt-dlp-ejs/yt_dlp_ejs-", source)
        self.assertIn("current/tools/chromium/chrome-win64/chrome.exe", source)
        self.assertIn("HuifaMediaDownloader-Setup.exe", source)
        self.assertIn("RELEASE_NOTES.md", source)
        self.assertIn("HuifaVideoDownloader.exe", source)
        self.assertIn("releases.win.json", source)
        self.assertIn("assets.win.json", source)
        self.assertIn("SHA256SUMS.txt", source)

    def test_current_version_has_non_empty_bilingual_release_notes(self) -> None:
        notes = ROOT / "release-notes" / f"{APP_VERSION}.md"
        self.assertTrue(notes.is_file())
        source = notes.read_text(encoding="utf-8")
        self.assertIn(f"Huifa Media Downloader {APP_VERSION}", source)
        self.assertIn("## 中文", source)
        self.assertIn("## English", source)

    def test_release_runtime_bootstrap_uses_official_github_api_and_digest(self) -> None:
        source = (ROOT / "scripts" / "prepare_release_runtime.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("api.github.com/repos/$Repository/releases/latest", source)
        self.assertIn("yt-dlp/FFmpeg-Builds", source)
        self.assertIn("ffmpeg-master-latest-win64-gpl.zip", source)
        self.assertIn("yt-dlp/yt-dlp", source)
        self.assertIn("yt-dlp.exe", source)
        self.assertIn("denoland/deno", source)
        self.assertIn("deno-x86_64-pc-windows-msvc.zip", source)
        self.assertIn("yt-dlp/ejs", source)
        self.assertIn("yt_dlp_ejs-", source)
        self.assertIn("2026-03-10", source)
        self.assertIn("GITHUB_TOKEN", source)
        self.assertIn("Headers.Authorization", source)
        self.assertIn("Asset.digest", source)
        self.assertIn("playwright install chromium", source)

    def test_downloaded_release_runtimes_are_not_committed(self) -> None:
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("/tools/chromium/", ignore)
        self.assertIn("/tools/ffmpeg/**/*.exe", ignore)
        self.assertIn("/tools/ffmpeg/**/*.dll", ignore)
        self.assertIn("/tools/deno/", ignore)
        self.assertIn("/tools/yt-dlp/", ignore)
        self.assertIn("/tools/yt-dlp-ejs/", ignore)


if __name__ == "__main__":
    unittest.main()

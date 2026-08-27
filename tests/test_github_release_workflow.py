from __future__ import annotations

import unittest
from pathlib import Path

from app.core.app_settings import default_settings


ROOT = Path(__file__).resolve().parents[1]


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

    def test_tag_release_builds_exact_asset_required_by_portable_updater(self) -> None:
        release = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("tags:", release)
        self.assertIn("v[0-9]+.[0-9]+.[0-9]+*", release)
        self.assertIn("contents: write", release)
        self.assertIn("prepare_release_runtime.ps1", release)
        self.assertIn("build_release.ps1", release)
        self.assertIn("HuifaVideoDownloader.exe", release)
        self.assertIn("gh release create", release)
        self.assertIn("gh release upload", release)
        self.assertIn("--clobber", release)
        self.assertIn("--verify-tag", release)
        self.assertIn("--latest", release)

    def test_release_runtime_bootstrap_uses_official_github_api_and_digest(self) -> None:
        source = (ROOT / "scripts" / "prepare_release_runtime.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("api.github.com/repos/$Repository/releases/latest", source)
        self.assertIn("yt-dlp/FFmpeg-Builds", source)
        self.assertIn("ffmpeg-master-latest-win64-gpl.zip", source)
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


if __name__ == "__main__":
    unittest.main()

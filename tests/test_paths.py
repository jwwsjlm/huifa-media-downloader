from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.paths import downloads_dir, portable_deployment
from app.core.version import APP_NAME_EN


class DeploymentDownloadPathTests(unittest.TestCase):
    @staticmethod
    def _managed_layout(root: Path, *, portable: bool) -> Path:
        current = root / "current"
        current.mkdir(parents=True)
        (root / "Update.exe").write_bytes(b"")
        (current / "sq.version").write_text("{}", encoding="utf-8")
        if portable:
            (root / ".portable").write_bytes(b"")
        return current

    def test_directory_portable_downloads_live_beside_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = self._managed_layout(root, portable=True)
            with patch("app.core.paths.application_dir", return_value=current):
                self.assertTrue(portable_deployment())
                self.assertEqual(downloads_dir(), root / "downloads")
                self.assertFalse((root / "data" / "downloads").exists())

    def test_installed_downloads_live_outside_velopack_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            current = self._managed_layout(base / "installed", portable=False)
            user_downloads = base / "user-downloads"
            with patch("app.core.paths.application_dir", return_value=current), patch(
                "app.core.paths.system_downloads_dir", return_value=user_downloads
            ):
                self.assertFalse(portable_deployment())
                self.assertEqual(downloads_dir(), user_downloads / APP_NAME_EN)
                self.assertFalse((base / "installed" / "data" / "downloads").exists())

    def test_source_and_legacy_portable_keep_root_downloads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("app.core.paths.application_dir", return_value=root):
                self.assertTrue(portable_deployment())
                self.assertEqual(downloads_dir(), root / "downloads")


if __name__ == "__main__":
    unittest.main()

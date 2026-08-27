from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.core.browser_cookies import BrowserCookie, CookieVault
from app.core.cookie_sources import COOKIE_SOURCE_EMBEDDED, EMBEDDED_DOWNLOAD_PROFILE
from app.core.download_service import DownloadWorker


class EmbeddedCookieDownloadIntegrationTests(unittest.TestCase):
    def test_cookie_export_is_removed_if_worker_setup_fails_after_materialization(self) -> None:
        import app.core.download_service as download_module

        with tempfile.TemporaryDirectory() as directory:
            cookie_path = Path(directory) / "temporary-cookies.txt"
            cookie_path.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
            vault = SimpleNamespace(
                create_temporary_netscape_file=lambda _profile: cookie_path,
            )
            worker = DownloadWorker(
                "cookie-setup-failure",
                "https://example.com/video",
                directory,
                object(),
                cookie_source=COOKIE_SOURCE_EMBEDDED,
            )

            with patch(
                "app.core.browser_cookies.CookieVault",
                return_value=vault,
            ), patch.object(worker, "_log", side_effect=RuntimeError("log failed")):
                with self.assertRaisesRegex(RuntimeError, "Cookie"):
                    worker._configure_cookie_options({})

            self.assertFalse(cookie_path.exists())

    @unittest.skipUnless(os.name == "nt", "CookieVault uses Windows DPAPI")
    def test_download_worker_passes_vault_cookies_to_ytdlp_and_deletes_temp_file(self) -> None:
        from yt_dlp.cookies import load_cookies
        import app.core.download_service as download_module

        secret = "integration-cookie-secret"
        captured: dict[str, object] = {}
        log_records: list[tuple[str, str, str, dict]] = []

        class FakeYoutubeDL:
            def __init__(self, options):
                self.params = dict(options)
                captured["options"] = self.params

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def extract_info(self, url, download):
                cookie_path = Path(self.params["cookiefile"])
                captured["cookie_path"] = cookie_path
                captured["cookie_exists_during_call"] = cookie_path.is_file()
                jar = load_cookies(str(cookie_path), None, None)
                captured["cookie_values"] = {
                    cookie.name: cookie.value for cookie in jar
                }
                return {
                    "id": "cookie-test",
                    "title": "Cookie Test",
                    "ext": "mp4",
                    "webpage_url": url,
                }

            @staticmethod
            def prepare_filename(_info):
                return "cookie-test.mp4"

        fake_ytdlp = SimpleNamespace(
            YoutubeDL=FakeYoutubeDL,
            utils=SimpleNamespace(DownloadError=RuntimeError),
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "downloads"
            vault = CookieVault(root / "vault")
            vault.save(
                EMBEDDED_DOWNLOAD_PROFILE,
                [
                    BrowserCookie(
                        "sessionid",
                        secret,
                        ".example.com",
                        secure=True,
                        http_only=True,
                        expires=2_000_000_000,
                    )
                ],
            )
            worker = DownloadWorker(
                "cookie-task",
                "https://example.com/video",
                str(output_dir),
                object(),
                playlist_mode="single",
                cookie_source=COOKIE_SOURCE_EMBEDDED,
                ytdlp_core_mode="builtin",
            )
            failures: list[tuple[str, str]] = []
            worker.failed.connect(lambda *args: failures.append(args))

            with patch.object(download_module, "yt_dlp", fake_ytdlp), patch(
                "app.core.browser_cookies.CookieVault", return_value=vault
            ), patch.object(
                worker,
                "_check_disk_low_watermark",
                return_value=None,
            ), patch.object(
                worker,
                "_complete_download_info",
                return_value=None,
            ), patch.object(
                worker,
                "_log",
                side_effect=lambda level, category, message, **details: log_records.append(
                    (level, category, message, details)
                ),
            ), patch.object(
                download_module,
                "ytdlp_ejs_runtime_options",
                return_value=({}, "", "auto"),
            ), patch.object(
                download_module,
                "ffmpeg_runtime_path",
                return_value="",
            ):
                worker.run()

            cookie_path = captured["cookie_path"]
            self.assertTrue(captured["cookie_exists_during_call"])
            self.assertEqual(captured["cookie_values"], {"sessionid": secret})
            self.assertFalse(Path(cookie_path).exists())
            self.assertEqual(failures, [])

        rendered_logs = repr(log_records)
        self.assertNotIn(secret, rendered_logs)
        self.assertTrue(Path(captured["options"]["cookiefile"]).name.startswith("huifa-cookie-"))

    @unittest.skipUnless(os.name == "nt", "CookieVault uses Windows DPAPI")
    def test_missing_embedded_cookie_profile_fails_before_ytdlp(self) -> None:
        import app.core.download_service as download_module

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vault = CookieVault(root / "vault")
            worker = DownloadWorker(
                "missing-cookie-task",
                "https://example.com/video",
                str(root / "downloads"),
                object(),
                playlist_mode="single",
                cookie_source=COOKIE_SOURCE_EMBEDDED,
                ytdlp_core_mode="builtin",
            )
            failures: list[tuple[str, str]] = []
            worker.failed.connect(lambda *args: failures.append(args))
            fake_ytdlp = SimpleNamespace(
                YoutubeDL=unittest.mock.MagicMock(),
                utils=SimpleNamespace(DownloadError=RuntimeError),
            )
            with patch.object(download_module, "yt_dlp", fake_ytdlp), patch(
                "app.core.browser_cookies.CookieVault", return_value=vault
            ), patch.object(
                worker,
                "_check_disk_low_watermark",
                return_value=None,
            ), patch.object(
                download_module,
                "ytdlp_ejs_runtime_options",
                return_value=({}, "", "auto"),
            ), patch.object(
                download_module,
                "ffmpeg_runtime_path",
                return_value="",
            ):
                worker.run()

            self.assertEqual(len(failures), 1)
            self.assertIn("Cookie", failures[0][1])
            fake_ytdlp.YoutubeDL.assert_not_called()


if __name__ == "__main__":
    unittest.main()

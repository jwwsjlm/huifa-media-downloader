from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QObject
from PySide6.QtWidgets import QApplication

from app.core.browser_cookies import (
    BrowserCookie,
    CookieVault,
    CookieVaultError,
    cleanup_stale_cookie_exports,
    cookies_from_playwright,
    netscape_cookie_text,
)
from app.core.publish_service import AccountWorker
from app.ui.embedded_browser import CookieViewerDialog


class BrowserCookieFormatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        existing = QCoreApplication.instance()
        if existing is not None and not isinstance(existing, QApplication):
            raise unittest.SkipTest("a non-GUI QCoreApplication already exists")
        cls.app = existing or QApplication([])

    @unittest.skipUnless(os.name == "nt", "CookieVault uses Windows DPAPI")
    def test_dpapi_vault_and_ytdlp_temporary_cookie_file_round_trip(self) -> None:
        from yt_dlp.cookies import load_cookies

        with tempfile.TemporaryDirectory() as directory:
            vault = CookieVault(Path(directory) / "vault")
            source = BrowserCookie(
                "session",
                "secret-value",
                ".example.com",
                expires=2_000_000_000,
                http_only=True,
                secure=True,
            )
            encrypted_path = vault.save("download", [source])
            self.assertNotIn(b"secret-value", encrypted_path.read_bytes())
            self.assertEqual(vault.load("download"), [source])
            temporary = vault.create_temporary_netscape_file("download")
            try:
                jar = load_cookies(str(temporary), None, None)
                loaded = next(cookie for cookie in jar if cookie.name == "session")
                self.assertEqual(loaded.value, "secret-value")
                self.assertTrue(loaded.secure)
            finally:
                temporary.unlink(missing_ok=True)

    def test_playwright_cookie_import_normalizes_browser_state(self) -> None:
        restored = cookies_from_playwright(
            {
                "cookies": [
                    {
                        "name": "session",
                        "value": "secret",
                        "domain": ".example.com",
                        "path": "/",
                        "expires": 2_000_000_000,
                        "httpOnly": True,
                        "secure": True,
                        "sameSite": "Strict",
                    }
                ]
            }
        )
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0].name, "session")
        self.assertTrue(restored[0].http_only)
        self.assertTrue(restored[0].secure)

    def test_invalid_expiry_and_malformed_domain_are_discarded_safely(self) -> None:
        normalized = BrowserCookie(
            "sid",
            "value",
            ".example.com",
            expires=float("inf"),
        ).normalized()

        self.assertIsNotNone(normalized)
        self.assertEqual(normalized.expires, 0)
        self.assertIsNone(
            BrowserCookie("sid", "value", "https://[invalid").normalized()
        )

    def test_cookie_viewer_groups_domains_and_hides_values_by_default(self) -> None:
        cookies = [
            BrowserCookie(
                "sid", "private-douyin", ".douyin.com", secure=True, http_only=True
            ),
            BrowserCookie("theme", "dark", ".douyin.com", path="/creator"),
            BrowserCookie("session", "private-youtube", ".youtube.com"),
        ]
        dialog = CookieViewerDialog(lambda: cookies, profile_id="download")
        try:
            self.assertEqual(dialog.tree.topLevelItemCount(), 2)
            domains = {
                dialog.tree.topLevelItem(index).text(0): dialog.tree.topLevelItem(index)
                for index in range(dialog.tree.topLevelItemCount())
            }
            self.assertEqual(domains[".douyin.com"].childCount(), 2)
            self.assertEqual(domains[".youtube.com"].childCount(), 1)
            self.assertEqual(domains[".douyin.com"].child(0).text(1), "••••••••")
            self.assertNotIn("private-douyin", dialog.summary.text())

            dialog.show_values.setChecked(True)
            self.app.processEvents()
            # Refresh rebuilds the tree, so query the new group after toggling.
            douyin_group = next(
                dialog.tree.topLevelItem(index)
                for index in range(dialog.tree.topLevelItemCount())
                if dialog.tree.topLevelItem(index).text(0) == ".douyin.com"
            )
            visible_values = {
                douyin_group.child(index).text(1)
                for index in range(douyin_group.childCount())
            }
            self.assertIn("private-douyin", visible_values)

            dialog.search.setText("youtube")
            self.app.processEvents()
            self.assertEqual(dialog.tree.topLevelItemCount(), 1)
            self.assertEqual(dialog.tree.topLevelItem(0).text(0), ".youtube.com")
        finally:
            dialog.close()

    @unittest.skipUnless(os.name == "nt", "CookieVault uses Windows DPAPI")
    def test_persistent_snapshot_is_restored_after_new_vault_instance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "vault"
            source = [
                BrowserCookie(
                    "sid",
                    "secret",
                    ".douyin.com",
                    expires=int(datetime.now().timestamp()) + 3600,
                )
            ]
            first_session = CookieVault(root)
            first_session.save("download", source)

            restarted_session = CookieVault(root)
            restored = restarted_session.load("download")

            self.assertEqual(restored, source)

    def test_unreadable_vault_is_backed_up_and_replaced_by_empty_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "vault"
            vault = CookieVault(root)
            path = vault.path_for("download")
            path.parent.mkdir(parents=True)
            path.write_bytes(b"not a valid DPAPI payload")

            self.assertEqual(vault.load("download"), [])
            self.assertFalse(path.exists())
            backups = list(root.glob(path.name + ".unreadable-*.bak"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), b"not a valid DPAPI payload")

            # The notice survives a short-lived count/check instance and is
            # shown when the actual embedded browser is opened later.
            restarted = CookieVault(root)
            notice = restarted.consume_recovery_notice("download")
            self.assertIn("请重新登录", notice)
            self.assertEqual(restarted.consume_recovery_notice("download"), "")

    def test_transient_read_failure_preserves_vault_instead_of_quarantining_it(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "vault"
            vault = CookieVault(root)
            path = vault.path_for("download")
            path.parent.mkdir(parents=True)
            path.write_bytes(b"encrypted-cookie-vault")

            with patch.object(
                Path,
                "read_bytes",
                side_effect=PermissionError("temporarily locked"),
            ):
                with self.assertRaisesRegex(CookieVaultError, "原文件保持不变"):
                    vault.load("download")

            self.assertEqual(path.read_bytes(), b"encrypted-cookie-vault")
            self.assertEqual(list(root.glob(path.name + ".unreadable-*.bak")), [])

    def test_failed_quarantine_never_returns_an_empty_overwritable_session(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "vault"
            vault = CookieVault(root)
            path = vault.path_for("download")
            path.parent.mkdir(parents=True)
            path.write_bytes(b"corrupt-cookie-vault")

            with patch(
                "app.core.browser_cookies._dpapi",
                side_effect=ValueError("corrupt"),
            ), patch(
                "app.core.browser_cookies.os.replace",
                side_effect=PermissionError("backup blocked"),
            ):
                with self.assertRaisesRegex(CookieVaultError, "未启动空会话"):
                    vault.load("download")

            self.assertEqual(path.read_bytes(), b"corrupt-cookie-vault")
            self.assertEqual(list(root.glob(path.name + ".unreadable-*.bak")), [])

    def test_load_and_save_from_separate_instances_share_one_profile_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "vault"
            reader = CookieVault(root)
            writer = CookieVault(root)
            path = reader.path_for("download")
            path.parent.mkdir(parents=True)
            path.write_bytes(b"corrupt-cookie-vault")
            decrypt_started = threading.Event()
            allow_decrypt_failure = threading.Event()
            writer_finished = threading.Event()
            load_results: list[list[BrowserCookie]] = []
            errors: list[BaseException] = []

            def fake_dpapi(value: bytes, *, protect: bool) -> bytes:
                if protect:
                    return b"encrypted-new-vault"
                self.assertEqual(value, b"corrupt-cookie-vault")
                decrypt_started.set()
                allow_decrypt_failure.wait(2)
                raise ValueError("corrupt")

            def load_corrupt_vault() -> None:
                try:
                    load_results.append(reader.load("download"))
                except BaseException as exc:
                    errors.append(exc)

            def save_new_vault() -> None:
                try:
                    writer.save(
                        "download",
                        [BrowserCookie("sid", "new-value", ".example.com")],
                    )
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    writer_finished.set()

            with patch("app.core.browser_cookies._dpapi", side_effect=fake_dpapi):
                load_thread = threading.Thread(target=load_corrupt_vault)
                save_thread = threading.Thread(target=save_new_vault)
                load_thread.start()
                self.assertTrue(decrypt_started.wait(1))
                save_thread.start()
                time.sleep(0.05)
                self.assertFalse(writer_finished.is_set())
                allow_decrypt_failure.set()
                load_thread.join(2)
                save_thread.join(2)

            self.assertFalse(load_thread.is_alive())
            self.assertFalse(save_thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(load_results, [[]])
            self.assertEqual(path.read_bytes(), b"encrypted-new-vault")
            backups = list(root.glob(path.name + ".unreadable-*.bak"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), b"corrupt-cookie-vault")

    def test_first_temp_export_cleans_previous_process_plaintext_only_once(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app_data = Path(directory)
            temp_root = app_data / "temp"
            temp_root.mkdir()
            stale = temp_root / "huifa-cookie-abandoned.txt"
            unrelated = temp_root / "other-file.txt"
            stale.write_text("secret", encoding="utf-8")
            unrelated.write_text("keep", encoding="utf-8")
            vault = CookieVault(app_data / "vault")
            cookies = [BrowserCookie("sid", "value", ".example.com")]

            with patch(
                "app.core.browser_cookies._TEMP_EXPORTS_CLEANED",
                False,
            ), patch(
                "app.core.browser_cookies.data_dir",
                return_value=app_data,
            ), patch.object(vault, "load", return_value=cookies):
                first = vault.create_temporary_netscape_file("download")
                current_process_file = temp_root / "huifa-cookie-current.txt"
                current_process_file.write_text("active", encoding="utf-8")
                second = vault.create_temporary_netscape_file("download")

            try:
                self.assertFalse(stale.exists())
                self.assertTrue(unrelated.exists())
                self.assertTrue(first.exists())
                self.assertTrue(second.exists())
                self.assertTrue(current_process_file.exists())
            finally:
                first.unlink(missing_ok=True)
                second.unlink(missing_ok=True)

    def test_explicit_stale_temp_cleanup_ignores_locked_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp_root = Path(directory)
            removable = temp_root / "huifa-cookie-removable.txt"
            locked = temp_root / "huifa-cookie-locked.txt"
            removable.write_text("secret", encoding="utf-8")
            locked.write_text("secret", encoding="utf-8")
            real_unlink = Path.unlink

            def selective_unlink(path: Path, *args, **kwargs):
                if path == locked:
                    raise PermissionError("locked")
                return real_unlink(path, *args, **kwargs)

            with patch.object(Path, "unlink", selective_unlink):
                removed = cleanup_stale_cookie_exports(temp_root)

            self.assertEqual(removed, 1)
            self.assertFalse(removable.exists())
            self.assertTrue(locked.exists())

    def test_download_login_uses_the_generic_persistent_browser(self) -> None:
        worker = AccountWorker(
            "browser",
            "download",
            "login",
            vault_profile_id="download",
        )
        results = []
        worker.result.connect(lambda *args: results.append(args))
        with patch(
            "app.core.publish_service.open_download_cookie_browser",
            return_value={"success": True, "message": "已持久化保存 3 条 Cookie"},
        ) as open_browser, patch(
            "app.core.publish_service.account_action",
        ) as account_action:
            worker.run()

        self.assertTrue(results[0][3])
        self.assertIn("3", results[0][4])
        open_browser.assert_called_once_with("download", cancel_event=worker._cancel)
        account_action.assert_not_called()


if __name__ == "__main__":
    unittest.main()

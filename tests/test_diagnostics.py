from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
from zipfile import ZipFile

from PySide6.QtCore import QCoreApplication

from app.core.download_service import DownloadWorker
from app.core.log_service import DownloadLogService
from app.core.single_instance import SingleInstance
from app.storage.database import Database


class DownloadDiagnosticsTests(unittest.TestCase):
    def test_single_instance_notifies_primary_process(self) -> None:
        app = QCoreApplication.instance() or QCoreApplication([])
        name = f"huifa-test-{uuid4().hex}"
        primary = SingleInstance(name)
        secondary = SingleInstance(name)
        activations: list[bool] = []
        primary.activation_requested.connect(lambda: activations.append(True))
        try:
            self.assertTrue(primary.acquire())
            self.assertFalse(secondary.acquire())
            for _ in range(20):
                app.processEvents()
                if activations:
                    break
            self.assertEqual(activations, [True])
        finally:
            secondary.close()
            primary.close()

    def test_download_worker_no_longer_hashes_completed_media(self) -> None:
        self.assertFalse(hasattr(DownloadWorker, "_sha256_file"))

    def test_playlist_source_ip_lookup_caches_failed_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "app.db")
            worker = DownloadWorker(
                "ip-cache-test",
                "https://example.com/playlist",
                directory,
                db,
                proxy="http://127.0.0.1:7890",
            )
            worker.logs = DownloadLogService(Path(directory) / "logs")
            with patch(
                "app.core.download_service.detect_public_ip",
                return_value="",
            ) as detect:
                self.assertEqual(worker._detect_source_ip_once(), "")
                self.assertEqual(worker._detect_source_ip_once(), "")
            detect.assert_called_once_with("http://127.0.0.1:7890")
            db.close()

    def test_direct_download_does_not_wait_for_third_party_ip_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "app.db")
            direct = DownloadWorker(
                "direct-ip",
                "https://example.com/video",
                directory,
                db,
            )
            proxied = DownloadWorker(
                "proxy-ip",
                "https://example.com/video",
                directory,
                db,
                proxy="http://127.0.0.1:7890",
            )
            with patch(
                "app.core.download_service.detect_public_ip",
                return_value="203.0.113.10",
            ) as detect:
                self.assertEqual(direct._source_ip_for_media(), "")
                self.assertEqual(
                    proxied._source_ip_for_media(),
                    "203.0.113.10",
                )
            detect.assert_called_once_with("http://127.0.0.1:7890")
            db.close()

    def test_log_round_trip_redacts_query_and_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = DownloadLogService(directory)
            service.write(
                "task-1",
                "error",
                "网络/代理",
                "连接超时",
                url="https://example.com/video?token=private",
                authorization="secret",
            )
            self.assertEqual(len(service.read("task-1")), 1)
            rendered = service.render("task-1")
            self.assertNotIn("private", rendered)
            self.assertNotIn("secret", rendered)
            self.assertIn("example.com/video", rendered)

    def test_log_message_redacts_credentials_headers_and_json_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = DownloadLogService(directory)
            service.write(
                "secrets",
                "error",
                "网络/代理",
                "\n".join((
                    "GET https://user:pass@example.com/video?token=private",
                    "Authorization: Bearer access-value",
                    "Cookie: sid=cookie-value",
                    '{"refresh_token": "refresh-value", "api_key": "key-value"}',
                )),
            )
            rendered = service.render("secrets")
            for secret in (
                "user",
                "pass",
                "private",
                "access-value",
                "cookie-value",
                "refresh-value",
                "key-value",
            ):
                self.assertNotIn(secret, rendered)
            self.assertIn("https://example.com/video", rendered)

    def test_task_log_paths_do_not_collide_after_filename_sanitizing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = DownloadLogService(directory)
            self.assertNotEqual(service.path_for("a/b"), service.path_for("a?b"))
            self.assertNotEqual(service.path_for("Task"), service.path_for("task"))
            self.assertNotEqual(service.path_for("CON"), service.path_for("con"))
            self.assertNotEqual(service.path_for("CON").stem.upper(), "CON")

    def test_log_write_tolerates_non_json_values(self) -> None:
        class BrokenText:
            def __str__(self) -> str:
                raise RuntimeError("cannot stringify")

        with tempfile.TemporaryDirectory() as directory:
            service = DownloadLogService(directory)
            service.write(
                "values",
                "info",
                "任务",
                "记录边界值",
                path=Path(directory) / "video.mp4",
                payload=b"binary",
                choices={"video", "audio"},
                ratio=float("inf"),
                unknown=BrokenText(),
            )
            details = service.read("values")[0]["details"]
            self.assertEqual(details["payload"], "<6 bytes>")
            self.assertEqual(set(details["choices"]), {"video", "audio"})
            self.assertEqual(details["ratio"], "inf")
            self.assertEqual(details["unknown"], "<BrokenText>")

    def test_log_rotation_reads_only_a_bounded_tail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = DownloadLogService(directory)
            service.max_log_bytes = 512
            with patch.object(
                Path,
                "read_bytes",
                side_effect=AssertionError("full read"),
            ):
                for index in range(12):
                    service.write(
                        "rotating",
                        "warning",
                        "任务",
                        f"事件 {index} " + "x" * 80,
                    )
            path = service.path_for("rotating")
            self.assertTrue(path.exists())
            self.assertLess(path.stat().st_size, service.max_log_bytes)
            self.assertGreater(len(service.read("rotating")), 0)

    def test_info_logs_are_buffered_until_read_or_flush(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = DownloadLogService(directory)
            service.write("buffered", "info", "任务", "第一条")
            service.write("buffered", "info", "任务", "第二条")
            self.assertFalse(service.path_for("buffered").exists())
            self.assertEqual(len(service.read("buffered")), 2)
            self.assertTrue(service.path_for("buffered").exists())

    def test_warning_flushes_pending_task_log_batch_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = DownloadLogService(directory)
            service.write("warning", "info", "任务", "开始")
            service.write("warning", "warning", "网络/代理", "重试")
            self.assertTrue(service.path_for("warning").exists())
            self.assertEqual(len(service.read("warning")), 2)

    def test_error_classification(self) -> None:
        for message in (
            "HTTP Error 429: Too Many Requests",
            "HTTP Error 403: Forbidden",
            "HTTP Error 401: Unauthorized",
            "Fresh cookies are needed",
        ):
            with self.subTest(message=message):
                self.assertEqual(
                    DownloadLogService.classify_error(message),
                    "风控/登录",
                )
        self.assertEqual(
            DownloadLogService.classify_error("Proxy connection timed out"),
            "网络/代理",
        )
        self.assertEqual(
            DownloadLogService.classify_error("Requested format is not available"),
            "格式/工具",
        )
        self.assertEqual(
            DownloadLogService.classify_error("用户取消下载"),
            "用户操作",
        )
        for message in (
            "[Errno 28] No space left on device",
            "[WinError 112] There is not enough space on the disk",
            "ENOSPC",
            "Disk quota exceeded",
            "Insufficient storage",
            "下载磁盘空间不足，无法安全开始该任务。",
            "目标磁盘剩余空间已低于安全阈值。",
            "No space left on network drive",
        ):
            with self.subTest(message=message):
                self.assertEqual(
                    DownloadLogService.classify_error(message),
                    "磁盘/存储",
                )
        self.assertEqual(
            DownloadLogService.classify_error("等待磁盘空间时任务已取消。"),
            "用户操作",
        )

    def test_clear_removes_task_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = DownloadLogService(directory)
            service.write("task-2", "info", "任务", "开始")
            service.clear("task-2")
            self.assertEqual(service.read("task-2"), [])

    def test_export_bundle_contains_manifest_and_logs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = DownloadLogService(Path(directory) / "logs")
            service.write(
                "task-3",
                "info",
                "任务",
                "开始",
                token="should-not-export",
            )
            bundle = service.export_bundle(
                Path(directory) / "diagnostics.zip",
                {"proxy": "configured"},
            )
            with ZipFile(bundle) as archive:
                names = set(archive.namelist())
                self.assertIn("manifest.json", names)
                self.assertIn("downloads/task-3.jsonl", names)
                payload = archive.read("downloads/task-3.jsonl").decode("utf-8")
                self.assertNotIn("should-not-export", payload)

    def test_network_retry_recovers_transient_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "app.db")
            worker = DownloadWorker(
                "retry-task",
                "https://example.com",
                directory,
                db,
            )
            worker.logs = DownloadLogService(Path(directory) / "logs")
            calls = {"count": 0}

            def flaky_action():
                calls["count"] += 1
                if calls["count"] < 3:
                    raise RuntimeError("Connection timed out")
                return "ok"

            self.assertEqual(
                worker._run_with_network_retry(flaky_action, "测试请求"),
                "ok",
            )
            self.assertEqual(calls["count"], 3)
            db.close()


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.core.log_service import DownloadLogService


class DownloadDiagnosticsTests(unittest.TestCase):
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
            events = service.read("task-1")
            self.assertEqual(len(events), 1)
            rendered = service.render("task-1")
            self.assertNotIn("private", rendered)
            self.assertNotIn("secret", rendered)
            self.assertIn("example.com/video", rendered)

    def test_error_classification(self) -> None:
        self.assertEqual(DownloadLogService.classify_error("HTTP Error 429: Too Many Requests"), "风控/登录")
        self.assertEqual(DownloadLogService.classify_error("Proxy connection timed out"), "网络/代理")
        self.assertEqual(DownloadLogService.classify_error("Requested format is not available"), "格式/工具")
        self.assertEqual(DownloadLogService.classify_error("用户取消下载"), "用户操作")

    def test_clear_removes_task_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = DownloadLogService(directory)
            service.write("task-2", "info", "任务", "开始")
            self.assertTrue(Path(service.path_for("task-2")).exists())
            service.clear("task-2")
            self.assertEqual(service.read("task-2"), [])


if __name__ == "__main__":
    unittest.main()

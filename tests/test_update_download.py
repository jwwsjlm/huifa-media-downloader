from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.update_download import AssetDownloadWorker
from app.core.update_service import UpdateService


_OFFICIAL_URL = (
    "https://github.com/owner/repository/releases/download/v1/tool.exe"
)


class _Response:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.url = (
            "https://release-assets.githubusercontent.com/path/tool.exe"
        )

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int = 0):
        del chunk_size
        if self.payload:
            yield self.payload


class UpdateDownloadTests(unittest.TestCase):
    def test_empty_success_response_preserves_existing_asset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "tool.exe"
            target.write_bytes(b"working-runtime")
            worker = AssetDownloadWorker(_OFFICIAL_URL, target)
            errors: list[str] = []
            worker.failed.connect(errors.append)

            with patch(
                "app.core.update_download.requests.get",
                return_value=_Response(b""),
            ):
                worker.run()

            self.assertEqual(errors, ["更新资源为空，未替换本地文件"])
            self.assertEqual(target.read_bytes(), b"working-runtime")
            self.assertFalse(target.with_name("tool.exe.part").exists())

    def test_size_mismatch_falls_back_to_next_route(self) -> None:
        expected = b"complete-runtime"
        candidates = [
            {"url": _OFFICIAL_URL, "name": "线路一", "third_party": False},
            {"url": _OFFICIAL_URL, "name": "线路二", "third_party": False},
        ]
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "tool.exe"
            worker = AssetDownloadWorker(
                candidates,
                target,
                expected_size=len(expected),
            )
            finished: list[str] = []
            errors: list[str] = []
            worker.finished.connect(finished.append)
            worker.failed.connect(errors.append)

            with patch(
                "app.core.update_download.requests.get",
                side_effect=[_Response(b"short"), _Response(expected)],
            ):
                worker.run()

            self.assertEqual(errors, [])
            self.assertEqual(finished, [str(target)])
            self.assertEqual(target.read_bytes(), expected)
            self.assertFalse(target.with_name("tool.exe.part").exists())

    def test_all_size_mismatches_preserve_existing_asset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "tool.exe"
            target.write_bytes(b"working-runtime")
            worker = AssetDownloadWorker(
                _OFFICIAL_URL,
                target,
                expected_size=100,
            )
            errors: list[str] = []
            worker.failed.connect(errors.append)

            with patch(
                "app.core.update_download.requests.get",
                return_value=_Response(b"truncated"),
            ):
                worker.run()

            self.assertEqual(len(errors), 1)
            self.assertIn("更新资源大小校验失败", errors[0])
            self.assertEqual(target.read_bytes(), b"working-runtime")
            self.assertFalse(target.with_name("tool.exe.part").exists())

    def test_service_rejects_invalid_release_size_before_starting_thread(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = UpdateService(directory)
            errors: list[str] = []
            service.download_failed.connect(errors.append)

            service.download_asset({
                "name": "tool.exe",
                "browser_download_url": _OFFICIAL_URL,
                "size": 0,
            })

            self.assertEqual(errors, ["更新资源大小无效"])
            self.assertFalse(service.runtime_active("download"))

    def test_service_passes_release_size_to_download_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = UpdateService(directory)
            with patch(
                "app.core.update_service.QThread.start",
                return_value=None,
            ):
                service.download_asset({
                    "name": "tool.exe",
                    "browser_download_url": _OFFICIAL_URL,
                    "size": 12345,
                })

            runtime = service._runtimes.get("download")
            self.assertIsNotNone(runtime)
            assert runtime is not None
            self.assertEqual(runtime.worker.expected_size, 12345)
            service._discard_failed_runtime_start(
                "download",
                runtime.thread,
                runtime.worker,
            )


if __name__ == "__main__":
    unittest.main()

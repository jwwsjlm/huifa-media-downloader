from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.download_thumbnails import DownloadThumbnailManager


class _Response:
    def __init__(self, payload: bytes, *, content_length: int | None = None):
        self.payload = payload
        self.headers = {"Content-Type": "image/jpeg"}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

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


def _manager(root: Path, logs: list[tuple] | None = None) -> DownloadThumbnailManager:
    def log(*args, **kwargs) -> None:
        if logs is not None:
            logs.append((*args, kwargs))

    return DownloadThumbnailManager(
        root,
        cancel_event=threading.Event(),
        log=log,
    )


class DownloadThumbnailManagerTests(unittest.TestCase):
    def test_new_preview_removes_stale_sibling_format(self) -> None:
        payload = b"\xff\xd8\xffcover"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stale = root / "demo.thumb.webp"
            stale.write_bytes(b"old-webp")
            manager = _manager(root)

            with patch(
                "app.core.download_thumbnails.requests.get",
                return_value=_Response(payload, content_length=len(payload)),
            ):
                result = Path(manager.save_preview("https://example.test/cover", "demo"))

            self.assertEqual(result, root / "demo.thumb.jpg")
            self.assertEqual(result.read_bytes(), payload)
            self.assertFalse(stale.exists())

    def test_truncated_response_preserves_existing_preview(self) -> None:
        payload = b"\xff\xd8\xffshort"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            existing = root / "demo.thumb.jpg"
            existing.write_bytes(b"existing-preview")
            logs: list[tuple] = []
            manager = _manager(root, logs)

            with patch(
                "app.core.download_thumbnails.requests.get",
                return_value=_Response(payload, content_length=len(payload) + 5),
            ):
                result = manager.save_preview("https://example.test/cover", "demo")

            self.assertEqual(result, "")
            self.assertEqual(existing.read_bytes(), b"existing-preview")
            self.assertEqual(list(root.glob(".*.thumb.*.tmp")), [])
            self.assertTrue(any("内容不完整" in str(row) for row in logs))

    def test_encoded_response_does_not_compare_decoded_bytes_to_wire_size(self) -> None:
        payload = b"\xff\xd8\xffdecoded-image"
        response = _Response(payload, content_length=5)
        response.headers["Content-Encoding"] = "gzip"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = _manager(root)

            with patch(
                "app.core.download_thumbnails.requests.get",
                return_value=response,
            ):
                result = Path(manager.save_preview("https://example.test/cover", "demo"))

            self.assertEqual(result.read_bytes(), payload)

    def test_logging_failure_cannot_leak_partial_preview(self) -> None:
        payload = b"\xff\xd8\xffshort"

        def broken_log(*_args, **_kwargs) -> None:
            raise RuntimeError("log storage unavailable")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = DownloadThumbnailManager(
                root,
                cancel_event=threading.Event(),
                log=broken_log,
            )
            with patch(
                "app.core.download_thumbnails.requests.get",
                return_value=_Response(payload, content_length=len(payload) + 1),
            ):
                result = manager.save_preview(
                    "https://example.test/cover",
                    "demo",
                )

            self.assertEqual(result, "")
            self.assertEqual(list(root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()

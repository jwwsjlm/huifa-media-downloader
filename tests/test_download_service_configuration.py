from __future__ import annotations

import unittest
from contextlib import contextmanager
from types import SimpleNamespace

from app.core.download_service import DownloadService


class DownloadServiceConfigurationTest(unittest.TestCase):
    def test_runtime_settings_are_normalized_by_the_service(self) -> None:
        service = SimpleNamespace(
            ytdlp_core_mode="auto",
            deno_path="",
            ffprobe_path="",
            ytdlp_ejs_source="auto",
            cover_convert_jpeg=False,
            cover_jpeg_quality=90,
        )

        DownloadService.configure_runtime_settings(
            service,
            ytdlp_core_mode="invalid",
            deno_path=" deno.exe ",
            ffprobe_path=" ffprobe.exe ",
            ytdlp_ejs_source="invalid",
            cover_convert_jpeg=True,
            cover_jpeg_quality=999,
        )

        self.assertEqual(service.ytdlp_core_mode, "auto")
        self.assertEqual(service.deno_path, "deno.exe")
        self.assertEqual(service.ffprobe_path, "ffprobe.exe")
        self.assertEqual(service.ytdlp_ejs_source, "auto")
        self.assertTrue(service.cover_convert_jpeg)
        self.assertEqual(service.cover_jpeg_quality, 100)


class DownloadServiceDeletionTest(unittest.TestCase):
    def test_missing_active_row_is_cancelled_through_the_service(self) -> None:
        updates: list[object] = []
        cancellations: list[str] = []

        @contextmanager
        def missing_row_mutation(_task: object):
            yield
            raise LookupError("task row is gone")

        task = SimpleNamespace(
            id="task",
            task_kind="video",
            status="downloading",
            stage="downloading",
            stage_text="正在下载",
            pause_requested=False,
            cancel_requested=False,
        )
        service = SimpleNamespace(
            tasks={"task": task},
            workers={"task": SimpleNamespace(cancel=cancellations.append)},
            conversion_workers={},
            _durable_task_mutation=missing_row_mutation,
            _mark_task_canceling=DownloadService._mark_task_canceling,
            _sync_task_indexes=lambda _task: None,
            _best_effort_service_log=lambda *_args, **_kwargs: None,
            _pending_deletes={},
            task_updated=SimpleNamespace(emit=updates.append),
        )

        self.assertTrue(DownloadService.delete_task(service, "task"))
        self.assertEqual(cancellations, ["delete"])
        self.assertEqual(service._pending_deletes, {"task": False})
        self.assertEqual(task.status, "canceling")
        self.assertTrue(task.cancel_requested)
        self.assertEqual(updates, [task])


if __name__ == "__main__":
    unittest.main()

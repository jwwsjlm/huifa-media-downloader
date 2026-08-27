from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication

from app.core.download_service import DownloadService, DownloadTask
from app.storage.database import Database


class DownloadServiceLogResilienceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db = Database(self.root / "app.db")
        self.service = DownloadService(self.db)

    def tearDown(self) -> None:
        self.service.workers.clear()
        self.service.threads.clear()
        self.service.shutdown(timeout_ms=0)
        self.db.close()
        self.temp_dir.cleanup()

    def _add_task(self, task: DownloadTask) -> DownloadTask:
        self.db.upsert_download_task(task)
        self.service._register_task(task)
        return task

    def test_queued_cancel_commits_when_audit_log_is_unavailable(self) -> None:
        task = self._add_task(DownloadTask(
            "log-failure-cancel",
            "https://example.test/log-failure-cancel",
            str(self.root),
            status="queued",
        ))
        self.service.queue.append(task.id)

        with patch.object(
            self.service.logs,
            "write",
            side_effect=PermissionError("log locked"),
        ):
            self.service.cancel(task.id)

        self.assertEqual(task.status, "canceled")
        self.assertEqual(list(self.service.queue), [])
        self.assertEqual(str(self.db.list_download_tasks()[0]["status"]), "canceled")

    def test_resume_and_retry_still_schedule_work_when_logging_fails(self) -> None:
        paused = self._add_task(DownloadTask(
            "log-failure-resume",
            "https://example.test/log-failure-resume",
            str(self.root),
            status="paused",
        ))
        failed = self._add_task(DownloadTask(
            "log-failure-retry",
            "https://example.test/log-failure-retry",
            str(self.root),
            status="failed",
            error="network failed",
        ))

        with patch.object(
            self.service.logs,
            "write",
            side_effect=PermissionError("log locked"),
        ), patch.object(self.service, "_start_next") as start_next:
            self.service.resume(paused.id)
            self.service.retry(failed.id)

        self.assertEqual(paused.status, "queued")
        self.assertEqual(failed.status, "queued")
        self.assertEqual(set(self.service.queue), {paused.id, failed.id})
        self.assertEqual(start_next.call_count, 2)

    def test_format_selection_reaches_worker_when_logging_fails(self) -> None:
        task = self._add_task(DownloadTask(
            "log-failure-format",
            "https://example.test/log-failure-format",
            str(self.root),
            status="waiting_selection",
            stage="waiting_selection",
        ))
        selections: list[tuple[str, str, str]] = []
        self.service.workers[task.id] = SimpleNamespace(
            set_format_selector=(
                lambda selector, *, content_mode, audio_format:
                selections.append((selector, content_mode, audio_format))
            ),
        )

        with patch.object(
            self.service.logs,
            "write",
            side_effect=PermissionError("log locked"),
        ):
            accepted = self.service.set_format_selection(task.id, {
                "selector": "137+140",
                "content_mode": "video",
                "audio_format": "m4a",
            })

        self.assertTrue(accepted)
        self.assertEqual(selections, [("137+140", "video", "m4a")])
        self.assertEqual(task.status, "downloading")
        self.assertEqual(task.format_selector, "137+140")
        self.assertEqual(
            str(self.db.list_download_tasks()[0]["format_selector"]),
            "137+140",
        )

    def test_completed_delete_returns_success_when_logging_fails(self) -> None:
        task = self._add_task(DownloadTask(
            "log-failure-delete",
            "https://example.test/log-failure-delete",
            str(self.root),
            status="completed",
            progress=100.0,
        ))
        deleted: list[str] = []
        self.service.task_deleted.connect(deleted.append)

        with patch.object(
            self.service.logs,
            "write",
            side_effect=PermissionError("log locked"),
        ):
            result = self.service.delete_task(task.id, delete_files=False)

        self.assertTrue(result)
        self.assertEqual(deleted, [task.id])
        self.assertNotIn(task.id, self.service.tasks)
        self.assertEqual(self.db.list_download_tasks(), [])

    def test_running_collection_delete_reaches_every_worker_when_logging_fails(self) -> None:
        parent = DownloadTask(
            "log-failure-collection",
            "https://example.test/log-failure-collection",
            str(self.root),
            task_kind="collection",
            status="downloading",
        )
        child = DownloadTask(
            "log-failure-collection-child",
            "https://example.test/log-failure-collection/child",
            str(self.root),
            parent_task_id=parent.id,
            root_task_id=parent.id,
            status="downloading",
        )
        self.db.upsert_download_tasks((parent, child))
        self.service._register_task(parent)
        self.service._register_task(child)
        cancel_reasons: list[str] = []
        self.service.workers[child.id] = SimpleNamespace(
            cancel=cancel_reasons.append,
        )

        with patch.object(
            self.service.logs,
            "write",
            side_effect=PermissionError("log locked"),
        ):
            result = self.service.delete_task(parent.id, delete_files=False)

        self.assertTrue(result)
        self.assertEqual(cancel_reasons, ["delete"])
        self.assertIn(parent.id, self.service._pending_collection_deletes)
        self.assertEqual(parent.status, "canceling")
        self.assertEqual(child.status, "canceling")

    def test_duplicate_coalescing_is_not_blocked_by_logging_failure(self) -> None:
        canonical = self._add_task(DownloadTask(
            "log-failure-canonical",
            "https://example.test/shared-video",
            str(self.root),
            status="queued",
            source_key="example:shared-video",
            title="Shared video",
        ))
        duplicate = self._add_task(DownloadTask(
            "log-failure-duplicate",
            "https://example.test/shared-video",
            str(self.root),
            status="queued",
            source_key="example:shared-video",
            title="Shared video",
        ))

        with patch.object(
            self.service.logs,
            "write",
            side_effect=PermissionError("log locked"),
        ):
            result = self.service._coalesce_resolved_duplicates(duplicate)

        self.assertEqual(result, canonical.id)
        self.assertIn(canonical.id, self.service.tasks)
        self.assertNotIn(duplicate.id, self.service.tasks)
        self.assertEqual(
            [str(row["id"]) for row in self.db.list_download_tasks()],
            [canonical.id],
        )


if __name__ == "__main__":
    unittest.main()

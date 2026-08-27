from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.core.download_queue import DownloadTaskQueue


class DownloadTaskQueueTests(unittest.TestCase):
    def test_unique_operations_preserve_fifo_order(self) -> None:
        queue = DownloadTaskQueue(("one", "two"))

        self.assertFalse(queue.append_unique("one"))
        self.assertTrue(queue.append_unique("three"))
        self.assertEqual(queue.extend_unique(("two", "four", "three", "five")), 2)

        self.assertEqual(list(queue), ["one", "two", "three", "four", "five"])

    def test_requeue_front_removes_every_duplicate(self) -> None:
        queue = DownloadTaskQueue(("task", "other", "task", "last"))

        queue.requeue_front("task")

        self.assertEqual(list(queue), ["task", "other", "last"])

    def test_requeue_back_rotates_one_blocked_task_behind_its_peers(self) -> None:
        queue = DownloadTaskQueue(("task", "other", "task", "last"))

        queue.requeue_back("task")

        self.assertEqual(list(queue), ["other", "last", "task"])

    def test_remove_all_mutates_existing_queue_object(self) -> None:
        queue = DownloadTaskQueue(("one", "two", "three", "two"))
        same_queue = queue

        removed = queue.remove_all(("two", "missing"))

        self.assertIs(queue, same_queue)
        self.assertEqual(removed, 2)
        self.assertEqual(list(queue), ["one", "three"])

    def test_take_next_discards_missing_and_non_runnable_entries(self) -> None:
        tasks = {
            "paused": SimpleNamespace(status="paused"),
            "ready": SimpleNamespace(status="queued"),
        }
        queue = DownloadTaskQueue(("missing", "paused", "ready"))

        task = queue.take_next(
            tasks.get,
            lambda candidate: candidate.status == "queued",
        )

        self.assertIs(task, tasks["ready"])
        self.assertFalse(queue)


if __name__ == "__main__":
    unittest.main()

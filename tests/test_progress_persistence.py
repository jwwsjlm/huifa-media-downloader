from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.core.progress_persistence import ProgressPersistenceBuffer


class ProgressPersistenceBufferTests(unittest.TestCase):
    def test_mark_persisted_does_not_remove_newer_task_instance(self) -> None:
        buffer = ProgressPersistenceBuffer()
        old = SimpleNamespace(id="task")
        newer = SimpleNamespace(id="task")
        buffer.enqueue(old)
        batch = buffer.batch()
        buffer.enqueue(newer)

        buffer.mark_persisted(batch, 12.0)

        self.assertIs(buffer.pending["task"], newer)
        self.assertEqual(buffer.persisted_at["task"], 12.0)

    def test_forget_removes_pending_and_persisted_state(self) -> None:
        buffer = ProgressPersistenceBuffer()
        task = SimpleNamespace(id="task")
        buffer.enqueue(task)
        buffer.persisted_at["task"] = 1.0

        buffer.forget("task")

        self.assertEqual(buffer.pending, {})
        self.assertEqual(buffer.persisted_at, {})

    def test_first_error_is_reported_then_rate_limited(self) -> None:
        buffer = ProgressPersistenceBuffer()

        self.assertTrue(buffer.should_report_error(5.0, interval=30.0))
        self.assertFalse(buffer.should_report_error(10.0, interval=30.0))
        self.assertTrue(buffer.should_report_error(36.0, interval=30.0))

    def test_pending_retry_does_not_repeat_immediate_writes(self) -> None:
        buffer = ProgressPersistenceBuffer()
        task = SimpleNamespace(id="task")

        self.assertTrue(buffer.should_write_immediately(task.id, force=False))
        buffer.enqueue(task)

        self.assertFalse(buffer.should_write_immediately(task.id, force=False))
        self.assertTrue(buffer.should_write_immediately(task.id, force=True))

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QStatusBar, QWidget

from app.ui.task_status_summary_controller import (
    TaskStatusSummaryController,
    format_transfer_speed,
)


class _Service:
    def __init__(self) -> None:
        self.stats = {
            "total": 8,
            "active": 2,
            "processing": 1,
            "queued": 3,
            "paused": 1,
            "completed": 1,
            "failed": 0,
        }
        self.speed = 3 * 1024 * 1024
        self.calls = 0
        self.error: Exception | None = None

    def task_statistics(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.stats

    def total_speed_bps(self):
        if self.error is not None:
            raise self.error
        return self.speed


class TaskStatusSummaryControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        existing = QCoreApplication.instance()
        if existing is not None and not isinstance(existing, QApplication):
            raise unittest.SkipTest("a non-GUI QCoreApplication already exists")
        cls.app = existing or QApplication([])

    def setUp(self) -> None:
        self.parent = QWidget()
        self.status_bar = QStatusBar(self.parent)
        self.service = _Service()
        self.controller = TaskStatusSummaryController(
            self.parent,
            self.status_bar,
            self.service,
        )

    def tearDown(self) -> None:
        self.controller.stop()
        self.parent.close()
        self.parent.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()

    def test_speed_formatter_handles_units_and_invalid_values(self) -> None:
        self.assertEqual(format_transfer_speed(0), "0 B/s")
        self.assertEqual(format_transfer_speed(1536), "1.50 KiB/s")
        self.assertEqual(format_transfer_speed(2 * 1024 * 1024), "2.00 MiB/s")
        for value in (-1, float("nan"), float("inf"), "invalid", None):
            with self.subTest(value=value):
                self.assertEqual(format_transfer_speed(value), "0 B/s")

    def test_start_refreshes_summary_and_owns_periodic_timer(self) -> None:
        self.controller.start()

        self.assertTrue(self.controller.running)
        self.assertIn("8", self.controller.label.text())
        self.assertIn("3.00 MiB/s", self.controller.label.text())
        self.assertEqual(self.controller.label.objectName(), "taskSummaryStatus")

    def test_same_event_loop_refresh_requests_are_coalesced(self) -> None:
        self.controller.schedule_refresh()
        self.controller.schedule_refresh()
        self.controller.schedule_refresh()
        self.assertEqual(self.service.calls, 0)

        self.app.processEvents()

        self.assertEqual(self.service.calls, 1)

    def test_transient_service_failure_keeps_last_good_text_and_timer_alive(self) -> None:
        self.controller.start()
        previous = self.controller.label.text()
        self.service.error = RuntimeError("statistics temporarily unavailable")

        self.controller.refresh()

        self.assertEqual(self.controller.label.text(), previous)
        self.assertTrue(self.controller.running)

    def test_malformed_statistics_are_bounded_instead_of_escaping_timer(self) -> None:
        self.service.stats = {
            "total": "bad",
            "active": -5,
            "processing": object(),
            "queued": None,
            "paused": -1,
            "completed": 222,
            "failed": "333",
        }

        self.controller.refresh()

        text = self.controller.label.text()
        self.assertIn("222", text)
        self.assertIn("333", text)

    def test_stop_cancels_periodic_and_pending_refreshes(self) -> None:
        self.controller.start()
        calls = self.service.calls
        self.controller.schedule_refresh()

        self.controller.stop()
        self.app.processEvents()
        self.controller.refresh()

        self.assertFalse(self.controller.running)
        self.assertEqual(self.service.calls, calls)


if __name__ == "__main__":
    unittest.main()

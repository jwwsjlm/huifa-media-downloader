from __future__ import annotations

import unittest
from unittest.mock import patch

from PySide6.QtCore import QThread

from app.core.collection_service import CollectionProbeRequest
from app.ui.collection_probe_coordinator import CollectionProbeCoordinator


class CollectionProbeCoordinatorTests(unittest.TestCase):
    def _coordinator(self) -> CollectionProbeCoordinator:
        return CollectionProbeCoordinator(
            on_metadata=lambda *_args: None,
            on_entries=lambda *_args: None,
            on_single=lambda *_args: None,
            on_failed=lambda *_args: None,
            on_finished=lambda *_args: None,
            on_start_error=lambda *_args: None,
            on_slot_released=lambda: None,
        )

    @staticmethod
    def _state(request_id: str) -> dict[str, object]:
        return {
            "request": CollectionProbeRequest(
                request_id,
                "https://example.com/playlist",
            ),
            "thread": None,
            "worker": None,
            "confirmed": False,
        }

    def test_shutdown_clears_pending_queue_and_prevents_new_threads(self) -> None:
        coordinator = self._coordinator()
        self.assertTrue(coordinator.enqueue("queued", self._state("queued")))

        coordinator.request_shutdown()
        with patch("app.ui.collection_probe_coordinator.QThread") as thread_type:
            coordinator.start_pending()

        self.assertEqual(list(coordinator.queue), [])
        self.assertNotIn("queued", coordinator.states)
        self.assertTrue(coordinator.shutdown_requested)
        thread_type.assert_not_called()
        self.assertFalse(
            coordinator.enqueue("late", self._state("late")),
        )

    def test_cancel_removes_a_queued_probe_before_it_can_start(self) -> None:
        coordinator = self._coordinator()
        self.assertTrue(coordinator.enqueue("queued", self._state("queued")))

        self.assertTrue(coordinator.cancel("queued"))
        with patch("app.ui.collection_probe_coordinator.QThread") as thread_type:
            coordinator.start_pending()

        self.assertEqual(list(coordinator.queue), [])
        self.assertNotIn("queued", coordinator.states)
        thread_type.assert_not_called()

    def test_duplicate_request_id_does_not_overwrite_original_state(self) -> None:
        coordinator = self._coordinator()
        original = self._state("duplicate")
        replacement = self._state("duplicate")
        replacement["url"] = "https://example.com/replacement"

        self.assertTrue(coordinator.enqueue("duplicate", original))
        self.assertFalse(coordinator.enqueue("duplicate", replacement))

        self.assertIs(coordinator.states["duplicate"], original)
        self.assertEqual(list(coordinator.queue), ["duplicate"])

    def test_confirmed_or_shutdown_state_rejects_late_results(self) -> None:
        coordinator = self._coordinator()
        state = self._state("late")
        self.assertTrue(coordinator.enqueue("late", state))
        self.assertIs(coordinator.result_state("late"), state)

        state["confirmed"] = True
        self.assertIsNone(coordinator.result_state("late"))

        state["confirmed"] = False
        coordinator.request_shutdown()
        self.assertIsNone(coordinator.result_state("late"))

    def test_running_ignores_invalid_thread_but_keeps_deferred_finish_owned(self) -> None:
        coordinator = self._coordinator()
        thread = QThread()
        state = self._state("finished")
        state["thread"] = thread
        coordinator.states["finished"] = state

        with patch.object(
            QThread,
            "isRunning",
            side_effect=RuntimeError("wrapped C++ object deleted"),
        ):
            self.assertFalse(coordinator.thread_is_running(thread))
            self.assertFalse(coordinator.running)
            coordinator.deferred_finishes.add("finished")
            self.assertTrue(coordinator.running)

        thread.deleteLater()


if __name__ == "__main__":
    unittest.main()

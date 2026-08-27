from __future__ import annotations

import unittest

from app.core.download_runtime_state import (
    download_runtime_signal_is_current,
    finished_download_state,
)


class DownloadRuntimeStateTests(unittest.TestCase):
    def test_completed_durable_state_wins_over_shutdown_pause_request(self) -> None:
        state = finished_download_state(
            status="completed",
            error="",
            pause_requested=True,
            cancel_requested=False,
        )

        self.assertEqual(state.status, "completed")
        self.assertEqual(state.progress, 100.0)

    def test_non_completed_terminal_states_clear_transient_progress(self) -> None:
        state = finished_download_state(
            status="downloading",
            error="",
            pause_requested=True,
            cancel_requested=False,
        )

        self.assertEqual(state.status, "paused")
        self.assertEqual(state.stage_progress, 0.0)
        self.assertEqual(state.reconnect_message, "")

    def test_old_runtime_signal_is_rejected_after_replacement(self) -> None:
        old_worker = object()
        replacement = object()

        self.assertFalse(download_runtime_signal_is_current(old_worker, replacement))
        self.assertTrue(download_runtime_signal_is_current(replacement, replacement))
        self.assertTrue(download_runtime_signal_is_current(None, replacement))
        self.assertTrue(download_runtime_signal_is_current(
            old_worker,
            None,
            allow_finished_runtime=True,
        ))


if __name__ == "__main__":
    unittest.main()

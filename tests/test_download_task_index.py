from __future__ import annotations

import unittest

from app.core.download_task_index import DownloadTaskIndex


class DownloadTaskIndexTests(unittest.TestCase):
    def test_sync_tracks_top_level_and_child_statuses_separately(self) -> None:
        index = DownloadTaskIndex()
        index.sync(
            "parent",
            parent_id="",
            status="downloading",
            speed_bps=0,
        )
        index.sync(
            "child",
            parent_id="parent",
            status="queued",
            speed_bps=0,
        )

        self.assertEqual(index.statistics()["total"], 2)
        self.assertEqual(index.statistics()["active"], 1)
        self.assertEqual(index.statistics()["queued"], 1)
        self.assertEqual(index.statistics(top_level_only=True)["total"], 1)
        self.assertEqual(index.child_ids("parent"), {"child"})

    def test_sync_moves_task_between_parents_without_leaving_stale_children(self) -> None:
        index = DownloadTaskIndex()
        index.sync("task", parent_id="first", status="queued", speed_bps=0)
        index.sync("task", parent_id="second", status="paused", speed_bps=0)

        self.assertFalse(index.child_ids("first"))
        self.assertEqual(index.child_ids("second"), {"task"})
        self.assertEqual(index.statistics()["queued"], 0)
        self.assertEqual(index.statistics()["paused"], 1)

        index.sync("task", parent_id="", status="completed", speed_bps=0)
        self.assertFalse(index.child_ids("second"))
        self.assertEqual(index.statistics(top_level_only=True)["completed"], 1)

    def test_speed_is_updated_by_delta_and_invalid_values_are_ignored(self) -> None:
        index = DownloadTaskIndex()
        index.sync("one", parent_id="", status="downloading", speed_bps=100)
        index.sync("two", parent_id="", status="downloading", speed_bps=200)
        self.assertEqual(index.total_speed_bps, 300)

        index.sync("one", parent_id="", status="downloading", speed_bps=150)
        self.assertEqual(index.total_speed_bps, 350)

        index.sync("two", parent_id="", status="completed", speed_bps=float("nan"))
        self.assertEqual(index.total_speed_bps, 150)

    def test_corrupted_total_speed_is_rebuilt_before_remove(self) -> None:
        index = DownloadTaskIndex()
        index.sync("one", parent_id="", status="downloading", speed_bps=100)
        index.sync("two", parent_id="", status="downloading", speed_bps=200)
        index._total_speed_bps = float("nan")

        removed = index.remove("one")

        self.assertIsNotNone(removed)
        self.assertEqual(index.total_speed_bps, 200)

    def test_remove_is_idempotent_and_cleans_parent_bucket(self) -> None:
        index = DownloadTaskIndex()
        index.sync("child", parent_id="parent", status="failed", speed_bps=0)

        self.assertIsNotNone(index.remove("child"))
        self.assertIsNone(index.remove("child"))
        self.assertFalse(index.states)
        self.assertFalse(index.child_ids("parent"))
        self.assertEqual(index.statistics()["total"], 0)

    def test_child_ids_is_a_stable_snapshot(self) -> None:
        index = DownloadTaskIndex()
        index.sync("first", parent_id="parent", status="queued", speed_bps=0)

        snapshot = index.child_ids("parent")
        index.sync("second", parent_id="parent", status="queued", speed_bps=0)

        self.assertEqual(snapshot, {"first"})
        self.assertEqual(index.child_ids("parent"), {"first", "second"})

if __name__ == "__main__":
    unittest.main()

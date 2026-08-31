from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from app.core.download_service import DownloadTask
from app.ui.task_list import (
    TaskListPagingState,
    ordered_top_level_tasks,
    task_matches_filter,
)
from app.ui.task_list_restore import enrich_completed_task_metadata


def task(
    task_id: str,
    *,
    title: str = "",
    url: str = "",
    status: str = "queued",
    created_at: str = "",
    parent_task_id: str = "",
):
    return DownloadTask(
        task_id,
        url,
        "D:/downloads",
        title=title,
        status=status,
        created_at=created_at,
        parent_task_id=parent_task_id,
    )


class TaskListRulesTests(unittest.TestCase):
    def test_filter_searches_task_identity_and_respects_status(self) -> None:
        item = task("task-a")

        self.assertTrue(task_matches_filter(item, "全部", "task-a"))
        self.assertFalse(task_matches_filter(item, "全部", "none"))
        self.assertFalse(task_matches_filter(item, "已完成", ""))

    def test_ordering_excludes_children_and_breaks_equal_keys_by_task_id(self) -> None:
        tasks = [
            task("b", title="Same"),
            task("child", parent_task_id="b", created_at="9999"),
            task("a", title="Same"),
        ]

        self.assertEqual(
            [item.id for item in ordered_top_level_tasks(tasks, "title")],
            ["a", "b"],
        )

    def test_status_sort_keeps_active_work_before_terminal_history(self) -> None:
        tasks = [
            task("deleted", status="deleted"),
            task("completed", status="completed"),
            task("processing", status="processing"),
            task("downloading", status="downloading"),
        ]

        ordered = [item.id for item in ordered_top_level_tasks(tasks, "status")]

        terminal_start = min(ordered.index("completed"), ordered.index("deleted"))
        self.assertLess(ordered.index("processing"), terminal_start)
        self.assertLess(ordered.index("downloading"), terminal_start)

    def test_paging_state_keeps_one_canonical_order_and_pending_queue(self) -> None:
        state = TaskListPagingState()
        state.set_ordered(("c", "b", "a"), {"b"})

        self.assertEqual(state.ordered_ids, ["c", "b", "a"])
        self.assertEqual(list(state.pending_ids), ["c", "a"])
        self.assertFalse(state.append_pending)

        matching = state.prioritize(lambda task_id: task_id == "a")
        self.assertEqual(matching, ["a"])
        self.assertEqual(list(state.pending_ids), ["a", "c"])

        state.remove("a")
        self.assertEqual(state.ordered_ids, ["c", "b"])
        self.assertEqual(list(state.pending_ids), ["c"])

    def test_paging_state_places_late_materialized_rows_in_canonical_order(self) -> None:
        state = TaskListPagingState()
        state.set_ordered(("a", "b", "c", "d"), {"a", "d"})

        self.assertEqual(state.materialized_row("b", {"a", "d"}), 1)
        self.assertEqual(state.materialized_row("c", {"a", "b", "d"}), 2)
        self.assertEqual(state.materialized_row("missing", {"a"}), -1)

    def test_paging_state_bounds_restore_and_load_more_goals(self) -> None:
        state = TaskListPagingState()
        state.begin_restore((str(index) for index in range(75)), 50)

        self.assertTrue(state.loading)
        self.assertTrue(state.append_pending)
        self.assertEqual(state.render_goal, 50)
        state.finish()
        self.assertTrue(state.begin_more(50, 25, 50))
        self.assertEqual(state.render_goal, 75)
        self.assertTrue(state.loading)

        state.clear()
        self.assertFalse(state.loading)
        self.assertFalse(state.append_pending)
        self.assertEqual(state.render_goal, 0)
        self.assertFalse(state.begin_more(0, 0, 50))

    def test_restore_metadata_uses_bounded_batch_lookup_not_full_catalog(self) -> None:
        source_url = "https://example.com/video"
        tasks = [
            SimpleNamespace(
                status="completed",
                url=source_url,
                media_path="",
                thumbnail_path="",
                uploader="",
                downloaded_at="",
            )
            for _index in range(2)
        ]
        media = SimpleNamespace(
            source_url=source_url,
            video_path="D:/downloads/video.mp4",
            thumbnail_path="D:/downloads/video.jpg",
            uploader="Uploader",
            downloaded_at="2026-08-26T10:00:00",
        )
        batch_lookup = Mock(return_value={source_url: media})
        list_media = Mock(side_effect=AssertionError("full catalog read"))
        database = SimpleNamespace(
            latest_media_by_source_urls=batch_lookup,
            list_media=list_media,
        )

        self.assertTrue(enrich_completed_task_metadata(tasks, database))

        batch_lookup.assert_called_once_with([source_url])
        list_media.assert_not_called()
        self.assertTrue(all(task.uploader == "Uploader" for task in tasks))
        self.assertTrue(all(task.media_path == media.video_path for task in tasks))

    def test_restore_metadata_failure_never_blocks_task_history(self) -> None:
        task = SimpleNamespace(
            status="completed",
            url="https://example.com/video",
            media_path="",
            thumbnail_path="",
            uploader="",
            downloaded_at="",
        )
        database = SimpleNamespace(
            latest_media_by_source_urls=Mock(side_effect=RuntimeError("busy")),
            list_media=Mock(side_effect=AssertionError("must not expand fallback")),
        )

        self.assertFalse(enrich_completed_task_metadata([task], database))
        database.list_media.assert_not_called()

if __name__ == "__main__":
    unittest.main()

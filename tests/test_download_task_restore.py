from __future__ import annotations

import unittest

from app.core.download_task_restore import (
    _resolve_hierarchy_records,
    build_task_restore_plan,
)


class _CountingRecords(dict[str, tuple[str, str]]):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.lookups = 0

    def get(self, key, default=None):
        self.lookups += 1
        return super().get(key, default)

    def __getitem__(self, key):
        self.lookups += 1
        return super().__getitem__(key)


class DownloadTaskRestoreTests(unittest.TestCase):
    def test_deep_hierarchy_uses_linear_path_compression(self) -> None:
        count = 5000
        records = _CountingRecords({
            f"task-{index}": (
                "collection",
                "" if index == 0 else f"task-{index - 1}",
            )
            for index in range(count)
        })

        hierarchy = _resolve_hierarchy_records(records)

        self.assertEqual(len(hierarchy), count)
        self.assertEqual(hierarchy[f"task-{count - 1}"].root_task_id, "task-0")
        self.assertLess(records.lookups, count * 6)

    def test_restore_plan_defers_only_terminal_children_over_budget(self) -> None:
        rows = [{
            "id": "parent",
            "task_kind": "collection",
            "parent_task_id": "",
            "status": "completed",
            "media_path": "",
            "url": "https://example.test/list",
        }]
        rows.extend({
            "id": f"child-{index}",
            "task_kind": "video",
            "parent_task_id": "parent",
            "status": "completed",
            "media_path": f"video-{index}.mp4",
            "url": f"https://example.test/video/{index}",
        } for index in range(5))

        plan = build_task_restore_plan(rows, initial_terminal_children=2)

        self.assertEqual(len(plan.immediate_rows), 3)
        self.assertEqual(len(plan.deferred_rows), 3)
        self.assertEqual(plan.deferred_parent_ids, {"parent"})

    def test_invalid_ancestor_is_propagated_to_descendants(self) -> None:
        records = {
            "child": ("video", "nested"),
            "nested": ("collection", "missing"),
        }

        hierarchy = _resolve_hierarchy_records(records)

        self.assertEqual(hierarchy["nested"].invalid_reason, "父合集记录不存在")
        self.assertEqual(hierarchy["child"].invalid_reason, "父合集记录不存在")
        self.assertEqual(hierarchy["child"].parent_task_id, "")


if __name__ == "__main__":
    unittest.main()

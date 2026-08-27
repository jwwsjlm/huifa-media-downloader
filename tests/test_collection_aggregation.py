from __future__ import annotations

import unittest

from app.core.collection_aggregation import (
    CollectionAggregate,
    collection_child_contribution,
    summarize_collection,
)


class CollectionAggregationTests(unittest.TestCase):
    def test_contribution_sanitizes_corrupted_runtime_numbers(self) -> None:
        contribution = collection_child_contribution(
            parent_id="parent",
            status="downloading",
            speed_bps=float("nan"),
            total_bytes="invalid",
            downloaded_bytes=float("inf"),
            progress=250,
        )

        self.assertIsNotNone(contribution)
        assert contribution is not None
        self.assertEqual(contribution.speed_bps, 0.0)
        self.assertEqual(contribution.known_size, 0)
        self.assertEqual(contribution.downloaded_bytes, 0)
        self.assertEqual(contribution.unknown_fraction, 1.0)

    def test_add_then_remove_cleans_counter_and_float_residue(self) -> None:
        contribution = collection_child_contribution(
            parent_id="parent",
            status="downloading",
            speed_bps=0.1,
            total_bytes=300,
            downloaded_bytes=100,
            progress=100 / 3,
        )
        assert contribution is not None
        aggregate = CollectionAggregate()

        aggregate.apply(contribution, 1)
        aggregate.apply(contribution, -1)

        self.assertEqual(aggregate.statuses, {})
        self.assertEqual(aggregate.child_count, 0)
        self.assertEqual(aggregate.speed_bps, 0.0)
        self.assertEqual(aggregate.known_done, 0.0)

    def test_known_children_are_weighted_by_bytes(self) -> None:
        aggregate = CollectionAggregate()
        small = collection_child_contribution(
            parent_id="parent",
            status="completed",
            speed_bps=0,
            total_bytes=10,
            downloaded_bytes=10,
            progress=100,
        )
        large = collection_child_contribution(
            parent_id="parent",
            status="downloading",
            speed_bps=10,
            total_bytes=100,
            downloaded_bytes=0,
            progress=0,
        )
        assert small is not None and large is not None
        aggregate.apply(small, 1)
        aggregate.apply(large, 1)

        summary = summarize_collection(aggregate, parsed_count=3)

        self.assertEqual(summary.status, "downloading")
        self.assertAlmostEqual(summary.progress, 100 / 11, places=2)
        self.assertEqual(summary.downloaded_bytes, 10)
        self.assertEqual(summary.total_bytes, 110)
        self.assertEqual(summary.metadata["skipped"], 1)

    def test_unknown_child_hides_combined_total_and_eta(self) -> None:
        aggregate = CollectionAggregate()
        known = collection_child_contribution(
            parent_id="parent",
            status="downloading",
            speed_bps=10,
            total_bytes=100,
            downloaded_bytes=50,
            progress=50,
        )
        unknown = collection_child_contribution(
            parent_id="parent",
            status="downloading",
            speed_bps=20,
            total_bytes=0,
            downloaded_bytes=500,
            progress=25,
        )
        assert known is not None and unknown is not None
        aggregate.apply(known, 1)
        aggregate.apply(unknown, 1)

        summary = summarize_collection(aggregate, parsed_count=2)

        self.assertEqual(summary.speed_bps, 30)
        self.assertEqual(summary.downloaded_bytes, 550)
        self.assertEqual(summary.total_bytes, 0)
        self.assertEqual(summary.eta, "")

    def test_non_finite_aggregate_values_do_not_escape_to_parent_summary(self) -> None:
        aggregate = CollectionAggregate(
            child_count=1,
            speed_bps=float("inf"),
            known_count=1,
            known_total=100,
            known_done=float("nan"),
            downloaded_bytes=50,
            total_bytes=100,
        )
        aggregate.statuses["downloading"] = 1

        summary = summarize_collection(aggregate, parsed_count="invalid")

        self.assertEqual(summary.status, "downloading")
        self.assertEqual(summary.progress, 0.0)
        self.assertEqual(summary.speed_bps, 0.0)
        self.assertEqual(summary.total_bytes, 100)
        self.assertEqual(summary.eta, "")
        self.assertEqual(summary.metadata["skipped"], 0)

    def test_empty_aggregate_is_not_reported_as_completed(self) -> None:
        summary = summarize_collection(CollectionAggregate(), parsed_count=0)

        self.assertEqual(summary.status, "queued")
        self.assertFalse(summary.terminal)


if __name__ == "__main__":
    unittest.main()

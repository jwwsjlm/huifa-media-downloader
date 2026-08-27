from __future__ import annotations

import unittest

from app.core.download_progress import (
    StageProgressState,
    TransferCounterState,
    format_eta,
    format_speed,
    merge_stage_progress,
    merge_stream_progress,
    merge_transfer_counters,
)


class DownloadProgressTests(unittest.TestCase):
    def test_transfer_text_formatters_reject_non_finite_values(self) -> None:
        self.assertEqual(format_speed(float("nan")), "")
        self.assertEqual(format_speed(float("inf")), "")
        self.assertEqual(format_eta(float("nan")), "")
        self.assertEqual(format_eta("invalid"), "")
        self.assertEqual(format_speed(1024 * 1024), "1.00 MiB/s")
        self.assertEqual(format_eta(65), "01:05")

    def test_stage_merge_accepts_explicit_zero_retry_values(self) -> None:
        merged = merge_stage_progress(
            StageProgressState(
                stage="reconnecting",
                stage_text="Retrying",
                stage_progress=20.0,
                retry_count=2,
                retry_total=5,
                reconnect_message="2 seconds",
            ),
            {
                "stage": "parsing",
                "stage_text": "Parsing",
                "retry_count": 0,
                "retry_total": 0,
            },
        )

        self.assertTrue(merged.stage_changed)
        self.assertTrue(merged.reset_transfer_rate)
        self.assertEqual(merged.state.stage_progress, 0.0)
        self.assertEqual(merged.state.retry_count, 0)
        self.assertEqual(merged.state.retry_total, 0)
        self.assertEqual(merged.state.reconnect_message, "")

    def test_invalid_same_stage_numbers_preserve_last_valid_values(self) -> None:
        current = StageProgressState(
            stage="transcoding",
            stage_text="Converting",
            stage_progress=42.0,
            retry_count=1,
            retry_total=3,
            elapsed_seconds=15.0,
            stage_elapsed_seconds=5.0,
            transcode_encoder="h264_nvenc",
        )
        merged = merge_stage_progress(
            current,
            {
                "stage": "transcoding",
                "stage_progress": float("nan"),
                "retry_count": "invalid",
                "retry_total": float("inf"),
                "elapsed_seconds": "invalid",
                "stage_elapsed_seconds": float("-inf"),
            },
        )

        self.assertFalse(merged.stage_changed)
        self.assertEqual(merged.state.stage_progress, 42.0)
        self.assertEqual(merged.state.retry_count, 1)
        self.assertEqual(merged.state.retry_total, 3)
        self.assertEqual(merged.state.elapsed_seconds, 15.0)
        self.assertEqual(merged.state.stage_elapsed_seconds, 5.0)
        self.assertEqual(merged.state.transcode_encoder, "h264_nvenc")

    def test_stage_and_stream_percentages_are_bounded(self) -> None:
        merged = merge_stage_progress(
            StageProgressState(stage="downloading_video"),
            {"stage": "downloading_video", "stage_progress": 150},
        )
        self.assertEqual(merged.state.stage_progress, 100.0)
        self.assertEqual(merge_stream_progress(20, 180), 100.0)
        self.assertEqual(merge_stream_progress(20, float("nan")), 20.0)

    def test_transfer_counters_ignore_invalid_values_and_repair_current_state(self) -> None:
        merged = merge_transfer_counters(
            TransferCounterState(
                progress=float("nan"),
                downloaded_bytes="invalid",  # type: ignore[arg-type]
                total_bytes=1_000,
                visible_progress=float("inf"),
                visible_downloaded_bytes=[],  # type: ignore[arg-type]
                visible_total_bytes={},  # type: ignore[arg-type]
            ),
            {
                "downloaded_bytes": 250,
                "total_bytes": float("inf"),
                "_percent_str": "unknown%",
            },
        )

        self.assertEqual(merged.progress, 25.0)
        self.assertEqual(merged.downloaded_bytes, 250)
        self.assertEqual(merged.total_bytes, 1_000)
        self.assertEqual(merged.visible_progress, 25.0)
        self.assertEqual(merged.visible_downloaded_bytes, 250)
        self.assertEqual(merged.visible_total_bytes, 1_000)

    def test_transfer_counters_never_regress_on_stream_switch_callbacks(self) -> None:
        merged = merge_transfer_counters(
            TransferCounterState(
                progress=80.0,
                downloaded_bytes=800,
                total_bytes=1_000,
                visible_progress=80.0,
                visible_downloaded_bytes=800,
                visible_total_bytes=1_000,
            ),
            {
                "downloaded_bytes": 10,
                "total_bytes": 100,
                "_percent_str": "10%",
            },
        )

        self.assertEqual(merged.progress, 80.0)
        self.assertEqual(merged.downloaded_bytes, 800)
        self.assertEqual(merged.total_bytes, 1_000)


if __name__ == "__main__":
    unittest.main()

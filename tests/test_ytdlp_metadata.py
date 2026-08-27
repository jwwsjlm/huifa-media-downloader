from __future__ import annotations

import unittest
from collections import UserDict

from app.core.ytdlp_metadata import (
    build_format_choices,
    is_ytdlp_collection_result,
    media_capability_profile,
    media_source_key,
    selected_video_quality,
)


class YtdlpMetadataTests(unittest.TestCase):
    def test_source_key_normalizes_extractor_but_preserves_case_sensitive_id(self) -> None:
        self.assertEqual(
            media_source_key({"extractor_key": "YouTube", "id": "AbC123"}),
            "youtube:AbC123",
        )

    def test_selected_quality_ignores_invalid_numeric_candidates(self) -> None:
        quality = selected_video_quality({
            "requested_formats": [
                "invalid",
                {
                    "vcodec": "av01.0.12M.08",
                    "width": "7680",
                    "height": "4320",
                    "fps": "60",
                    "dynamic_range": "HDR",
                },
                {
                    "vcodec": "h264",
                    "width": float("inf"),
                    "height": float("nan"),
                    "fps": "invalid",
                },
            ],
        })

        self.assertEqual(quality, "8K · 7680×4320 · 60 FPS · AV1 · HDR")

    def test_selected_quality_follows_nested_requested_download_formats(self) -> None:
        quality = selected_video_quality({
            "width": 640,
            "height": 360,
            "vcodec": "avc1",
            "requested_downloads": [{
                "format_id": "merged",
                "requested_formats": [
                    {
                        "format_id": "video-4k",
                        "width": 3840,
                        "height": 2160,
                        "fps": 60,
                        "vcodec": "vp09.00.51.08",
                        "acodec": "none",
                    },
                    {"format_id": "audio", "vcodec": "none", "acodec": "opus"},
                ],
            }],
        })

        self.assertEqual(quality, "4K · 3840×2160 · 60 FPS · VP9")

    def test_portrait_video_uses_short_dimension_for_conventional_quality(self) -> None:
        self.assertEqual(
            selected_video_quality({
                "width": 1080,
                "height": 1920,
                "fps": 30,
                "vcodec": "h264",
            }),
            "1080p · 1080×1920 · 30 FPS · H.264",
        )

    def test_requested_format_cycle_is_bounded(self) -> None:
        info = {
            "width": 1920,
            "height": 1080,
            "vcodec": "h264",
        }
        info["requested_downloads"] = [info]

        self.assertEqual(
            selected_video_quality(info),
            "1080p · 1920×1080 · H.264",
        )

    def test_collection_result_recognizes_playlist_and_multi_video(self) -> None:
        self.assertTrue(is_ytdlp_collection_result({"_type": "playlist"}))
        self.assertTrue(is_ytdlp_collection_result(UserDict({"_type": "Multi_Video"})))
        self.assertFalse(is_ytdlp_collection_result({"_type": "video"}))

    def test_format_choices_ignore_malformed_entries_and_normalize_numeric_fields(self) -> None:
        choices = build_format_choices({
            "formats": [
                None,
                "invalid",
                UserDict({
                    "format_id": "video",
                    "height": "1080",
                    "fps": "59.94",
                    "tbr": "4500",
                    "vcodec": "avc1.64002a",
                    "acodec": "none",
                    "ext": "mp4",
                    "filesize": "invalid",
                }),
                {
                    "format_id": "audio",
                    "vcodec": "none",
                    "acodec": "opus",
                    "abr": "160.4",
                    "ext": "webm",
                },
            ],
        })

        self.assertEqual(choices[0]["selector"], "video+bestaudio/best")
        self.assertEqual(choices[0]["height"], 1080)
        self.assertEqual(choices[0]["fps"], "59")
        self.assertEqual(choices[0]["filesize"], 0)
        self.assertEqual(choices[-1]["selector"], "audio")
        self.assertEqual(choices[-1]["abr"], 160)

    def test_format_choices_keep_portrait_hdr_fps_and_muxed_variants(self) -> None:
        choices = build_format_choices({
            "formats": [
                {
                    "format_id": "portrait-hdr-video",
                    "width": 1080,
                    "height": 1920,
                    "fps": 60,
                    "vcodec": "h264",
                    "acodec": "none",
                    "ext": "mp4",
                    "dynamic_range": "HDR10",
                },
                {
                    "format_id": "portrait-sdr-video",
                    "width": 1080,
                    "height": 1920,
                    "fps": 60,
                    "vcodec": "h264",
                    "acodec": "none",
                    "ext": "mp4",
                    "dynamic_range": "SDR",
                },
                {
                    "format_id": "portrait-sdr-30",
                    "width": 1080,
                    "height": 1920,
                    "fps": 30,
                    "vcodec": "h264",
                    "acodec": "none",
                    "ext": "mp4",
                },
                {
                    "format_id": "portrait-muxed",
                    "width": 1080,
                    "height": 1920,
                    "fps": 60,
                    "vcodec": "h264",
                    "acodec": "aac",
                    "ext": "mp4",
                },
            ],
        })
        videos = [choice for choice in choices if choice["kind"] == "video"]

        self.assertEqual(len(videos), 4)
        self.assertTrue(all(choice["height"] == 1080 for choice in videos))
        self.assertTrue(all("1080p (1080×1920)" in choice["label"] for choice in videos))
        self.assertIn("HDR", videos[0]["label"])
        self.assertEqual(
            next(choice for choice in videos if choice["selector"] == "portrait-muxed")['has_audio'],
            True,
        )

    def test_resolution_only_formats_are_available_for_manual_selection(self) -> None:
        choices = build_format_choices({
            "formats": [
                {
                    "format_id": "resolution-field",
                    "resolution": "2560x1440",
                    "fps": 60,
                    "vcodec": "vp9",
                    "acodec": "none",
                    "ext": "webm",
                },
                {
                    "format_id": "format-note-field",
                    "format_note": "1080p60 HDR",
                    "fps": 60,
                    "vcodec": "h264",
                    "acodec": "aac",
                    "ext": "mp4",
                },
            ],
        })
        videos = [choice for choice in choices if choice["kind"] == "video"]

        self.assertEqual([choice["height"] for choice in videos], [1440, 1080])
        self.assertEqual(videos[0]["width"], 2560)
        self.assertEqual(videos[0]["source_height"], 1440)
        self.assertIn("1440p (2560×1440)", videos[0]["label"])
        self.assertTrue(videos[1]["hdr"])

    def test_capability_profile_accepts_one_format_mapping_without_a_list(self) -> None:
        profile = media_capability_profile({
            "formats": {
                "format_id": "single-mapping",
                "url": "https://media.example/single",
                "resolution": "3840×2160",
                "vcodec": "av1",
                "acodec": "none",
            },
        })

        self.assertTrue(profile.usable)
        self.assertEqual((profile.width, profile.height), (3840, 2160))

    def test_audio_choices_accept_missing_or_case_insensitive_none_video_codec(self) -> None:
        choices = build_format_choices({
            "formats": [
                {
                    "format_id": "missing-vcodec",
                    "acodec": "opus",
                    "abr": 160,
                    "ext": "webm",
                },
                {
                    "format_id": "upper-none",
                    "vcodec": "NONE",
                    "acodec": "aac",
                    "abr": 128,
                    "ext": "m4a",
                },
            ],
        })
        audio_selectors = {
            choice["selector"]
            for choice in choices
            if choice["kind"] == "audio"
        }

        self.assertEqual(
            audio_selectors,
            {"bestaudio/best", "missing-vcodec", "upper-none"},
        )

    def test_capability_profile_handles_non_iterable_formats_and_counts_playable_entries(self) -> None:
        profile = media_capability_profile({
            "_type": "playlist",
            "entries": [
                {"id": "broken", "formats": 42},
                {"_type": "url", "url": "https://example.test/playable"},
            ],
        })

        self.assertTrue(profile.usable)
        self.assertEqual(profile.collection_items, 1)
        self.assertEqual(profile.playable_formats, 1)


if __name__ == "__main__":
    unittest.main()

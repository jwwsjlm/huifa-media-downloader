from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import app.core.download_service as download_module
from app.core.download_service import (
    DownloadWorker,
    media_capability_profile,
)


def video_info(format_id: str, width: int, height: int, fps: float, *, audio: bool = False) -> dict:
    return {
        "id": "video",
        "formats": [
            {
                "format_id": format_id,
                "url": f"https://media.example/{format_id}",
                "ext": "mp4",
                "width": width,
                "height": height,
                "fps": fps,
                "tbr": 8_000 if height >= 2160 else 800,
                "vcodec": "av01" if height >= 2160 else "avc1",
                "acodec": "mp4a" if audio else "none",
            },
            {
                "format_id": "audio",
                "url": "https://media.example/audio",
                "ext": "webm",
                "vcodec": "none",
                "acodec": "opus",
                "abr": 160,
            },
        ],
    }


class AdaptiveCookieTests(unittest.TestCase):
    def test_media_profile_prefers_real_8k_formats_over_360p_cookie_fallback(self) -> None:
        anonymous = media_capability_profile(video_info("702", 7680, 4320, 60))
        cookie = media_capability_profile(video_info("18", 640, 360, 30, audio=True))

        self.assertTrue(anonymous.usable)
        self.assertEqual((anonymous.width, anonymous.height, anonymous.fps), (7680, 4320, 60))
        self.assertGreater(anonymous.score, cookie.score)

    def test_media_profile_prefers_cookie_when_it_unlocks_more_collection_items(self) -> None:
        anonymous = media_capability_profile({
            "_type": "playlist",
            "entries": [{"_type": "url", "url": "https://example.test/1"}],
        })
        cookie = media_capability_profile({
            "_type": "playlist",
            "entries": [
                {"_type": "url", "url": "https://example.test/1"},
                {"_type": "url", "url": "https://example.test/2"},
            ],
        })

        self.assertGreater(cookie.score, anonymous.score)

    def test_builtin_probe_drops_cookie_when_anonymous_quality_is_higher(self) -> None:
        worker = DownloadWorker(
            "adaptive-builtin",
            "https://video.example/watch/1",
            ".",
            object(),
            playlist_mode="single",
        )
        logs: list[tuple[str, dict]] = []

        class FakeYoutubeDL:
            def __init__(self, options):
                self.options = dict(options)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def extract_info(self, _url, download=False):
                self.assert_download = download
                return (
                    video_info("18", 640, 360, 30, audio=True)
                    if "cookiefile" in self.options
                    else video_info("702", 7680, 4320, 60)
                )

        options = {"format": "bv*+ba/b", "cookiefile": "cookies.txt", "noplaylist": True}
        fake_ytdlp = SimpleNamespace(YoutubeDL=FakeYoutubeDL)
        with patch.object(download_module, "yt_dlp", fake_ytdlp), patch.object(
            worker,
            "_log",
            side_effect=lambda _level, _category, message, **details: logs.append((message, details)),
        ):
            preview = worker._select_adaptive_cookie_access("", options)

        self.assertNotIn("cookiefile", options)
        self.assertEqual(media_capability_profile(preview).height, 4320)
        self.assertTrue(any(details.get("reason") == "anonymous_quality_higher" for _, details in logs))

    def test_external_probe_keeps_cookie_when_anonymous_access_is_unusable(self) -> None:
        worker = DownloadWorker(
            "adaptive-external",
            "https://video.example/private/1",
            ".",
            object(),
            playlist_mode="single",
        )
        options = {"format": "bv*+ba/b", "cookiefile": "cookies.txt", "noplaylist": True}

        def fake_run(_executable, _url, probe_options, **_kwargs):
            if "cookiefile" not in probe_options:
                raise RuntimeError("login required")
            return video_info("cookie-hd", 1920, 1080, 60)

        with patch.object(download_module, "run_external_ytdlp", side_effect=fake_run), patch.object(
            worker, "_log", return_value=None
        ):
            preview = worker._select_adaptive_cookie_access("yt-dlp.exe", options)

        self.assertEqual(options["cookiefile"], "cookies.txt")
        self.assertEqual(media_capability_profile(preview).height, 1080)

    def test_cancel_during_cookie_probe_propagates_without_mutating_download_options(self) -> None:
        worker = DownloadWorker(
            "adaptive-cancel",
            "https://video.example/watch/cancel",
            ".",
            object(),
            playlist_mode="single",
        )
        options = {
            "format": "bv*+ba/b",
            "cookiefile": "cookies.txt",
            "noplaylist": True,
        }

        with patch.object(
            worker,
            "_probe_media_access",
            side_effect=(
                video_info("anonymous", 1920, 1080, 60),
                InterruptedError("用户取消下载"),
            ),
        ), patch.object(worker, "_log", return_value=None):
            with self.assertRaisesRegex(InterruptedError, "用户取消下载"):
                worker._select_adaptive_cookie_access("", options)

        self.assertEqual(options["cookiefile"], "cookies.txt")
        self.assertEqual(options["format"], "bv*+ba/b")

    def test_collection_probe_applies_access_choice_but_forces_full_followup_parse(self) -> None:
        worker = DownloadWorker(
            "adaptive-collection",
            "https://video.example/playlist/1",
            ".",
            object(),
            playlist_mode="playlist",
        )
        options = {"cookiefile": "cookies.txt"}
        anonymous = {
            "_type": "playlist",
            "entries": [
                {"_type": "url", "url": f"https://example.test/{index}"}
                for index in range(3)
            ],
        }
        cookie = {
            "_type": "playlist",
            "entries": [{"_type": "url", "url": "https://example.test/1"}],
        }
        logs: list[dict] = []

        with patch.object(
            worker,
            "_probe_media_access",
            side_effect=(anonymous, cookie),
        ), patch.object(
            worker,
            "_log",
            side_effect=lambda _level, _category, _message, **details: logs.append(details),
        ):
            preview = worker._select_adaptive_cookie_access("", options)

        self.assertIsNone(preview)
        self.assertNotIn("cookiefile", options)
        self.assertTrue(any(
            details.get("reason") == "anonymous_quality_higher"
            for details in logs
        ))


if __name__ == "__main__":
    unittest.main()

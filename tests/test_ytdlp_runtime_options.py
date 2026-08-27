from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

from app.core.download_options import DownloadOptions
from app.core.download_service import DownloadWorker
from app.core.ytdlp_runtime_options import (
    YtdlpDownloadOptionRequest,
    build_ytdlp_download_options,
)


def request_for(options: DownloadOptions, **overrides) -> YtdlpDownloadOptionRequest:
    values = {
        "output_template": "D:/downloads/%(title)s.%(ext)s",
        "output_dir": "D:/downloads",
        "quality": "4k",
        "download_options": options,
    }
    values.update(overrides)
    return YtdlpDownloadOptionRequest(**values)


class YtdlpRuntimeOptionsTests(unittest.TestCase):
    def test_video_options_include_resolved_runtime_and_bounded_network_values(self) -> None:
        options = DownloadOptions.from_mapping({
            "container": "mkv",
            "video_fps": "60",
            "source_video_codec": "h264",
            "rate_limit": "8M",
        })

        mapped = build_ytdlp_download_options(request_for(
            options,
            processing_workspace="E:/temp/task/download",
            filename_limit=180,
            request_delay=1.5,
            ffmpeg_location="D:/tools/ffmpeg.exe",
            proxy="http://127.0.0.1:7890",
            ejs_options={
                "js_runtimes": {"deno": {"path": "D:/tools/deno.exe"}},
                "remote_components": {"ejs:github"},
            },
            windows_filenames=True,
            fragment_concurrent=8,
        ))

        self.assertEqual(mapped["paths"], {
            "home": "D:/downloads",
            "temp": "E:/temp/task/download",
        })
        self.assertEqual(mapped["trim_file_name"], 180)
        self.assertEqual(mapped["sleep_interval_requests"], 1.5)
        self.assertEqual(mapped["ffmpeg_location"], "D:/tools/ffmpeg.exe")
        self.assertEqual(mapped["proxy"], "http://127.0.0.1:7890")
        self.assertEqual(mapped["concurrent_fragment_downloads"], 8)
        self.assertIn("fps:60", mapped["format_sort"])
        self.assertIn("vcodec:h264", mapped["format_sort"])
        self.assertEqual(mapped["merge_output_format"], "mkv")
        self.assertIn(
            {"key": "FFmpegVideoRemuxer", "preferedformat": "mkv"},
            mapped["postprocessors"],
        )
        self.assertEqual(mapped["ratelimit"], 8 * 1024 * 1024)
        self.assertNotIn("ratelimit_text", mapped)
        self.assertEqual(mapped["remote_components"], {"ejs:github"})

    def test_runtime_builder_bounds_invalid_fragment_and_delay_values(self) -> None:
        mapped = build_ytdlp_download_options(request_for(
            DownloadOptions(),
            fragment_concurrent=999,
            request_delay=float("inf"),
        ))

        self.assertEqual(mapped["concurrent_fragment_downloads"], 32)
        self.assertNotIn("sleep_interval_requests", mapped)

    def test_audio_mode_uses_audio_selector_without_video_sort_or_container(self) -> None:
        options = DownloadOptions.from_mapping({
            "content_mode": "audio",
            "audio_format": "flac",
            "container": "mkv",
        })

        mapped = build_ytdlp_download_options(request_for(options))

        self.assertEqual(mapped["format"], "bestaudio/best")
        self.assertNotIn("format_sort", mapped)
        self.assertNotIn("merge_output_format", mapped)
        self.assertIn(
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "flac",
                "preferredquality": "0",
            },
            mapped["postprocessors"],
        )

    def test_custom_transcode_removes_only_container_remux(self) -> None:
        options = DownloadOptions.from_mapping({
            "container": "mp4",
            "split_chapters": True,
            "embed_metadata": True,
        })

        mapped = build_ytdlp_download_options(request_for(
            options,
            remove_remux_postprocessor=True,
        ))

        self.assertNotIn("merge_output_format", mapped)
        keys = [processor["key"] for processor in mapped["postprocessors"]]
        self.assertNotIn("FFmpegVideoRemuxer", keys)
        self.assertIn("FFmpegSplitChapters", keys)
        self.assertIn("FFmpegMetadata", keys)

    def test_subtitle_and_playlist_policy_are_preserved(self) -> None:
        mapped = build_ytdlp_download_options(request_for(
            DownloadOptions(),
            subtitle_language="en",
            playlist_mode="single",
        ))

        self.assertTrue(mapped["noplaylist"])
        self.assertTrue(mapped["writesubtitles"])
        self.assertTrue(mapped["writeautomaticsub"])
        self.assertIn("en", mapped["subtitleslangs"])

    def test_worker_builds_options_when_deno_and_remote_ejs_are_available(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worker = DownloadWorker(
                "deno-options",
                "https://www.youtube.com/watch?v=demo",
                directory,
                object(),
                deno_path="tools/deno/deno.exe",
                ytdlp_ejs_source="github",
            )
            ejs_options = {
                "js_runtimes": {"deno": {"path": "tools/deno/deno.exe"}},
                "remote_components": {"ejs:github"},
            }

            with patch(
                "app.core.download_service.ytdlp_ejs_runtime_options",
                return_value=(ejs_options, "tools/deno/deno.exe", "github"),
            ), patch(
                "app.core.download_service.ffmpeg_runtime_path",
                return_value="",
            ), patch.object(worker, "_log"):
                mapped = worker._build_ytdlp_options()

        self.assertEqual(mapped["js_runtimes"], ejs_options["js_runtimes"])
        self.assertEqual(mapped["remote_components"], {"ejs:github"})
        self.assertEqual(len(mapped["progress_hooks"]), 1)
        self.assertEqual(len(mapped["postprocessor_hooks"]), 1)

    def test_worker_without_deno_builds_normal_options_and_records_warning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worker = DownloadWorker(
                "no-deno-options",
                "https://example.com/video",
                directory,
                object(),
            )
            logs: list[tuple[str, str]] = []
            with patch(
                "app.core.download_service.ytdlp_ejs_runtime_options",
                return_value=({}, "", "auto"),
            ), patch(
                "app.core.download_service.ffmpeg_runtime_path",
                return_value="",
            ), patch.object(
                worker,
                "_log",
                side_effect=lambda level, _category, message, **_details: logs.append(
                    (level, message)
                ),
            ):
                mapped = worker._build_ytdlp_options()

        self.assertNotIn("js_runtimes", mapped)
        self.assertNotIn("remote_components", mapped)
        self.assertIn(
            ("warning", "未找到 Deno；部分 YouTube 格式可能缺少可下载格式"),
            logs,
        )


if __name__ == "__main__":
    unittest.main()

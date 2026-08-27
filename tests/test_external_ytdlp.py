from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.download_service import ytdlp_ejs_runtime_options
from app.core.external_ytdlp import (
    PROGRESS_PREFIX,
    RESULT_PREFIX,
    build_external_ytdlp_command,
    cached_external_ytdlp_version,
    clear_external_ytdlp_version_cache,
    remember_external_ytdlp_version,
    run_external_ytdlp,
)


class ExternalYtdlpTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_external_ytdlp_version_cache()

    def test_startup_version_probe_is_reused_by_download_workers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            executable = Path(temp_dir) / "yt-dlp.exe"
            self.assertIsNone(cached_external_ytdlp_version(executable))
            remember_external_ytdlp_version(executable, "2026.08.25")
            self.assertEqual(cached_external_ytdlp_version(executable), "2026.08.25")

    def test_ejs_source_maps_to_official_remote_component_modes(self) -> None:
        with patch("app.core.download_service.deno_runtime_path", return_value="D:/tools/deno.exe"), patch(
            "app.core.download_service.activate_local_ejs", return_value=None
        ):
            automatic, deno, source = ytdlp_ejs_runtime_options("configured.exe", "auto")
            github, _, _ = ytdlp_ejs_runtime_options("configured.exe", "github")
            local, _, _ = ytdlp_ejs_runtime_options("configured.exe", "local")

        self.assertEqual(deno, "D:/tools/deno.exe")
        self.assertEqual(source, "auto")
        self.assertEqual(automatic["remote_components"], {"ejs:npm"})
        self.assertEqual(github["remote_components"], {"ejs:github"})
        self.assertNotIn("remote_components", local)
        self.assertEqual(local["js_runtimes"]["deno"]["path"], "D:/tools/deno.exe")

        with patch("app.core.download_service.deno_runtime_path", return_value="D:/tools/deno.exe"), patch(
            "app.core.download_service.activate_local_ejs", return_value=object()
        ):
            local_first, _, _ = ytdlp_ejs_runtime_options("configured.exe", "auto")
        self.assertNotIn("remote_components", local_first)

    def test_command_translates_runtime_cookie_and_download_options(self) -> None:
        command = build_external_ytdlp_command(
            "yt-dlp.exe",
            "https://example.com/video",
            {
                "outtmpl": "%(title)s.%(ext)s",
                "paths": {"home": "D:/youtube", "temp": "E:/huifa-temp/task-1"},
                "windowsfilenames": True,
                "trim_file_name": 120,
                "format": "bv*+ba/b",
                "ffmpeg_location": "D:/tools/ffmpeg.exe",
                "cookiefile": "D:/temp/cookies.txt",
                "js_runtimes": {"deno": {"path": "D:/tools/deno.exe"}},
                "remote_components": {"ejs:github"},
                "concurrent_fragment_downloads": 16,
                "writesubtitles": True,
                "writeautomaticsub": True,
                "subtitleslangs": ["zh-Hans"],
                "subtitlesformat": "srt/vtt/best",
            },
            download=True,
        )
        self.assertIn("--ignore-config", command)
        self.assertIn("--windows-filenames", command)
        path_values = [
            command[index + 1]
            for index, value in enumerate(command[:-1])
            if value == "--paths"
        ]
        self.assertEqual(path_values, ["home:D:/youtube", "temp:E:/huifa-temp/task-1"])
        self.assertEqual(command[command.index("--trim-filenames") + 1], "120")
        self.assertEqual(command[command.index("--cookies") + 1], "D:/temp/cookies.txt")
        self.assertEqual(command[command.index("--js-runtimes") + 1], "deno:D:/tools/deno.exe")
        self.assertEqual(command[command.index("--remote-components") + 1], "ejs:github")
        self.assertEqual(command[command.index("--concurrent-fragments") + 1], "16")
        self.assertIn("--write-subs", command)
        self.assertIn("--write-auto-subs", command)
        self.assertEqual(command[command.index("--sub-langs") + 1], "zh-Hans")
        self.assertEqual(command[command.index("--sub-format") + 1], "srt/vtt/best")
        self.assertIn(f"download:{PROGRESS_PREFIX}%()j", command)
        self.assertIn(f"after_move:{RESULT_PREFIX}%()j", command)

    def test_single_string_list_options_are_not_split_into_characters(self) -> None:
        command = build_external_ytdlp_command(
            "yt-dlp.exe",
            "https://example.com/video",
            {
                "subtitleslangs": "en",
                "download_sections": "*00:00-00:10",
                "remote_components": "ejs:github",
                "postprocessors": [
                    {"key": "SponsorBlock", "categories": "sponsor"},
                ],
            },
            download=True,
        )

        self.assertEqual(command[command.index("--sub-langs") + 1], "en")
        self.assertEqual(
            command[command.index("--download-sections") + 1],
            "*00:00-00:10",
        )
        self.assertEqual(
            command[command.index("--remote-components") + 1],
            "ejs:github",
        )
        self.assertEqual(
            command[command.index("--sponsorblock-mark") + 1],
            "sponsor",
        )

    def test_external_runner_parses_progress_and_completion_json(self) -> None:
        progress = {
            "progress": {"status": "downloading", "downloaded_bytes": 128, "total_bytes": 256},
            "info": {"id": "demo"},
        }
        result = {"id": "demo", "title": "Example", "filepath": "D:/youtube/example.mp4"}

        class FakeStdout:
            def __iter__(self):
                return iter(
                    [
                        f"{PROGRESS_PREFIX}{json.dumps(progress)}\n",
                        "[download] Destination: example.mp4\n",
                        f"{RESULT_PREFIX}{json.dumps(result)}\n",
                    ]
                )

            def close(self):
                return None

        class FakeProcess:
            stdout = FakeStdout()
            pid = 123

            def poll(self):
                return 0

            def wait(self, timeout=None):
                return 0

            def terminate(self):
                return None

        progress_events = []
        log_lines = []
        with patch("app.core.external_ytdlp.subprocess.Popen", return_value=FakeProcess()):
            actual = run_external_ytdlp(
                "yt-dlp.exe",
                "https://example.com/video",
                {},
                download=True,
                cancel_event=threading.Event(),
                progress_hook=progress_events.append,
                log_line=log_lines.append,
            )
        self.assertEqual(actual, result)
        self.assertEqual(progress_events, [progress])
        self.assertEqual(log_lines, ["[download] Destination: example.mp4"])

    def test_external_probe_aggregates_every_dump_json_entry(self) -> None:
        first = {
            "id": "a",
            "title": "First",
            "playlist_id": "collection-1",
            "playlist_title": "Collection",
            "extractor_key": "Example",
        }
        second = {
            "id": "b",
            "title": "Second",
            "playlist_id": "collection-1",
            "playlist_title": "Collection",
            "extractor_key": "Example",
        }

        class FakeStdout:
            def __iter__(self):
                return iter([
                    json.dumps(first) + "\n",
                    "[warning] temporary message\n",
                    json.dumps(second) + "\n",
                ])

            def close(self):
                return None

        class FakeProcess:
            stdout = FakeStdout()
            pid = 124

            @staticmethod
            def poll():
                return 0

            @staticmethod
            def wait(timeout=None):
                return 0

            @staticmethod
            def terminate():
                return None

        logs: list[str] = []
        with patch("app.core.external_ytdlp.subprocess.Popen", return_value=FakeProcess()):
            actual = run_external_ytdlp(
                "yt-dlp.exe",
                "https://example.com/collection",
                {"dump_json": True},
                download=False,
                cancel_event=threading.Event(),
                log_line=logs.append,
            )

        self.assertEqual(actual["_type"], "playlist")
        self.assertEqual(actual["id"], "collection-1")
        self.assertEqual(actual["title"], "Collection")
        self.assertEqual(actual["playlist_count"], 2)
        self.assertEqual(actual["entries"], [first, second])
        self.assertEqual(logs, ["[warning] temporary message"])

    def test_external_runner_cancellation_terminates_process_tree(self) -> None:
        class FakeStdout:
            def __iter__(self):
                return iter(())

            def close(self):
                return None

        class FakeProcess:
            stdout = FakeStdout()
            pid = 125

            @staticmethod
            def poll():
                return None

            @staticmethod
            def wait(timeout=None):
                return 0

        cancelled = threading.Event()
        cancelled.set()
        process = FakeProcess()
        with patch(
            "app.core.external_ytdlp.subprocess.Popen",
            return_value=process,
        ), patch("app.core.external_ytdlp.terminate_external_ytdlp_process") as terminate:
            with self.assertRaisesRegex(InterruptedError, "取消"):
                run_external_ytdlp(
                    "yt-dlp.exe",
                    "https://example.com/video",
                    {},
                    download=True,
                    cancel_event=cancelled,
                )

        terminate.assert_called_once_with(process)

    def test_exited_process_does_not_wait_forever_for_inherited_stdout(self) -> None:
        reader_entered = threading.Event()
        release_reader = threading.Event()
        reader_finished = threading.Event()

        class BlockingStdout:
            def __init__(self):
                self.closed = False

            def __iter__(self):
                reader_entered.set()
                release_reader.wait(2)
                reader_finished.set()
                return iter(())

            def close(self):
                self.closed = True

        class FakeProcess:
            stdout = BlockingStdout()
            pid = 126

            @staticmethod
            def poll():
                return 1

            @staticmethod
            def wait(timeout=None):
                return 1

        process = FakeProcess()
        try:
            with patch(
                "app.core.external_ytdlp.subprocess.Popen",
                return_value=process,
            ), patch(
                "app.core.external_ytdlp._OUTPUT_POLL_SECONDS",
                0.005,
            ), patch(
                "app.core.external_ytdlp._OUTPUT_DRAIN_GRACE_SECONDS",
                0.02,
            ), patch(
                "app.core.external_ytdlp._READER_JOIN_TIMEOUT_SECONDS",
                0.01,
            ):
                started_at = time.monotonic()
                with self.assertRaisesRegex(RuntimeError, "代码 1"):
                    run_external_ytdlp(
                        "yt-dlp.exe",
                        "https://example.com/video",
                        {},
                        download=True,
                        cancel_event=threading.Event(),
                    )
                elapsed = time.monotonic() - started_at

            self.assertTrue(reader_entered.is_set())
            self.assertLess(elapsed, 0.5)
            self.assertFalse(
                process.stdout.closed,
                "仍在读取的管道不应由任务线程强行关闭",
            )
        finally:
            release_reader.set()
            self.assertTrue(reader_finished.wait(1))

    def test_output_reader_start_failure_terminates_started_process(self) -> None:
        class FakeStdout:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        class FakeProcess:
            stdout = FakeStdout()
            pid = 127

        process = FakeProcess()
        with patch(
            "app.core.external_ytdlp.subprocess.Popen",
            return_value=process,
        ), patch(
            "app.core.external_ytdlp.threading.Thread.start",
            side_effect=RuntimeError("thread unavailable"),
        ), patch(
            "app.core.external_ytdlp.terminate_external_ytdlp_process",
        ) as terminate:
            with self.assertRaisesRegex(RuntimeError, "无法读取外置 yt-dlp 输出"):
                run_external_ytdlp(
                    "yt-dlp.exe",
                    "https://example.com/video",
                    {},
                    download=True,
                    cancel_event=threading.Event(),
                )

        terminate.assert_called_once_with(process)
        self.assertTrue(process.stdout.closed)

    def test_windows_taskkill_failure_falls_back_to_process_termination(self) -> None:
        import app.core.external_ytdlp as external_module

        class FakeProcess:
            pid = 126

            def __init__(self):
                self.terminated = 0
                self.waited = 0

            @staticmethod
            def poll():
                return None

            def terminate(self):
                self.terminated += 1

            def wait(self, timeout=None):
                self.waited += 1
                return 0

        process = FakeProcess()
        failed_taskkill = subprocess.CompletedProcess([], 1)
        with patch.object(external_module.os, "name", "nt"), patch(
            "app.core.external_ytdlp.subprocess.run",
            return_value=failed_taskkill,
        ):
            external_module.terminate_external_ytdlp_process(process)

        self.assertEqual(process.terminated, 1)
        self.assertEqual(process.waited, 1)


if __name__ == "__main__":
    unittest.main()

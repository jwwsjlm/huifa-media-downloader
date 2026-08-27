from __future__ import annotations

import os
import unittest
import threading
from unittest.mock import call, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.core.local_core_versions import LocalCoreVersionWorker


class LocalCoreVersionWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_detects_every_local_download_component_with_configured_paths(self) -> None:
        versions = {
            "yt-dlp": ("2026.08.25", "local", "yt-dlp.exe"),
            "yt-dlp-ejs": ("0.9.0", "local", "ejs.whl"),
            "Deno": ("2.5.0", "local", "deno.exe"),
            "FFmpeg": ("8.0", "local", "ffmpeg.exe"),
            "FFprobe": ("8.0", "local", "ffprobe.exe"),
        }

        def detect(name: str, *_paths: str) -> tuple[str, str, str]:
            return versions[name]

        worker = LocalCoreVersionWorker("D:/deno.exe", "D:/ffmpeg.exe", "D:/ffprobe.exe")
        completed: list[dict] = []
        worker.completed.connect(completed.append)
        with patch("app.core.local_core_versions.installed_component_details", side_effect=detect) as detector, patch(
            "app.core.local_core_versions.compiled_transcode_encoders",
            return_value=("libx264", "h264_nvenc"),
        ) as encoder_detector:
            worker.run()

        expected = dict(versions)
        expected["__video_encoders__"] = {
            "items": ("libx264", "h264_nvenc"),
            "error": "",
        }
        self.assertEqual(completed, [expected])
        encoder_detector.assert_called_once_with("ffmpeg.exe", worker._cancelled)
        self.assertEqual(
            detector.call_args_list,
            [
                call("yt-dlp", ""),
                call("yt-dlp-ejs", ""),
                call("Deno", "D:/deno.exe"),
                call("FFmpeg", "D:/ffmpeg.exe"),
                call("FFprobe", "D:/ffmpeg.exe", "D:/ffprobe.exe"),
            ],
        )

    def test_one_failed_probe_does_not_hide_other_versions(self) -> None:
        def detect(name: str, *_paths: str) -> tuple[str, str, str]:
            if name == "Deno":
                raise RuntimeError("probe error")
            return ("1.0", "local", f"{name}.exe")

        worker = LocalCoreVersionWorker()
        completed: list[dict] = []
        worker.completed.connect(completed.append)
        with patch("app.core.local_core_versions.installed_component_details", side_effect=detect), patch(
            "app.core.local_core_versions.compiled_transcode_encoders",
            return_value=(),
        ):
            worker.run()

        self.assertEqual(completed[0]["Deno"], ("检测失败", "probe error", ""))
        self.assertEqual(completed[0]["yt-dlp"][0], "1.0")
        self.assertEqual(completed[0]["FFprobe"][0], "1.0")
        self.assertEqual(completed[0]["__video_encoders__"]["items"], ())

    def test_cancel_returns_without_waiting_for_blocked_version_commands(self) -> None:
        worker = LocalCoreVersionWorker()
        started = threading.Event()
        release = threading.Event()

        def detect(_name: str, *_paths: str) -> tuple[str, str, str]:
            started.set()
            release.wait(5)
            return ("1.0", "local", "tool.exe")

        caller = threading.Thread(target=worker.run)
        with patch("app.core.local_core_versions.installed_component_details", side_effect=detect), patch(
            "app.core.local_core_versions.compiled_transcode_encoders",
            return_value=(),
        ):
            caller.start()
            self.assertTrue(started.wait(1))
            worker.cancel()
            caller.join(0.5)
            release.set()

        self.assertFalse(caller.is_alive())

    def test_orchestrator_failure_still_emits_terminal_result(self) -> None:
        worker = LocalCoreVersionWorker()
        completed: list[dict] = []
        worker.completed.connect(completed.append)

        with patch(
            "app.core.local_core_versions.run_disposable_jobs",
            side_effect=RuntimeError("executor unavailable"),
        ), patch(
            "app.core.local_core_versions.compiled_transcode_encoders",
        ) as encoder_detector:
            worker.run()

        self.assertEqual(len(completed), 1)
        result = completed[0]
        for component in ("yt-dlp", "yt-dlp-ejs", "Deno", "FFmpeg", "FFprobe"):
            self.assertEqual(result[component], ("检测失败", "executor unavailable", ""))
        self.assertEqual(
            result["__video_encoders__"],
            {"items": (), "error": "executor unavailable"},
        )
        encoder_detector.assert_not_called()


if __name__ == "__main__":
    unittest.main()

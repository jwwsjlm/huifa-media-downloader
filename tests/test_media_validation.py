from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from app.core.media_validation import (
    MediaValidationError,
    MediaValidationErrorCode,
    validate_media_file,
)


def probe_result(payload: object, *, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["ffprobe"],
        returncode=returncode,
        stdout=json.dumps(payload),
        stderr=stderr,
    )


def valid_payload(*, audio: bool = True) -> dict[str, object]:
    streams: list[dict[str, object]] = [
        {
            "codec_type": "video",
            "codec_name": "h264",
            "width": 1920,
            "height": 1080,
            "duration": "12.5",
            "disposition": {"attached_pic": 0},
        }
    ]
    if audio:
        streams.append({"codec_type": "audio", "codec_name": "aac", "duration": "12.48"})
    return {
        "format": {
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
            "format_long_name": "QuickTime / MOV",
            "duration": "12.5",
        },
        "streams": streams,
    }


class MediaValidationTests(unittest.TestCase):
    def test_valid_media_returns_immutable_metadata_and_bounded_probe_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "video.mp4"
            media.write_bytes(b"not-a-real-video-but-readable")
            completed = probe_result(valid_payload())

            with patch("app.core.media_validation.subprocess.run", return_value=completed) as run:
                result = validate_media_file(
                    media,
                    "C:/portable/ffprobe.exe",
                    require_audio=True,
                    timeout_seconds=7.5,
                )

            self.assertEqual(result.file_path, str(media))
            self.assertEqual(result.size_bytes, media.stat().st_size)
            self.assertEqual(result.duration_seconds, 12.5)
            self.assertEqual(result.container, "mov,mp4,m4a,3gp,3g2,mj2")
            self.assertEqual(result.stream_count, 2)
            self.assertEqual(result.video_stream_count, 1)
            self.assertEqual(result.audio_stream_count, 1)
            self.assertTrue(result.has_video)
            self.assertTrue(result.has_audio)
            with self.assertRaises(FrozenInstanceError):
                result.size_bytes = 1  # type: ignore[misc]

            command = run.call_args.args[0]
            kwargs = run.call_args.kwargs
            self.assertEqual(command[0], "C:/portable/ffprobe.exe")
            self.assertNotIn("-nostdin", command)
            self.assertIn("-print_format", command)
            self.assertIn("-show_entries", command)
            self.assertEqual(command[-2:], ["-i", str(media)])
            self.assertEqual(kwargs["timeout"], 7.5)
            self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
            self.assertFalse(kwargs["check"])

    def test_missing_file_fails_before_starting_ffprobe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.mp4"
            with patch("app.core.media_validation.subprocess.run") as run:
                with self.assertRaises(MediaValidationError) as raised:
                    validate_media_file(missing, "ffprobe")

            self.assertEqual(raised.exception.code, MediaValidationErrorCode.FILE_NOT_FOUND)
            self.assertIn("重新下载", raised.exception.action)
            run.assert_not_called()

    def test_directory_is_rejected_as_non_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch("app.core.media_validation.subprocess.run") as run:
                with self.assertRaises(MediaValidationError) as raised:
                    validate_media_file(directory, "ffprobe")

            self.assertEqual(raised.exception.code, MediaValidationErrorCode.NOT_REGULAR_FILE)
            run.assert_not_called()

    def test_zero_byte_file_fails_before_starting_ffprobe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "empty.mp4"
            media.touch()
            with patch("app.core.media_validation.subprocess.run") as run:
                with self.assertRaises(MediaValidationError) as raised:
                    validate_media_file(media, "ffprobe")

            self.assertEqual(raised.exception.code, MediaValidationErrorCode.EMPTY_FILE)
            run.assert_not_called()

    def test_unreadable_file_fails_before_starting_ffprobe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "locked.mp4"
            media.write_bytes(b"content")
            with patch.object(Path, "open", side_effect=PermissionError(13, "denied")), patch(
                "app.core.media_validation.subprocess.run"
            ) as run:
                with self.assertRaises(MediaValidationError) as raised:
                    validate_media_file(media, "ffprobe")

            self.assertEqual(raised.exception.code, MediaValidationErrorCode.FILE_UNREADABLE)
            self.assertNotIn("denied", str(raised.exception))
            run.assert_not_called()

    def test_damaged_file_reports_sanitized_bounded_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "private video.mp4"
            media.write_bytes(b"damaged")
            secret = "very-secret-token"
            stderr = (
                f"{media}: Invalid data; token={secret}; "
                f"source=https://example.test/watch?access_token={secret} " + "x" * 500
            )
            completed = probe_result({}, returncode=1, stderr=stderr)

            with patch("app.core.media_validation.subprocess.run", return_value=completed):
                with self.assertRaises(MediaValidationError) as raised:
                    validate_media_file(media, "ffprobe")

            error = raised.exception
            self.assertEqual(error.code, MediaValidationErrorCode.FFPROBE_FAILED)
            self.assertNotIn(secret, str(error))
            self.assertNotIn(secret, error.diagnostic)
            self.assertNotIn(str(media), error.diagnostic)
            self.assertNotIn("https://", error.diagnostic)
            self.assertLessEqual(len(error.diagnostic), 240)
            self.assertEqual(error.as_dict()["code"], "ffprobe_failed")

    def test_invalid_json_is_rejected_without_exposing_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "broken.mp4"
            media.write_bytes(b"broken")
            secret_stdout = "not-json access_token=do-not-leak"
            completed = subprocess.CompletedProcess(["ffprobe"], 0, secret_stdout, "")

            with patch("app.core.media_validation.subprocess.run", return_value=completed):
                with self.assertRaises(MediaValidationError) as raised:
                    validate_media_file(media, "ffprobe")

            self.assertEqual(raised.exception.code, MediaValidationErrorCode.FFPROBE_INVALID_OUTPUT)
            self.assertNotIn(secret_stdout, str(raised.exception))
            self.assertNotIn("do-not-leak", raised.exception.diagnostic)
            self.assertIsNone(raised.exception.__cause__)

    def test_no_valid_video_stream_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "cover-only.m4a"
            media.write_bytes(b"content")
            payload = {
                "format": {"format_name": "mp4", "duration": "5"},
                "streams": [
                    {"codec_type": "audio", "codec_name": "aac", "duration": "5"},
                    {
                        "codec_type": "video",
                        "codec_name": "mjpeg",
                        "width": 640,
                        "height": 640,
                        "disposition": {"attached_pic": 1},
                    },
                ],
            }
            with patch("app.core.media_validation.subprocess.run", return_value=probe_result(payload)):
                with self.assertRaises(MediaValidationError) as raised:
                    validate_media_file(media, "ffprobe")

            self.assertEqual(raised.exception.code, MediaValidationErrorCode.NO_VIDEO_STREAM)

    def test_audio_stream_is_only_required_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "silent.mp4"
            media.write_bytes(b"content")
            completed = probe_result(valid_payload(audio=False))

            with patch("app.core.media_validation.subprocess.run", return_value=completed):
                result = validate_media_file(media, "ffprobe")
            self.assertEqual(result.audio_stream_count, 0)

            with patch("app.core.media_validation.subprocess.run", return_value=completed):
                with self.assertRaises(MediaValidationError) as raised:
                    validate_media_file(media, "ffprobe", require_audio=True)
            self.assertEqual(raised.exception.code, MediaValidationErrorCode.NO_AUDIO_STREAM)

    def test_non_positive_duration_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "no-duration.mp4"
            media.write_bytes(b"content")
            payload = valid_payload()
            payload["format"]["duration"] = "0"  # type: ignore[index]
            for stream in payload["streams"]:  # type: ignore[union-attr]
                stream["duration"] = "N/A"

            with patch("app.core.media_validation.subprocess.run", return_value=probe_result(payload)):
                with self.assertRaises(MediaValidationError) as raised:
                    validate_media_file(media, "ffprobe")
            self.assertEqual(raised.exception.code, MediaValidationErrorCode.INVALID_DURATION)

    def test_cover_and_subtitle_durations_do_not_validate_missing_audio_duration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "audio-with-cover.m4a"
            media.write_bytes(b"content")
            payload = {
                "format": {"format_name": "mp4", "duration": "N/A"},
                "streams": [
                    {"codec_type": "audio", "codec_name": "aac", "duration": "N/A"},
                    {
                        "codec_type": "video",
                        "codec_name": "mjpeg",
                        "width": 1000,
                        "height": 1000,
                        "duration": "600",
                        "disposition": {"attached_pic": 1},
                    },
                    {"codec_type": "subtitle", "codec_name": "mov_text", "duration": "600"},
                ],
            }

            with patch(
                "app.core.media_validation.subprocess.run",
                return_value=probe_result(payload),
            ):
                with self.assertRaises(MediaValidationError) as raised:
                    validate_media_file(
                        media,
                        "ffprobe",
                        require_video=False,
                        require_audio=True,
                    )

            self.assertEqual(
                raised.exception.code,
                MediaValidationErrorCode.INVALID_DURATION,
            )

    def test_timeout_is_structured_and_does_not_include_process_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "slow.mp4"
            media.write_bytes(b"content")
            timeout = subprocess.TimeoutExpired(
                cmd=["ffprobe", str(media)],
                timeout=0.25,
                output="token=secret-output",
                stderr="cookie=secret-cookie",
            )

            with patch("app.core.media_validation.subprocess.run", side_effect=timeout):
                with self.assertRaises(MediaValidationError) as raised:
                    validate_media_file(media, "ffprobe", timeout_seconds=0.25)

            self.assertEqual(raised.exception.code, MediaValidationErrorCode.FFPROBE_TIMEOUT)
            self.assertIn("0.25 秒", raised.exception.message)
            self.assertNotIn("secret", str(raised.exception))
            self.assertFalse(raised.exception.diagnostic)
            self.assertIsNone(raised.exception.__cause__)

    def test_cancelable_probe_timeout_terminates_the_running_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "slow-cancelable.mp4"
            media.write_bytes(b"content")
            cancel_event = threading.Event()

            class RunningProbe:
                returncode = None

                def __init__(self) -> None:
                    self.terminated = False

                def poll(self):
                    return self.returncode

                def communicate(self, timeout=None):
                    if self.terminated:
                        return "", ""
                    raise subprocess.TimeoutExpired("ffprobe", timeout)

                def terminate(self) -> None:
                    self.terminated = True
                    self.returncode = -15

                def kill(self) -> None:
                    self.returncode = -9

            process = RunningProbe()
            with patch(
                "app.core.media_validation.subprocess.Popen",
                return_value=process,
            ), patch(
                "app.core.media_validation.time.monotonic",
                side_effect=(0.0, 0.0, 2.0),
            ):
                with self.assertRaises(MediaValidationError) as raised:
                    validate_media_file(
                        media,
                        "ffprobe",
                        timeout_seconds=1.0,
                        cancel_event=cancel_event,
                    )

            self.assertEqual(
                raised.exception.code,
                MediaValidationErrorCode.FFPROBE_TIMEOUT,
            )
            self.assertTrue(process.terminated)

    def test_missing_ffprobe_has_actionable_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "video.mp4"
            media.write_bytes(b"content")

            with patch("app.core.media_validation.subprocess.run", side_effect=FileNotFoundError):
                with self.assertRaises(MediaValidationError) as raised:
                    validate_media_file(media, "missing-ffprobe")

            self.assertEqual(raised.exception.code, MediaValidationErrorCode.FFPROBE_NOT_FOUND)
            self.assertIn("软件目录", raised.exception.action)

    def test_windows_process_uses_no_console_flag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "video.mp4"
            media.write_bytes(b"content")
            completed = probe_result(valid_payload())

            with patch("app.core.media_validation._IS_WINDOWS", True), patch(
                "app.core.media_validation.subprocess.run", return_value=completed
            ) as run:
                validate_media_file(media, "ffprobe")

            self.assertEqual(
                run.call_args.kwargs["creationflags"],
                getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
            )

    def test_cancelled_before_start_does_not_touch_file_or_probe(self) -> None:
        cancelled = threading.Event()
        cancelled.set()
        with patch("app.core.media_validation.Path.stat") as file_stat, patch(
            "app.core.media_validation.subprocess.run"
        ) as run:
            with self.assertRaises(MediaValidationError) as raised:
                validate_media_file("never-opened.mp4", "ffprobe", cancel_event=cancelled)

        self.assertEqual(raised.exception.code, MediaValidationErrorCode.CANCELLED)
        file_stat.assert_not_called()
        run.assert_not_called()

    def test_cancelled_while_probe_runs_terminates_process_promptly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "video.mp4"
            media.write_bytes(b"content")
            cancelled = threading.Event()

            class RunningProbe:
                returncode = None

                def __init__(self) -> None:
                    self.terminated = False
                    self.killed = False

                def poll(self):
                    return self.returncode

                def communicate(self, timeout=None):
                    if self.terminated or self.killed:
                        return "", ""
                    cancelled.set()
                    raise subprocess.TimeoutExpired("ffprobe", timeout)

                def terminate(self) -> None:
                    self.terminated = True
                    self.returncode = -15

                def kill(self) -> None:
                    self.killed = True
                    self.returncode = -9

            process = RunningProbe()
            with patch(
                "app.core.media_validation.subprocess.Popen",
                return_value=process,
            ):
                with self.assertRaises(MediaValidationError) as raised:
                    validate_media_file(media, "ffprobe", cancel_event=cancelled)

            self.assertEqual(raised.exception.code, MediaValidationErrorCode.CANCELLED)
            self.assertTrue(process.terminated)
            self.assertFalse(process.killed)


if __name__ == "__main__":
    unittest.main()

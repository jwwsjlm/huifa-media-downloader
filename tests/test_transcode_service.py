from __future__ import annotations

import tempfile
import threading
import unittest
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from app.core.media_probe import (
    ChapterInfo,
    MediaStreamInfo,
    TranscodeError,
    VideoStreamInfo,
    probe_video_stream,
    validate_transcode_topology,
    video_stream_info_from_probe_payload,
)
from app.core.transcode_service import (
    PreparedTranscode,
    available_ffmpeg_encoders,
    clear_ffmpeg_encoder_cache,
    compiled_transcode_encoders,
    encoder_candidates,
    ffmpeg_encoder_usable,
    normalize_transcode_codec,
    normalize_transcode_device,
    normalize_transcode_encoder,
    prepare_transcode_media,
    transcode_encoder_codec,
    transcode_encoder_device,
)


class _FakeProcess:
    def __init__(self, command: list[str], return_code: int, lines: list[str]):
        self.command = command
        self._return_code = return_code
        self.stdout = iter(lines)
        self._running = True
        if return_code == 0:
            Path(command[-1]).write_bytes(b"converted-video")

    def wait(self, timeout=None):
        self._running = False
        return self._return_code

    def poll(self):
        return None if self._running else self._return_code

    def terminate(self):
        self._running = False

    def kill(self):
        self._running = False


class TranscodeServiceTests(unittest.TestCase):
    def test_cross_volume_publish_uses_staging_and_can_roll_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "final" / "video.mp4"
            temporary = root / "ssd-temp" / ".video.transcoding.part.mp4"
            source.parent.mkdir()
            temporary.parent.mkdir()
            source.write_bytes(b"original-video")
            temporary.write_bytes(b"converted-video")
            prepared = PreparedTranscode(
                source,
                source,
                temporary,
                "libx264",
                False,
            )

            with patch.object(PreparedTranscode, "_same_volume", return_value=False):
                published = prepared.commit()

            self.assertEqual(source.read_bytes(), b"converted-video")
            self.assertFalse(temporary.exists())
            recovery = published.rollback()
            self.assertEqual(source.read_bytes(), b"original-video")
            self.assertIsNotNone(recovery)
            self.assertEqual(recovery.read_bytes(), b"converted-video")

    def test_cross_volume_cleanup_failure_keeps_publication_rollback_available(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "final" / "video.mp4"
            temporary = root / "ssd-temp" / ".video.transcoding.part.mp4"
            source.parent.mkdir()
            temporary.parent.mkdir()
            source.write_bytes(b"original-video")
            temporary.write_bytes(b"converted-video")
            prepared = PreparedTranscode(
                source,
                source,
                temporary,
                "libx264",
                False,
            )
            path_type = type(temporary)
            real_unlink = path_type.unlink

            def fail_old_temporary_cleanup(path, *args, **kwargs):
                if path == temporary and source.read_bytes() == b"converted-video":
                    raise OSError("temporary file is still busy")
                return real_unlink(path, *args, **kwargs)

            with patch.object(PreparedTranscode, "_same_volume", return_value=False), patch.object(
                path_type,
                "unlink",
                new=fail_old_temporary_cleanup,
            ):
                published = prepared.commit()

            self.assertEqual(source.read_bytes(), b"converted-video")
            self.assertTrue(temporary.exists())
            published.rollback()
            self.assertEqual(source.read_bytes(), b"original-video")

    def test_rollback_restore_failure_puts_converted_file_back_in_place(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "video.mp4"
            temporary = Path(directory) / ".video.transcoding.part.mp4"
            source.write_bytes(b"original-video")
            temporary.write_bytes(b"converted-video")
            published = PreparedTranscode(
                source,
                source,
                temporary,
                "libx264",
                False,
            ).commit()
            backup = published.backup_path
            self.assertIsNotNone(backup)
            path_type = type(source)
            real_replace = path_type.replace

            def fail_backup_restore(path, target):
                if path == backup and Path(target) == source:
                    raise OSError("source path is locked")
                return real_replace(path, target)

            with patch.object(path_type, "replace", new=fail_backup_restore):
                with self.assertRaisesRegex(OSError, "source path is locked"):
                    published.rollback()

            self.assertEqual(source.read_bytes(), b"converted-video")
            self.assertEqual(backup.read_bytes(), b"original-video")
            self.assertEqual(list(source.parent.glob("*.uncommitted*.mp4")), [])

    def test_probe_video_stream_returns_codec_resolution_and_duration(self):
        result = type("Result", (), {
            "returncode": 0,
            "stdout": json.dumps({
                "streams": [
                    {
                        "codec_type": "video", "codec_name": "hevc",
                        "width": 3840, "height": 2160, "avg_frame_rate": "60000/1001",
                    },
                    {"codec_type": "audio", "codec_name": "aac"},
                ],
                "format": {"duration": "12.5"},
            }),
        })()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "video.mkv"
            source.write_bytes(b"media")
            with patch("app.core.media_probe.subprocess.run", return_value=result):
                info = probe_video_stream(source, "ffprobe.exe")

        self.assertEqual((info.codec, info.width, info.height), ("h265", 3840, 2160))
        self.assertEqual(info.duration_seconds, 12.5)
        self.assertAlmostEqual(info.frame_rate, 59.94, places=2)
        self.assertTrue(info.has_audio)

    def test_probe_skips_attached_cover_art_and_selects_real_video(self):
        info = video_stream_info_from_probe_payload({
            "streams": [
                {
                    "codec_type": "video", "codec_name": "mjpeg",
                    "width": 600, "height": 600, "avg_frame_rate": "0/0",
                    "disposition": {"attached_pic": 1},
                },
                {
                    "codec_type": "video", "codec_name": "hevc",
                    "width": 3840, "height": 2160, "avg_frame_rate": "60000/1001",
                    "disposition": {"default": 1},
                },
                {"codec_type": "audio", "codec_name": "aac"},
            ],
            "format": {"duration": "12.5"},
        })

        self.assertEqual((info.codec, info.width, info.height), ("h265", 3840, 2160))
        self.assertEqual(info.primary_video_ordinal, 1)
        self.assertAlmostEqual(info.frame_rate, 59.94, places=2)

    def test_probe_bad_auxiliary_metadata_does_not_reject_valid_video(self):
        info = video_stream_info_from_probe_payload({
            "streams": [
                {
                    "codec_type": "video", "codec_name": "h264",
                    "width": 1920, "height": 1080, "avg_frame_rate": "nan/1",
                },
                {
                    "codec_type": "audio", "codec_name": "aac",
                    "disposition": {"default": "invalid", "forced": "yes"},
                },
            ],
            "format": {"duration": "Infinity"},
            "chapters": [
                {"start_time": "nan", "end_time": "10", "tags": {"title": "Bad"}},
                {"start_time": "2", "end_time": "1", "tags": {"title": "Backwards"}},
                {"start_time": "1", "end_time": "3", "tags": {"title": "Good"}},
            ],
        })

        self.assertEqual(info.duration_seconds, 0.0)
        self.assertEqual(info.frame_rate, 0.0)
        self.assertEqual(info.streams[1].disposition, ("forced",))
        self.assertEqual(info.chapters, (ChapterInfo(1.0, 3.0, "Good"),))

    def test_probe_uses_display_dimensions_for_rotated_phone_video(self):
        info = video_stream_info_from_probe_payload({
            "streams": [{
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1920,
                "height": 1080,
                "avg_frame_rate": "30/1",
                "side_data_list": [{"rotation": -90}],
            }],
            "format": {"duration": "10"},
        })

        self.assertEqual((info.width, info.height), (1080, 1920))

    def test_transcode_maps_selected_real_video_after_attached_cover_art(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "video.mkv"
            source.write_bytes(b"original-video")
            calls: list[list[str]] = []
            source_info = VideoStreamInfo(
                "h264", 1920, 1080, 10.0, 30.0,
                primary_video_ordinal=1,
            )

            def fake_popen(command, **_kwargs):
                calls.append(command)
                return _FakeProcess(command, 0, ["progress=end\n"])

            with patch(
                "app.core.transcode_service.available_ffmpeg_encoders",
                return_value=frozenset({"libx264"}),
            ), patch(
                "app.core.transcode_service.subprocess.Popen",
                side_effect=fake_popen,
            ):
                prepared = prepare_transcode_media(
                    source, "ffmpeg.exe", "h264", "cpu",
                    encoder="libx264", source_info=source_info,
                )

            command = calls[0]
            self.assertEqual(command[command.index("-map") + 1], "0:v:1")
            prepared.discard()

    def test_cover_filter_uses_selected_real_video_after_attached_cover_art(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "video.mkv"
            cover = Path(directory) / "cover.jpg"
            source.write_bytes(b"original-video")
            cover.write_bytes(b"cover")
            calls: list[list[str]] = []
            source_info = VideoStreamInfo(
                "h264", 1080, 1920, 10.0, 30.0,
                primary_video_ordinal=1,
            )

            def fake_popen(command, **_kwargs):
                calls.append(command)
                return _FakeProcess(command, 0, ["progress=end\n"])

            with patch(
                "app.core.transcode_service.available_ffmpeg_encoders",
                return_value=frozenset({"libx264"}),
            ), patch(
                "app.core.transcode_service.subprocess.Popen",
                side_effect=fake_popen,
            ):
                prepared = prepare_transcode_media(
                    source, "ffmpeg.exe", "h264", "cpu",
                    encoder="libx264", source_info=source_info,
                    cover_path=cover, prepend_cover_frames=3,
                )

            graph = calls[0][calls[0].index("-filter_complex") + 1]
            self.assertIn("scale=1080:1920", graph)
            self.assertIn("[1:v:1]fps=30", graph)
            prepared.discard()

    def test_cover_frames_are_inserted_before_video_and_audio_is_delayed(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "video.mp4"
            cover = Path(directory) / "cover.jpg"
            source.write_bytes(b"original-video")
            cover.write_bytes(b"cover")
            calls: list[list[str]] = []

            def fake_popen(command, **_kwargs):
                calls.append(command)
                return _FakeProcess(command, 0, ["out_time_us=1000000\n", "progress=end\n"])

            with patch(
                "app.core.transcode_service.available_ffmpeg_encoders",
                return_value=frozenset({"libx264"}),
            ), patch(
                "app.core.transcode_service.subprocess.Popen",
                side_effect=fake_popen,
            ):
                prepared = prepare_transcode_media(
                    source,
                    "ffmpeg.exe",
                    "original",
                    encoder="original",
                    duration_seconds=10,
                    cover_path=cover,
                    prepend_cover_frames=3,
                    source_codec="h264",
                    source_width=1920,
                    source_height=1080,
                    source_frame_rate=30,
                    source_has_audio=True,
                )
                published = prepared.commit()
                published.finalize()

            self.assertEqual(published.final_path, source)
            self.assertEqual(published.encoder, "libx264")
            command = calls[0]
            self.assertEqual(command[command.index("-framerate") + 1], "30")
            self.assertEqual(command[command.index("-i") + 1], str(cover))
            filter_graph = command[command.index("-filter_complex") + 1]
            self.assertIn("trim=end_frame=3", filter_graph)
            self.assertIn("[coverv][mainv]concat=n=2:v=1:a=0[v]", filter_graph)
            self.assertIn("[1:a:0]adelay=100:all=1[a0]", filter_graph)
            self.assertIn("[a0]", command)

    def test_non_finite_cover_frame_rate_falls_back_to_30_fps(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "video.mp4"
            cover = Path(directory) / "cover.jpg"
            source.write_bytes(b"original-video")
            cover.write_bytes(b"cover")
            calls: list[list[str]] = []
            source_info = VideoStreamInfo(
                "h264",
                1920,
                1080,
                10.0,
                float("nan"),
                True,
                audio_stream_count=1,
                streams=(
                    MediaStreamInfo("video", "h264"),
                    MediaStreamInfo("audio", "aac"),
                ),
            )

            def fake_popen(command, **_kwargs):
                calls.append(command)
                return _FakeProcess(command, 0, ["progress=end\n"])

            with patch(
                "app.core.transcode_service.available_ffmpeg_encoders",
                return_value=frozenset({"libx264"}),
            ), patch(
                "app.core.transcode_service.subprocess.Popen",
                side_effect=fake_popen,
            ):
                prepared = prepare_transcode_media(
                    source,
                    "ffmpeg.exe",
                    "h264",
                    "cpu",
                    encoder="libx264",
                    cover_path=cover,
                    prepend_cover_frames=3,
                    source_info=source_info,
                )

            command = calls[0]
            self.assertEqual(command[command.index("-framerate") + 1], "30")
            graph = command[command.index("-filter_complex") + 1]
            self.assertIn("[1:a:0]adelay=100:all=1[a0]", graph)
            prepared.discard()

    def test_background_transcode_reserves_cpu_for_gui_and_parallel_download(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "video.mp4"
            source.write_bytes(b"original-video")
            calls: list[tuple[list[str], dict]] = []

            def fake_popen(command, **kwargs):
                calls.append((command, kwargs))
                return _FakeProcess(command, 0, ["out_time_us=1000000\n", "progress=end\n"])

            with patch(
                "app.core.transcode_service.available_ffmpeg_encoders",
                return_value=frozenset({"libx265"}),
            ), patch(
                "app.core.transcode_service.os.cpu_count",
                return_value=8,
            ), patch(
                "app.core.transcode_service.subprocess.BELOW_NORMAL_PRIORITY_CLASS",
                0x00004000,
                create=True,
            ), patch(
                "app.core.transcode_service.subprocess.Popen",
                side_effect=fake_popen,
            ):
                prepared = prepare_transcode_media(
                    source,
                    "ffmpeg.exe",
                    "h265",
                    "cpu",
                    encoder="libx265",
                    duration_seconds=10,
                )

            command, kwargs = calls[0]
            self.assertEqual(command[command.index("-threads") + 1], "6")
            self.assertEqual(command[command.index("-x265-params") + 1], "pools=6")
            self.assertTrue(kwargs["creationflags"] & 0x00004000)
            prepared.discard()

    def test_unknown_audio_codec_is_reencoded_for_mp4_instead_of_blind_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "video.mp4"
            source.write_bytes(b"original-video")
            calls: list[list[str]] = []
            source_info = VideoStreamInfo(
                "h264",
                1920,
                1080,
                10.0,
                30.0,
                True,
                audio_stream_count=1,
                streams=(
                    MediaStreamInfo("video", "h264"),
                    MediaStreamInfo("audio", ""),
                ),
            )

            def fake_popen(command, **_kwargs):
                calls.append(command)
                return _FakeProcess(command, 0, ["out_time_us=1000000\n", "progress=end\n"])

            with patch(
                "app.core.transcode_service.available_ffmpeg_encoders",
                return_value=frozenset({"libx264"}),
            ), patch("app.core.transcode_service.subprocess.Popen", side_effect=fake_popen):
                prepared = prepare_transcode_media(
                    source,
                    "ffmpeg.exe",
                    "h264",
                    "cpu",
                    encoder="libx264",
                    source_info=source_info,
                )

            command = calls[0]
            self.assertEqual(command[command.index("-c:a") + 1], "aac")
            self.assertEqual(command[command.index("-b:a") + 1], "192k")
            prepared.discard()

    def test_auto_container_preserves_mkv_and_its_attachment_streams(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "video.mkv"
            source.write_bytes(b"original-video")
            calls: list[list[str]] = []
            source_info = VideoStreamInfo(
                "h264",
                1920,
                1080,
                10.0,
                30.0,
                False,
                attachment_stream_count=1,
                streams=(
                    MediaStreamInfo("video", "h264"),
                    MediaStreamInfo("attachment", "ttf"),
                ),
            )

            def fake_popen(command, **_kwargs):
                calls.append(command)
                return _FakeProcess(command, 0, ["out_time_us=1000000\n", "progress=end\n"])

            with patch(
                "app.core.transcode_service.available_ffmpeg_encoders",
                return_value=frozenset({"libx264"}),
            ), patch("app.core.transcode_service.subprocess.Popen", side_effect=fake_popen):
                prepared = prepare_transcode_media(
                    source,
                    "ffmpeg.exe",
                    "h264",
                    "cpu",
                    encoder="libx264",
                    source_info=source_info,
                    output_container="auto",
                )

            self.assertEqual(prepared.target_path, source)
            self.assertEqual(prepared.temporary_path.suffix, ".mkv")
            self.assertTrue(str(calls[0][-1]).endswith(".mkv"))
            self.assertIn("0:t?", calls[0])
            prepared.discard()

    def test_explicit_mp4_rejects_mkv_attachment_streams_before_starting_ffmpeg(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "video.mkv"
            source.write_bytes(b"original-video")
            source_info = VideoStreamInfo(
                "h264",
                1920,
                1080,
                10.0,
                attachment_stream_count=1,
            )

            with patch("app.core.transcode_service.subprocess.Popen") as popen:
                with self.assertRaisesRegex(TranscodeError, "附件或数据流"):
                    prepare_transcode_media(
                        source,
                        "ffmpeg.exe",
                        "h264",
                        "cpu",
                        encoder="libx264",
                        source_info=source_info,
                        output_container="mp4",
                    )

            popen.assert_not_called()

    def test_failed_chapter_metadata_generation_removes_partial_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "video.mp4"
            cover = root / "cover.jpg"
            source.write_bytes(b"original-video")
            cover.write_bytes(b"cover")
            source_info = VideoStreamInfo(
                "h264",
                1920,
                1080,
                10.0,
                30.0,
                False,
                chapters=(ChapterInfo(1.0, 2.0, "Intro"),),
            )

            def fail_after_write(path, *_args):
                path.write_text("partial metadata", encoding="utf-8")
                raise RuntimeError("metadata failed")

            with patch(
                "app.core.transcode_service.available_ffmpeg_encoders",
                return_value=frozenset({"libx264"}),
            ), patch(
                "app.core.transcode_service._write_shifted_chapters",
                side_effect=fail_after_write,
            ), patch("app.core.transcode_service.subprocess.Popen") as popen:
                with self.assertRaisesRegex(RuntimeError, "metadata failed"):
                    prepare_transcode_media(
                        source,
                        "ffmpeg.exe",
                        "h264",
                        "cpu",
                        encoder="libx264",
                        cover_path=cover,
                        prepend_cover_frames=3,
                        source_info=source_info,
                    )

            popen.assert_not_called()
            self.assertEqual(list(root.glob("*.ffmeta")), [])

    def test_cover_insert_delays_every_audio_and_maps_subtitles_and_shifted_chapters(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "video.mkv"
            cover = Path(directory) / "cover.jpg"
            source.write_bytes(b"original-video")
            cover.write_bytes(b"cover")
            calls: list[list[str]] = []
            chapter_payloads: list[str] = []
            source_info = VideoStreamInfo(
                "h264", 1920, 1080, 10.0, 30.0, True,
                audio_stream_count=2,
                subtitle_stream_count=1,
                streams=(
                    MediaStreamInfo("video", "h264"),
                    MediaStreamInfo("audio", "aac", "eng", ("default",)),
                    MediaStreamInfo("audio", "aac", "jpn"),
                    MediaStreamInfo("subtitle", "subrip", "chi", ("forced",)),
                ),
                chapters=(ChapterInfo(1.0, 3.0, "Intro"),),
            )

            def fake_popen(command, **_kwargs):
                calls.append(command)
                for value in command:
                    if str(value).endswith(".chapters.ffmeta"):
                        chapter_payloads.append(Path(value).read_text(encoding="utf-8"))
                return _FakeProcess(command, 0, ["out_time_us=1000000\n", "progress=end\n"])

            with patch(
                "app.core.transcode_service.available_ffmpeg_encoders",
                return_value=frozenset({"libx264"}),
            ), patch("app.core.transcode_service.subprocess.Popen", side_effect=fake_popen):
                prepared = prepare_transcode_media(
                    source,
                    "ffmpeg.exe",
                    "h264",
                    "cpu",
                    encoder="libx264",
                    duration_seconds=10,
                    cover_path=cover,
                    prepend_cover_frames=3,
                    source_info=source_info,
                )

            command = calls[0]
            graph = command[command.index("-filter_complex") + 1]
            self.assertIn("[1:a:0]adelay=100:all=1[a0]", graph)
            self.assertIn("[1:a:1]adelay=100:all=1[a1]", graph)
            self.assertIn("[a0]", command)
            self.assertIn("[a1]", command)
            self.assertIn("1:s?", command)
            self.assertEqual(command[command.index("-map_chapters") + 1], "2")
            self.assertIn("language=eng", command)
            self.assertIn("language=jpn", command)
            self.assertIn("language=chi", command)
            self.assertIn("title=Opening cover", chapter_payloads[0])
            self.assertIn("START=1100", chapter_payloads[0])
            self.assertIn("END=3100", chapter_payloads[0])
            prepared.discard()

    def test_topology_validation_rejects_silent_stream_loss(self):
        source = VideoStreamInfo(
            "h264", 1920, 1080, 10.0, 30.0, True,
            audio_stream_count=2,
            subtitle_stream_count=1,
            streams=(
                MediaStreamInfo("audio", "aac", "eng"),
                MediaStreamInfo("audio", "aac", "jpn"),
                MediaStreamInfo("subtitle", "subrip", "chi"),
            ),
        )
        output = VideoStreamInfo(
            "h264", 1920, 1080, 10.0, 30.0, True,
            audio_stream_count=1,
            subtitle_stream_count=0,
        )
        with self.assertRaisesRegex(TranscodeError, "音轨数量不完整"):
            validate_transcode_topology(source, output)

    def test_prepared_same_path_transcode_can_roll_back_after_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "video.mp4"
            source.write_bytes(b"original-video")
            with patch(
                "app.core.transcode_service.available_ffmpeg_encoders",
                return_value=frozenset({"libx264"}),
            ), patch(
                "app.core.transcode_service.subprocess.Popen",
                side_effect=lambda command, **_kwargs: _FakeProcess(
                    command, 0, ["out_time_us=1000000\n", "progress=end\n"]
                ),
            ):
                prepared = prepare_transcode_media(
                    source, "ffmpeg.exe", "h264", "cpu", encoder="libx264"
                )
            self.assertEqual(source.read_bytes(), b"original-video")
            published = prepared.commit()
            self.assertEqual(source.read_bytes(), b"converted-video")
            recovery = published.rollback()
            self.assertEqual(source.read_bytes(), b"original-video")
            self.assertIsNotNone(recovery)
            self.assertEqual(recovery.read_bytes(), b"converted-video")

    def test_clear_ffmpeg_encoder_cache_reprobes_replaced_binary(self):
        result = type("Result", (), {
            "stdout": " V..... h264_nvenc NVIDIA NVENC H.264 encoder\n",
            "returncode": 0,
        })()
        clear_ffmpeg_encoder_cache()
        with patch("app.core.transcode_service.subprocess.run", return_value=result) as run:
            self.assertIn("h264_nvenc", available_ffmpeg_encoders("cache-test-ffmpeg.exe"))
            self.assertIn("h264_nvenc", available_ffmpeg_encoders("cache-test-ffmpeg.exe"))
            self.assertEqual(run.call_count, 1)
            clear_ffmpeg_encoder_cache()
            self.assertIn("h264_nvenc", available_ffmpeg_encoders("cache-test-ffmpeg.exe"))
            self.assertEqual(run.call_count, 2)

    def test_normalizers_and_encoder_order(self):
        self.assertEqual(normalize_transcode_codec("H265"), "h265")
        self.assertEqual(normalize_transcode_codec("vp9"), "original")
        self.assertEqual(normalize_transcode_device("GPU"), "gpu")
        self.assertEqual(normalize_transcode_device("unknown"), "auto")
        self.assertEqual(normalize_transcode_encoder("AV1_QSV"), "av1_qsv")
        self.assertEqual(normalize_transcode_encoder("unknown"), "original")
        self.assertEqual(transcode_encoder_codec("hevc_amf"), "h265")
        self.assertEqual(transcode_encoder_device("hevc_amf"), "gpu")
        self.assertEqual(transcode_encoder_codec("libaom-av1"), "av1")
        self.assertEqual(transcode_encoder_device("libaom-av1"), "cpu")
        self.assertEqual(
            encoder_candidates("h264", "auto", {"h264_qsv", "libx264"}),
            ("h264_qsv", "libx264"),
        )
        self.assertEqual(
            encoder_candidates("h265", "cpu", {"hevc_nvenc", "libx265"}),
            ("libx265",),
        )

    def test_compiled_encoder_listing_does_not_open_gpu_encoders(self):
        compiled = frozenset({"libx264", "h264_nvenc", "h264_qsv"})
        with patch(
            "app.core.transcode_service.available_ffmpeg_encoders",
            return_value=compiled,
        ), patch(
            "app.core.transcode_service.ffmpeg_encoder_usable",
        ) as probe:
            detected = compiled_transcode_encoders("ffmpeg.exe")

        self.assertEqual(detected, ("libx264", "h264_nvenc", "h264_qsv"))
        probe.assert_not_called()

    def test_auto_falls_back_to_cpu_and_replaces_only_after_success(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "video.mp4"
            source.write_bytes(b"original-video")
            calls: list[list[str]] = []

            def fake_popen(command, **_kwargs):
                calls.append(command)
                encoder = command[command.index("-c:v") + 1]
                if encoder == "h264_nvenc":
                    return _FakeProcess(command, 1, ["GPU unavailable\n"])
                return _FakeProcess(command, 0, ["out_time_us=5000000\n", "progress=end\n"])

            progress: list[tuple[float, str]] = []
            with patch("app.core.transcode_service.available_ffmpeg_encoders", return_value=frozenset({"h264_nvenc", "libx264"})), patch(
                "app.core.transcode_service.ffmpeg_encoder_usable", return_value=True
            ), patch(
                "app.core.transcode_service.subprocess.Popen", side_effect=fake_popen
            ):
                prepared = prepare_transcode_media(
                    source,
                    "ffmpeg.exe",
                    "h264",
                    "auto",
                    duration_seconds=10,
                    progress=lambda percent, name: progress.append((percent, name)),
                )
                published = prepared.commit()
                published.finalize()

            self.assertEqual(published.final_path, source)
            self.assertEqual(published.encoder, "libx264")
            self.assertEqual(source.read_bytes(), b"converted-video")
            self.assertEqual(
                [call[call.index("-c:v") + 1] for call in calls],
                ["h264_nvenc", "h264_nvenc", "libx264"],
            )
            self.assertIn("-hwaccel", calls[0])
            self.assertEqual(calls[0][calls[0].index("-hwaccel") + 1], "cuda")
            self.assertEqual(
                calls[0][calls[0].index("-hwaccel_output_format") + 1],
                "cuda",
            )
            self.assertNotIn("-hwaccel", calls[1])
            self.assertIn("-pix_fmt", calls[1])
            self.assertIn((50.0, "libx264"), progress)
            self.assertEqual(progress[-1], (100.0, "libx264"))

    def test_preserve_source_keeps_original_when_extension_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "video.webm"
            source.write_bytes(b"original-video")
            with patch(
                "app.core.transcode_service.available_ffmpeg_encoders",
                return_value=frozenset({"libx264"}),
            ), patch(
                "app.core.transcode_service.subprocess.Popen",
                side_effect=lambda command, **_kwargs: _FakeProcess(
                    command,
                    0,
                    ["out_time_us=1000000\n", "progress=end\n"],
                ),
            ):
                prepared = prepare_transcode_media(
                    source,
                    "ffmpeg.exe",
                    "h264",
                    "cpu",
                    encoder="libx264",
                    duration_seconds=1,
                    preserve_source=True,
                )
                published = prepared.commit()
                published.finalize()

            output = source.with_suffix(".mp4")
            self.assertEqual(published.encoder, "libx264")
            self.assertEqual(published.final_path, output)
            self.assertTrue(source.is_file())
            self.assertEqual(source.read_bytes(), b"original-video")
            self.assertEqual(output.read_bytes(), b"converted-video")

    def test_explicit_gpu_without_encoder_is_actionable(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "video.mp4"
            source.write_bytes(b"original")
            with patch("app.core.transcode_service.available_ffmpeg_encoders", return_value=frozenset({"libx264"})):
                with self.assertRaisesRegex(TranscodeError, "GPU"):
                    prepare_transcode_media(source, "ffmpeg.exe", "h264", "gpu")
            self.assertEqual(source.read_bytes(), b"original")

    def test_hardware_probe_encodes_a_generated_frame(self):
        class ProbeResult:
            returncode = 0

        with patch("app.core.transcode_service._resolved_ffmpeg_executable", return_value="ffmpeg.exe"), patch(
            "app.core.transcode_service.subprocess.run", return_value=ProbeResult()
        ) as run:
            ffmpeg_encoder_usable.cache_clear()
            self.assertTrue(ffmpeg_encoder_usable("ffmpeg.exe", "h264_nvenc"))
        command = run.call_args.args[0]
        self.assertIn("lavfi", command)
        self.assertEqual(command[command.index("-c:v") + 1], "h264_nvenc")

    def test_explicit_gpu_tries_all_detected_gpu_encoders_without_cpu_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "video.mp4"
            source.write_bytes(b"original")
            encoders: list[str] = []

            def fake_popen(command, **_kwargs):
                encoder = command[command.index("-c:v") + 1]
                encoders.append(encoder)
                return _FakeProcess(command, 1, [f"{encoder} unavailable\n"])

            with patch(
                "app.core.transcode_service.available_ffmpeg_encoders",
                return_value=frozenset({"h264_nvenc", "h264_qsv", "libx264"}),
            ), patch("app.core.transcode_service.ffmpeg_encoder_usable", return_value=True), patch(
                "app.core.transcode_service.subprocess.Popen", side_effect=fake_popen
            ):
                with self.assertRaisesRegex(TranscodeError, "GPU"):
                    prepare_transcode_media(source, "ffmpeg.exe", "h264", "gpu")
            self.assertEqual(encoders, ["h264_nvenc", "h264_nvenc", "h264_qsv"])
            self.assertEqual(source.read_bytes(), b"original")

    def test_explicit_encoder_is_used_strictly_without_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "video.mp4"
            source.write_bytes(b"original")
            encoders: list[str] = []
            commands: list[list[str]] = []

            def fake_popen(command, **_kwargs):
                commands.append(command)
                encoder = command[command.index("-c:v") + 1]
                encoders.append(encoder)
                return _FakeProcess(command, 1, ["selected encoder failed\n"])

            with patch(
                "app.core.transcode_service.available_ffmpeg_encoders",
                return_value=frozenset({"h264_nvenc", "libx264"}),
            ), patch(
                "app.core.transcode_service.ffmpeg_encoder_usable",
                return_value=True,
            ), patch(
                "app.core.transcode_service.subprocess.Popen",
                side_effect=fake_popen,
            ):
                with self.assertRaisesRegex(TranscodeError, "h264_nvenc"):
                    prepare_transcode_media(
                        source,
                        "ffmpeg.exe",
                        "original",
                        "auto",
                        encoder="h264_nvenc",
                    )

            self.assertEqual(encoders, ["h264_nvenc", "h264_nvenc"])
            self.assertEqual(commands[0][commands[0].index("-hwaccel") + 1], "cuda")
            self.assertEqual(
                commands[0][commands[0].index("-hwaccel_output_format") + 1],
                "cuda",
            )
            self.assertNotIn("-hwaccel", commands[1])
            self.assertIn("-pix_fmt", commands[1])
            self.assertEqual(source.read_bytes(), b"original")

    def test_nvdec_is_not_used_with_cover_filter_or_other_encoders(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cover = root / "cover.jpg"
            cover.write_bytes(b"cover")
            cases = (
                ("h264_nvenc", cover, 3),
                ("h264_qsv", None, 0),
                ("h264_amf", None, 0),
                ("libx264", None, 0),
            )

            for index, (encoder, cover_path, cover_frames) in enumerate(cases):
                with self.subTest(encoder=encoder, cover=bool(cover_path)):
                    source = root / f"video-{index}.mp4"
                    source.write_bytes(b"original")
                    calls: list[list[str]] = []

                    def fake_popen(command, **_kwargs):
                        calls.append(command)
                        return _FakeProcess(command, 0, ["progress=end\n"])

                    with patch(
                        "app.core.transcode_service.available_ffmpeg_encoders",
                        return_value=frozenset({encoder}),
                    ), patch(
                        "app.core.transcode_service.ffmpeg_encoder_usable",
                        return_value=True,
                    ), patch(
                        "app.core.transcode_service.subprocess.Popen",
                        side_effect=fake_popen,
                    ):
                        prepared = prepare_transcode_media(
                            source,
                            "ffmpeg.exe",
                            "h264",
                            "auto",
                            encoder=encoder,
                            cover_path=cover_path or "",
                            prepend_cover_frames=cover_frames,
                            source_width=1920,
                            source_height=1080,
                            source_frame_rate=30,
                        )

                    self.assertNotIn("-hwaccel", calls[0])
                    prepared.discard()

    def test_cancel_before_start_keeps_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "video.mp4"
            source.write_bytes(b"original")
            cancel = threading.Event()
            cancel.set()
            with patch("app.core.transcode_service.available_ffmpeg_encoders", return_value=frozenset({"libx264"})):
                with self.assertRaises(InterruptedError):
                    prepare_transcode_media(
                        source,
                        "ffmpeg.exe",
                        "h264",
                        "cpu",
                        cancel_event=cancel,
                    )
            self.assertEqual(source.read_bytes(), b"original")

    def test_transcode_process_does_not_inherit_standard_input(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "video.mp4"
            source.write_bytes(b"original")
            process_kwargs: list[dict] = []

            def fake_popen(command, **kwargs):
                process_kwargs.append(kwargs)
                return _FakeProcess(command, 0, ["progress=end\n"])

            with patch(
                "app.core.transcode_service.available_ffmpeg_encoders",
                return_value=frozenset({"libx264"}),
            ), patch(
                "app.core.transcode_service.subprocess.Popen",
                side_effect=fake_popen,
            ):
                prepared = prepare_transcode_media(
                    source,
                    "ffmpeg.exe",
                    "h264",
                    "cpu",
                    encoder="libx264",
                )

            self.assertIs(process_kwargs[0]["stdin"], subprocess.DEVNULL)
            prepared.discard()

    def test_cancel_after_process_start_terminates_ffmpeg_once(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "video.mp4"
            source.write_bytes(b"original")

            class ToggleCancel:
                def __init__(self):
                    self.checks = 0

                def is_set(self):
                    self.checks += 1
                    return self.checks >= 2

            process = _FakeProcess(["fake-output"], 1, [])
            with patch(
                "app.core.transcode_service.available_ffmpeg_encoders",
                return_value=frozenset({"libx264"}),
            ), patch(
                "app.core.transcode_service.subprocess.Popen",
                return_value=process,
            ), patch("app.core.transcode_service._terminate_process") as terminate:
                with self.assertRaises(InterruptedError):
                    prepare_transcode_media(
                        source,
                        "ffmpeg.exe",
                        "h264",
                        "cpu",
                        encoder="libx264",
                        cancel_event=ToggleCancel(),
                    )

            terminate.assert_called_once_with(process)
            self.assertEqual(source.read_bytes(), b"original")

    def test_progress_reader_start_failure_terminates_ffmpeg(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "video.mp4"
            source.write_bytes(b"original")
            process = _FakeProcess(["fake-output"], 1, [])

            with patch(
                "app.core.transcode_service.available_ffmpeg_encoders",
                return_value=frozenset({"libx264"}),
            ), patch(
                "app.core.transcode_service.subprocess.Popen",
                return_value=process,
            ), patch(
                "app.core.transcode_service.threading.Thread.start",
                side_effect=RuntimeError("reader thread unavailable"),
            ), patch(
                "app.core.transcode_service._terminate_process"
            ) as terminate:
                with self.assertRaisesRegex(RuntimeError, "reader thread unavailable"):
                    prepare_transcode_media(
                        source,
                        "ffmpeg.exe",
                        "h264",
                        "cpu",
                        encoder="libx264",
                    )

            terminate.assert_called_once_with(process)
            self.assertEqual(source.read_bytes(), b"original")

    def test_cancel_still_works_after_ffmpeg_closes_progress_pipe_early(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "video.mp4"
            source.write_bytes(b"original")

            class DelayedCancel:
                def __init__(self):
                    self.checks = 0

                def is_set(self):
                    self.checks += 1
                    return self.checks >= 4

            class HangingAfterOutputProcess(_FakeProcess):
                def __init__(self, command):
                    super().__init__(command, 1, [])

                def wait(self, timeout=None):
                    if timeout is not None:
                        raise subprocess.TimeoutExpired(self.command, timeout)
                    return super().wait(timeout)

            process = HangingAfterOutputProcess(["fake-output"])
            with patch(
                "app.core.transcode_service.available_ffmpeg_encoders",
                return_value=frozenset({"libx264"}),
            ), patch(
                "app.core.transcode_service.subprocess.Popen",
                return_value=process,
            ), patch("app.core.transcode_service._terminate_process") as terminate:
                with self.assertRaises(InterruptedError):
                    prepare_transcode_media(
                        source,
                        "ffmpeg.exe",
                        "h264",
                        "cpu",
                        encoder="libx264",
                        cancel_event=DelayedCancel(),
                    )

            terminate.assert_called_once_with(process)
            self.assertEqual(source.read_bytes(), b"original")


if __name__ == "__main__":
    unittest.main()

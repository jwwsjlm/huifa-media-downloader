from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from collections import UserDict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QThread
from PySide6.QtWidgets import QApplication

from app.core.download_service import (
    CompletedMediaTranscodeWorker,
    DownloadService,
    DownloadTask,
    DownloadWorker,
    cleanup_processing_workspace,
    completed_task_file_manifest,
    ffprobe_runtime_path,
    processing_temp_workspace,
    processing_temp_workspace_path,
    task_download_artifact_paths,
    validate_filename_template,
)
from app.core.download_options import DownloadOptions
from app.core.disk_capacity import DiskCapacityError, DiskCapacityErrorCode
from app.core.external_ytdlp import ExternalYtdlpError
from app.core.log_service import DownloadLogService
from app.core.media_validation import (
    MediaValidationError,
    MediaValidationErrorCode,
    MediaValidationResult,
)
from app.core.qt_lifecycle import delete_unstarted_worker
from app.core.media_probe import TranscodeError, VideoStreamInfo
from app.core.transcode_service import PublishedTranscode
from app.storage.database import Database
from app.storage.models import MediaItem


def validation_result(path: Path, *, audio: bool = True) -> MediaValidationResult:
    streams = [{
        "index": 0,
        "codec_type": "video",
        "codec_name": "h264",
        "width": 1920,
        "height": 1080,
        "avg_frame_rate": "30/1",
        "tags": {},
        "disposition": {},
    }]
    if audio:
        streams.append({
            "index": 1,
            "codec_type": "audio",
            "codec_name": "aac",
            "tags": {},
            "disposition": {},
        })
    return MediaValidationResult(
        file_path=str(path),
        size_bytes=path.stat().st_size,
        duration_seconds=12.5,
        container="mov,mp4,m4a,3gp,3g2,mj2",
        container_description="QuickTime / MOV",
        stream_count=2 if audio else 1,
        video_stream_count=1,
        audio_stream_count=1 if audio else 0,
        subtitle_stream_count=0,
        other_stream_count=0,
        probe_payload={
            "format": {"duration": "12.5"},
            "streams": streams,
            "chapters": [],
        },
    )


class FakePublishedTranscode:
    def __init__(self, path: Path, encoder: str):
        self.final_path = path
        self.encoder = encoder

    def finalize(self) -> None:
        pass

    def rollback(self) -> None:
        pass


class FakePreparedTranscode:
    def __init__(self, path: Path, encoder: str):
        self.temporary_path = path
        self.target_path = path
        self.encoder = encoder

    def commit(self):
        return FakePublishedTranscode(self.target_path, self.encoder)

    def discard(self) -> None:
        pass


class FakeYoutubeDL:
    payload: dict = {}

    def __init__(self, options: dict):
        self.options = options

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def extract_info(self, _url: str, *, download: bool):
        if not download:
            raise AssertionError("single/playlist worker test must not perform a preliminary probe")
        return self.payload

    def prepare_filename(self, entry: dict) -> str:
        return str(entry.get("_filename") or "")


class DownloadCompletionFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.qt_app = QApplication.instance() or QApplication([])

    def test_custom_processing_root_uses_relative_ytdlp_template_and_isolated_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "mechanical"
            temp_root = root / "ssd"
            output.mkdir()
            worker = DownloadWorker(
                "task-123",
                "https://example.test/video",
                str(output),
                SimpleNamespace(),
                options_json={"processing_temp_dir": str(temp_root)},
            )
            workspace = processing_temp_workspace(temp_root, worker.task_id, "download")
            worker._processing_workspace = workspace

            template, _limit = worker._download_output_template("%(title)s [%(id)s].%(ext)s")

            self.assertFalse(template.is_absolute())
            self.assertTrue(
                os.path.samefile(
                    workspace,
                    temp_root / "huifa-processing" / "task-123" / "download",
                ),
            )
            marker = workspace / "fragment.part"
            marker.write_bytes(b"partial")
            cleanup_processing_workspace(workspace)
            self.assertFalse(workspace.exists())
            self.assertTrue(temp_root.exists())

    def test_processing_workspace_components_do_not_collide_on_windows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            slash = processing_temp_workspace_path(root, "a/b", "download")
            question = processing_temp_workspace_path(root, "a?b", "download")
            uppercase = processing_temp_workspace_path(root, "Task", "download")
            lowercase = processing_temp_workspace_path(root, "task", "download")
            reserved = processing_temp_workspace_path(root, "CON", "download")
            relative = processing_temp_workspace_path(root, "..", "download")

            self.assertNotEqual(slash, question)
            self.assertNotEqual(uppercase, lowercase)
            self.assertNotEqual(reserved.parent.name.upper(), "CON")
            self.assertNotIn(relative.parent.name, {".", ".."})

    def test_processing_cleanup_rejects_root_and_reparse_ancestor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = processing_temp_workspace(root, "task-safe", "download")
            self.assertIsNotNone(workspace)
            assert workspace is not None
            marker = workspace / "keep.part"
            marker.write_bytes(b"partial")
            app_root = root / "huifa-processing"

            self.assertFalse(cleanup_processing_workspace(app_root))
            self.assertFalse(cleanup_processing_workspace(app_root / "task-safe"))
            self.assertTrue(marker.exists())

            task_root = app_root / "task-safe"
            with patch(
                "app.core.download_service._is_reparse_point",
                side_effect=lambda path: os.path.samefile(path, task_root),
            ):
                self.assertFalse(cleanup_processing_workspace(workspace))
            self.assertTrue(marker.exists())

    def test_processing_cleanup_unlinks_child_symlink_without_touching_target(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as target_dir:
            root = Path(directory)
            target = Path(target_dir)
            protected = target / "protected.txt"
            protected.write_text("keep", encoding="utf-8")
            workspace = processing_temp_workspace(root, "task-link", "download")
            self.assertIsNotNone(workspace)
            assert workspace is not None
            link = workspace / "outside-link"
            try:
                link.symlink_to(target, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlinks are unavailable: {exc}")

            self.assertTrue(cleanup_processing_workspace(workspace))
            self.assertTrue(protected.exists())
            self.assertFalse(link.exists())

    def test_missing_cookie_file_cleans_created_processing_workspace_and_finishes_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "downloads"
            temp_root = root / "processing"
            worker = DownloadWorker(
                "missing-cookie",
                "https://example.test/video",
                str(output),
                SimpleNamespace(),
                cookie_source="file",
                cookie_file=str(root / "missing-cookies.txt"),
                ytdlp_core_mode="external",
                options_json={"processing_temp_dir": str(temp_root)},
            )
            worker.logs = DownloadLogService(root / "logs")
            failures: list[str] = []
            finished: list[bool] = []
            worker.failed.connect(lambda _task_id, error: failures.append(error))
            worker.finished.connect(lambda: finished.append(True))

            with patch(
                "app.core.download_service.ytdlp_runtime_path",
                return_value="yt-dlp.exe",
            ), patch(
                "app.core.download_service.cached_external_ytdlp_version",
                return_value="2026.08.26",
            ):
                worker.run()

            workspace = processing_temp_workspace_path(
                temp_root,
                worker.task_id,
                "download",
            )
            self.assertEqual(len(failures), 1)
            self.assertIn("Cookie 文件不存在", failures[0])
            self.assertEqual(finished, [True])
            self.assertEqual(worker._stage, "failed")
            self.assertIsNotNone(workspace)
            assert workspace is not None
            self.assertFalse(workspace.exists())

    def test_external_progress_cancel_does_not_require_bundled_ytdlp_module(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = DownloadWorker(
                "external-cancel",
                "https://example.test/video",
                directory,
                SimpleNamespace(),
                ytdlp_core_mode="external",
            )
            worker.logs = DownloadLogService(Path(directory) / "logs")
            worker._cancel.set()

            with patch("app.core.download_service.yt_dlp", None):
                with self.assertRaises(InterruptedError):
                    worker._progress_hook({"status": "downloading"})

    def test_worker_progress_uses_valid_estimate_when_primary_bytes_are_invalid(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = DownloadWorker(
                "invalid-worker-progress",
                "https://example.test/video",
                directory,
                SimpleNamespace(),
            )
            worker.logs = DownloadLogService(Path(directory) / "logs")
            worker._started_at = worker._stage_started_at = 1.0
            events: list[dict] = []
            worker.progress.connect(
                lambda _task_id, payload: events.append(dict(payload))
            )

            with patch("app.core.download_service.time.monotonic", return_value=2.0):
                worker._progress_hook({
                    "status": "downloading",
                    "total_bytes": float("nan"),
                    "total_bytes_estimate": "1000",
                    "downloaded_bytes": "250",
                    "info_dict": {
                        "format_id": "video",
                        "vcodec": "h264",
                        "acodec": "none",
                    },
                })

            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["stream_kind"], "video")
            self.assertEqual(events[0]["stream_progress"], 25.0)

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db = Database(self.root / "app.db")

    def tearDown(self) -> None:
        self.db.close()
        self.temp_dir.cleanup()

    def test_download_runtime_reuses_service_log_buffer(self) -> None:
        service = DownloadService(self.db)
        task = DownloadTask(
            "shared-log-service",
            "https://example.test/shared-log-service",
            str(self.root),
        )
        runtime = service._create_download_runtime(task)
        try:
            self.assertIs(runtime.worker.logs, service.logs)
        finally:
            delete_unstarted_worker(runtime.worker, runtime.thread)
            service.shutdown(timeout_ms=0)

    def test_worker_run_isolates_cleanup_failures_and_always_finishes(self) -> None:
        class FailingCookieSource:
            options = {"cookiefile": "temporary.txt"}
            normalized_source = "embedded"
            temporary_file = Path("temporary.txt")

            def __init__(self) -> None:
                self.cleanup_count = 0

            def cleanup(self) -> bool:
                self.cleanup_count += 1
                raise RuntimeError("cookie cleanup failed")

        class FailingPreparedTranscode:
            def __init__(self) -> None:
                self.discard_count = 0

            def discard(self) -> None:
                self.discard_count += 1
                raise RuntimeError("transcode cleanup failed")

        class FailingDiskLease:
            def __init__(self) -> None:
                self.release_count = 0

            def release_all(self) -> int:
                self.release_count += 1
                raise RuntimeError("capacity cleanup failed")

        cookie_source = FailingCookieSource()
        prepared = FailingPreparedTranscode()
        lease = FailingDiskLease()
        worker = DownloadWorker(
            "run-cleanup-isolation",
            "https://example.test/watch/cleanup",
            str(self.root),
            self.db,
            disk_lease=lease,
        )
        worker.logs = DownloadLogService(self.root / "logs")
        worker._pending_transcodes.append(prepared)
        worker._processing_workspace = self.root / "processing-workspace"
        failed: list[str] = []
        finished: list[bool] = []
        worker.failed.connect(lambda _task_id, message: failed.append(message))
        worker.finished.connect(lambda: finished.append(True))

        with patch.object(worker, "_resolve_download_core", return_value=""), patch.object(
            worker,
            "_prepare_run_environment",
        ), patch.object(
            worker,
            "_build_ytdlp_options",
            return_value={"format": "best"},
        ), patch.object(
            worker,
            "_configure_cookie_options",
            return_value=cookie_source,
        ), patch.object(
            worker,
            "_run_download_flow",
            side_effect=RuntimeError(),
        ), patch(
            "app.core.download_service.cleanup_processing_workspace",
            side_effect=RuntimeError("workspace cleanup failed"),
        ), patch.object(
            worker.logs,
            "flush",
            side_effect=RuntimeError("log flush failed"),
        ):
            worker.run()

        self.assertEqual(failed, ["RuntimeError"])
        self.assertEqual(finished, [True])
        self.assertEqual(cookie_source.cleanup_count, 1)
        self.assertEqual(prepared.discard_count, 1)
        self.assertEqual(lease.release_count, 1)
        self.assertEqual(worker._pending_transcodes, [])
        self.assertEqual(worker._stage, "failed")

    def test_cookie_setup_preserves_original_error_when_cleanup_fails(self) -> None:
        class FailingOptions(dict):
            def update(self, *_args, **_kwargs) -> None:
                raise RuntimeError("options update failed")

        class FailingCookieSource:
            options = {"cookiefile": "temporary.txt"}
            normalized_source = "embedded"
            temporary_file = Path("temporary.txt")

            def __init__(self) -> None:
                self.cleanup_count = 0

            def cleanup(self) -> bool:
                self.cleanup_count += 1
                raise RuntimeError("cookie cleanup failed")

        materialized = FailingCookieSource()
        worker = DownloadWorker(
            "cookie-half-initialized",
            "https://example.test/watch/cookie",
            str(self.root),
            self.db,
        )
        worker.logs = DownloadLogService(self.root / "logs")

        with patch(
            "app.core.download_service.materialize_cookie_source",
            return_value=materialized,
        ), self.assertRaisesRegex(Exception, "options update failed"):
            worker._configure_cookie_options(FailingOptions())

        self.assertEqual(materialized.cleanup_count, 1)

    def test_worker_core_selection_uses_shared_auto_fallback_policy(self) -> None:
        worker = DownloadWorker(
            "core-auto-fallback",
            "https://example.test/watch/core",
            str(self.root),
            self.db,
            ytdlp_core_mode="auto",
        )
        worker.logs = DownloadLogService(self.root / "logs")

        with patch(
            "app.core.download_service.ytdlp_runtime_path",
            return_value="tools/yt-dlp.exe",
        ), patch(
            "app.core.download_service.cached_external_ytdlp_version",
            return_value="",
        ), patch(
            "app.core.download_service.yt_dlp",
            SimpleNamespace(),
        ):
            selected = worker._resolve_download_core()

        self.assertEqual(selected, "")

    def test_worker_external_core_failure_keeps_reason_in_setup_error(self) -> None:
        worker = DownloadWorker(
            "core-external-failure",
            "https://example.test/watch/core",
            str(self.root),
            self.db,
            ytdlp_core_mode="external",
        )
        worker.logs = DownloadLogService(self.root / "logs")

        with patch(
            "app.core.download_service.ytdlp_runtime_path",
            return_value="tools/yt-dlp.exe",
        ), patch(
            "app.core.download_service.cached_external_ytdlp_version",
            return_value="",
        ), self.assertRaises(Exception) as raised:
            worker._resolve_download_core()

        self.assertEqual(raised.exception.details["reason"], "external_probe_failed")

    def test_external_flow_reuses_first_preview_for_capacity_when_format_is_unchanged(self) -> None:
        worker = DownloadWorker(
            "external-preview-reuse",
            "https://example.test/watch/external-preview-reuse",
            str(self.root),
            self.db,
            playlist_mode="auto",
            disk_lease=object(),
        )
        preview = {"id": "preview", "title": "Preview", "marker": "initial"}
        calls: list[bool] = []
        reserved: list[dict] = []

        def run_external(*_args, download: bool, **_kwargs):
            calls.append(download)
            return preview

        with patch(
            "app.core.download_service.run_external_ytdlp",
            side_effect=run_external,
        ), patch.object(worker, "_capacity_match_filter") as reserve, patch.object(
            worker,
            "_complete_download_info",
        ), patch.object(worker, "_log"):
            reserve.side_effect = lambda _resolver, entry: reserved.append(dict(entry))
            worker._run_external_flow("yt-dlp.exe", {"format": "best"}, lambda _payload: None)

        self.assertEqual(calls, [False, True])
        self.assertEqual([item["marker"] for item in reserved], ["initial"])

    def test_external_flow_reprobes_capacity_after_manual_format_changes(self) -> None:
        worker = DownloadWorker(
            "external-preview-format-change",
            "https://example.test/watch/external-preview-format-change",
            str(self.root),
            self.db,
            disk_lease=object(),
        )
        stale_preview = {"id": "stale", "marker": "before-selection"}
        refreshed_preview = {"id": "fresh", "marker": "after-selection"}
        calls: list[bool] = []
        reserved: list[dict] = []

        def prepare(options, **_kwargs):
            options["format"] = "selected-format-id"
            return stale_preview

        def run_external(*_args, download: bool, **_kwargs):
            calls.append(download)
            return refreshed_preview

        with patch.object(
            worker,
            "_prepare_preview_and_selection",
            side_effect=prepare,
        ), patch(
            "app.core.download_service.run_external_ytdlp",
            side_effect=run_external,
        ), patch.object(worker, "_capacity_match_filter") as reserve, patch.object(
            worker,
            "_complete_download_info",
        ), patch.object(worker, "_log"):
            reserve.side_effect = lambda _resolver, entry: reserved.append(dict(entry))
            worker._run_external_flow("yt-dlp.exe", {"format": "best"}, lambda _payload: None)

        self.assertEqual(calls, [False, True])
        self.assertEqual([item["marker"] for item in reserved], ["after-selection"])

    def test_media_catalog_does_not_store_unverifiable_video_hashes(self) -> None:
        columns = {
            str(row[1])
            for row in self.db.conn.execute("PRAGMA table_info(media_items)").fetchall()
        }
        self.assertNotIn("sha256", columns)
        self.assertFalse(hasattr(MediaItem(), "sha256"))

    @staticmethod
    def _fake_ytdlp():
        return SimpleNamespace(
            YoutubeDL=FakeYoutubeDL,
            utils=SimpleNamespace(DownloadError=RuntimeError),
        )

    def _worker(self, task: DownloadTask) -> DownloadWorker:
        self.db.upsert_download_task(task)
        worker = DownloadWorker(
            task.id,
            task.url,
            task.output_dir,
            self.db,
            ytdlp_core_mode="builtin",
            quality=task.quality,
            playlist_mode=task.playlist_mode,
            transcode_codec=task.transcode_codec,
            transcode_device=task.transcode_device,
            transcode_encoder=task.transcode_encoder,
            options_json=task.options_json,
        )
        worker.logs = DownloadLogService(self.root / "logs")
        return worker

    def test_completed_entry_prefers_current_exact_output_over_stale_mp4_sibling(self) -> None:
        current = self.root / "same-title.webm"
        stale = self.root / "same-title.mp4"
        current.write_bytes(b"current-download")
        stale.write_bytes(b"stale-older-download")
        task = DownloadTask(
            "prefer-exact-completed-output",
            "https://example.test/prefer-exact-completed-output",
            str(self.root),
            status="downloading",
        )
        worker = self._worker(task)
        entry = {
            "title": "Current download",
            "_filename": str(current),
            "requested_downloads": [{
                "filepath": str(current),
                "vcodec": "vp9",
                "acodec": "opus",
            }],
        }

        with patch(
            "app.core.download_service.validate_media_file",
            return_value=validation_result(current),
        ) as validate:
            item, prepared = worker._prepare_completed_entry(
                entry,
                lambda _entry: str(current),
                "C:/tools/ffprobe.exe",
                [],
                release_capacity=False,
            )

        self.assertIsNone(prepared)
        self.assertEqual(item.video_path, str(current))
        self.assertEqual(validate.call_args.args[0], current)

    def test_completed_entry_uses_configured_container_when_prepared_path_was_remuxed(self) -> None:
        prepared_name = self.root / "remuxed.webm"
        final_media = prepared_name.with_suffix(".mkv")
        final_media.write_bytes(b"remuxed-download")
        task = DownloadTask(
            "resolve-remuxed-completed-output",
            "https://example.test/resolve-remuxed-completed-output",
            str(self.root),
            status="downloading",
            options_json={"container": "mkv"},
        )
        worker = self._worker(task)
        entry = {
            "title": "Remuxed download",
            "_filename": str(prepared_name),
            "requested_downloads": [{
                "filepath": str(prepared_name),
                "vcodec": "h264",
                "acodec": "aac",
            }],
        }

        with patch(
            "app.core.download_service.validate_media_file",
            return_value=validation_result(final_media),
        ) as validate:
            item, prepared = worker._prepare_completed_entry(
                entry,
                lambda _entry: str(prepared_name),
                "C:/tools/ffprobe.exe",
                [],
                release_capacity=False,
            )

        self.assertIsNone(prepared)
        self.assertEqual(item.video_path, str(final_media))
        self.assertEqual(validate.call_args.args[0], final_media)

    def test_single_entry_multi_video_is_published_as_collection(self) -> None:
        worker = DownloadWorker(
            "multi-video-preview",
            "https://example.test/multi",
            str(self.root),
            self.db,
            playlist_mode="auto",
        )
        worker.logs = DownloadLogService(self.root / "logs")
        published: list[dict] = []
        worker.playlist_info.connect(
            lambda _task_id, payload: published.append(payload)
        )

        worker._publish_playlist_preview({
            "_type": "multi_video",
            "title": "One-part multi video",
            "entries": [{"id": "part-one"}],
        })

        self.assertEqual(len(published), 1)
        self.assertTrue(published[0]["is_playlist"])
        self.assertEqual(published[0]["count"], 1)

    def test_shared_preview_pipeline_uses_first_mapping_entry_for_manual_formats(self) -> None:
        worker = DownloadWorker(
            "manual-preview",
            "https://example.test/manual",
            str(self.root),
            self.db,
            playlist_mode="auto",
            options_json={"content_mode": "manual"},
        )
        worker.logs = DownloadLogService(self.root / "logs")
        emitted: list[dict] = []
        probe_operations: list[str] = []

        def select_format(_task_id: str, payload: dict) -> None:
            emitted.append(payload)
            worker.set_format_selector("video-1+bestaudio/best", content_mode="video")

        worker.formats_ready.connect(select_format)
        valid_entry = UserDict({
            "id": "video-one",
            "title": "Video one",
            "formats": [
                "invalid-format-entry",
                UserDict({
                    "format_id": "video-1",
                    "height": 1080,
                    "fps": 30,
                    "vcodec": "avc1.640028",
                    "acodec": "none",
                    "ext": "mp4",
                }),
            ],
        })

        def probe(operation: str):
            probe_operations.append(operation)
            return {
                "_type": "multi_video",
                "title": "Mixed entries",
                "entries": [None, "invalid-entry", valid_entry],
            }

        ydl_opts: dict = {}
        preview = worker._prepare_preview_and_selection(
            ydl_opts,
            initial_preview=None,
            probe=probe,
            parse_log_message="test preview",
        )

        self.assertEqual(probe_operations, ["视频信息解析"])
        self.assertEqual(preview["title"], "Mixed entries")
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0]["choices"][0]["height"], 1080)
        self.assertEqual(ydl_opts["format"], "video-1+bestaudio/best")

    def test_shared_preview_pipeline_preserves_external_missing_selection_error(self) -> None:
        worker = DownloadWorker(
            "external-manual-preview",
            "https://example.test/manual",
            str(self.root),
            self.db,
            playlist_mode="single",
            options_json={"content_mode": "manual"},
        )
        worker.logs = DownloadLogService(self.root / "logs")
        worker._format_event.set()

        with self.assertRaisesRegex(ExternalYtdlpError, "未选择视频分辨率"):
            worker._prepare_preview_and_selection(
                {},
                initial_preview={"id": "video", "formats": []},
                probe=lambda _operation: self.fail("initial preview must be reused"),
                parse_log_message="unused",
                missing_selection_error=ExternalYtdlpError,
            )

    def test_single_video_is_validated_before_atomic_completion(self) -> None:
        media_path = self.root / "single.mp4"
        media_path.write_bytes(b"verified-media")
        FakeYoutubeDL.payload = {
            "id": "single",
            "title": "单视频",
            "webpage_url": "https://example.test/watch/single",
            "_filename": str(media_path),
            "requested_downloads": [
                {"filepath": str(media_path), "vcodec": "h264", "acodec": "aac"}
            ],
        }
        task = DownloadTask(
            "single-task",
            "https://example.test/watch/single",
            str(self.root),
            playlist_mode="single",
            status="downloading",
        )
        worker = self._worker(task)
        completed: list[MediaItem] = []
        failures: list[str] = []
        worker.completed.connect(lambda _task_id, item: completed.append(item))
        worker.failed.connect(lambda _task_id, error: failures.append(error))

        with patch("app.core.download_service.yt_dlp", self._fake_ytdlp()), patch(
            "app.core.download_service.deno_runtime_path", return_value=""
        ), patch("app.core.download_service.ffmpeg_runtime_path", return_value="C:/tools/ffmpeg.exe"), patch(
            "app.core.download_service.ffprobe_runtime_path", return_value="C:/tools/ffprobe.exe"
        ), patch(
            "app.core.download_service.validate_media_file",
            return_value=validation_result(media_path),
        ) as validate:
            worker.run()

        self.assertEqual(failures, [])
        self.assertEqual(len(completed), 1)
        self.assertIsNotNone(completed[0].id)
        row = self.db.list_download_tasks()[0]
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["progress"], 100.0)
        self.assertEqual(row["media_path"], str(media_path))
        self.assertEqual(len(self.db.list_media()), 1)
        self.assertFalse(hasattr(completed[0], "sha256"))
        self.assertEqual(validate.call_args.args[:2], (media_path, "C:/tools/ffprobe.exe"))
        self.assertTrue(validate.call_args.kwargs["require_video"])
        self.assertTrue(validate.call_args.kwargs["require_audio"])
        self.assertIs(validate.call_args.kwargs["cancel_event"], worker._cancel)

    def test_multi_video_mapping_entries_complete_as_individual_media(self) -> None:
        media_path = self.root / "multi-part.mp4"
        media_path.write_bytes(b"verified-multi-video")
        FakeYoutubeDL.payload = {
            "_type": "multi_video",
            "id": "multi-root",
            "title": "Multi video",
            "entries": [
                "invalid-entry",
                UserDict({
                    "id": "multi-part",
                    "title": "Multi part",
                    "webpage_url": "https://example.test/watch/multi-part",
                    "_filename": str(media_path),
                    "requested_downloads": [UserDict({
                        "filepath": str(media_path),
                        "vcodec": "h264",
                        "acodec": "aac",
                    })],
                }),
            ],
        }
        task = DownloadTask(
            "multi-video-task",
            "https://example.test/watch/multi",
            str(self.root),
            playlist_mode="single",
            status="downloading",
        )
        worker = self._worker(task)
        completed: list[MediaItem] = []
        failures: list[str] = []
        worker.completed.connect(lambda _task_id, item: completed.append(item))
        worker.failed.connect(lambda _task_id, error: failures.append(error))

        with patch("app.core.download_service.yt_dlp", self._fake_ytdlp()), patch(
            "app.core.download_service.deno_runtime_path", return_value=""
        ), patch(
            "app.core.download_service.ffmpeg_runtime_path",
            return_value="C:/tools/ffmpeg.exe",
        ), patch(
            "app.core.download_service.ffprobe_runtime_path",
            return_value="C:/tools/ffprobe.exe",
        ), patch(
            "app.core.download_service.validate_media_file",
            return_value=validation_result(media_path),
        ):
            worker.run()

        self.assertEqual(failures, [])
        self.assertEqual(
            [item.source_url for item in completed],
            ["https://example.test/watch/multi-part"],
        )
        self.assertEqual(self.db.list_download_tasks()[0]["status"], "completed")
        self.assertEqual(len(self.db.list_media()), 1)

    def test_one_invalid_playlist_entry_leaves_no_partial_completed_catalog(self) -> None:
        first_path = self.root / "first.mp4"
        second_path = self.root / "second.mp4"
        first_path.write_bytes(b"first-media")
        second_path.write_bytes(b"damaged-media")
        FakeYoutubeDL.payload = {
            "_type": "playlist",
            "entries": [
                {
                    "id": "first",
                    "title": "第一条",
                    "_filename": str(first_path),
                    "requested_downloads": [{"filepath": str(first_path), "vcodec": "h264", "acodec": "aac"}],
                },
                {
                    "id": "second",
                    "title": "第二条",
                    "_filename": str(second_path),
                    "requested_downloads": [{"filepath": str(second_path), "vcodec": "h264", "acodec": "aac"}],
                },
            ],
        }
        task = DownloadTask(
            "playlist-task",
            "https://example.test/playlist",
            str(self.root),
            playlist_mode="playlist",
            status="downloading",
        )
        worker = self._worker(task)
        failures: list[str] = []
        completed: list[MediaItem] = []
        worker.failed.connect(lambda _task_id, error: failures.append(error))
        worker.completed.connect(lambda _task_id, item: completed.append(item))
        invalid = MediaValidationError(
            MediaValidationErrorCode.FFPROBE_FAILED,
            "下载成品无法被媒体工具识别，文件可能不完整或已损坏。",
            "请重新下载该任务。",
        )

        with patch("app.core.download_service.yt_dlp", self._fake_ytdlp()), patch(
            "app.core.download_service.deno_runtime_path", return_value=""
        ), patch("app.core.download_service.ffmpeg_runtime_path", return_value="C:/tools/ffmpeg.exe"), patch(
            "app.core.download_service.ffprobe_runtime_path", return_value="C:/tools/ffprobe.exe"
        ), patch(
            "app.core.download_service.validate_media_file",
            side_effect=[validation_result(first_path), invalid],
        ), patch.object(
            self.db,
            "complete_download_task_batch",
            wraps=self.db.complete_download_task_batch,
        ) as complete_batch:
            worker.run()

        self.assertEqual(completed, [])
        self.assertEqual(len(failures), 1)
        self.assertIn("重新下载", failures[0])
        complete_batch.assert_not_called()
        self.assertEqual(self.db.list_media(), [])
        self.assertEqual(self.db.list_download_tasks()[0]["status"], "downloading")

    def test_requested_transcode_runs_before_validation_and_reports_stage_progress(self) -> None:
        media_path = self.root / "convert.mp4"
        media_path.write_bytes(b"downloaded-media")
        FakeYoutubeDL.payload = {
            "id": "convert",
            "title": "待转换视频",
            "duration": 20,
            "_filename": str(media_path),
            "requested_downloads": [{"filepath": str(media_path), "vcodec": "vp9", "acodec": "opus"}],
        }
        task = DownloadTask(
            "convert-task",
            "https://example.test/watch/convert",
            str(self.root),
            playlist_mode="single",
            status="downloading",
            transcode_encoder="h264_nvenc",
        )
        worker = self._worker(task)
        stages: list[dict] = []
        worker.progress.connect(lambda _task_id, payload: stages.append(dict(payload)))

        def fake_transcode(path, _ffmpeg, codec, device, **kwargs):
            self.assertEqual(Path(path), media_path)
            self.assertEqual((codec, device), ("h264", "gpu"))
            self.assertEqual(kwargs["encoder"], "h264_nvenc")
            self.assertEqual(kwargs["duration_seconds"], 20.0)
            kwargs["progress"](42.0, "h264_nvenc")
            kwargs["progress"](100.0, "h264_nvenc")
            return FakePreparedTranscode(media_path, "h264_nvenc")

        with patch("app.core.download_service.yt_dlp", self._fake_ytdlp()), patch(
            "app.core.download_service.deno_runtime_path", return_value=""
        ), patch("app.core.download_service.ffmpeg_runtime_path", return_value="C:/tools/ffmpeg.exe"), patch(
            "app.core.download_service.ffprobe_runtime_path", return_value="C:/tools/ffprobe.exe"
        ), patch("app.core.download_service.probe_video_stream", return_value=VideoStreamInfo(
            "vp9", 1920, 1080, 20.0, 30.0, True, 1,
        )), patch("app.core.download_service.prepare_transcode_media", side_effect=fake_transcode) as transcode, patch(
            "app.core.download_service.validate_media_file", return_value=validation_result(media_path)
        ) as validate:
            worker.run()

        transcode.assert_called_once()
        # The validated FFprobe document is reused for topology checks and the
        # final task record, so the converted output is probed only once.
        validate.assert_called_once()
        transcode_index = next(index for index, item in enumerate(stages) if item.get("stage") == "transcoding")
        verify_index = next(index for index, item in enumerate(stages) if item.get("stage") == "verifying")
        self.assertLess(transcode_index, verify_index)
        self.assertTrue(any(item.get("stage") == "transcoding" and item.get("stage_progress") == 42.0 for item in stages))
        row = self.db.list_download_tasks()[0]
        self.assertEqual(row["transcode_codec"], "h264")
        self.assertEqual(row["transcode_device"], "gpu")
        self.assertEqual(row["transcode_encoder"], "h264_nvenc")

    def test_transcode_cleanup_failure_does_not_mask_validation_failure(self) -> None:
        media_path = self.root / "invalid-transcode-source.webm"
        temporary_path = self.root / "invalid-transcode-output.mp4"
        media_path.write_bytes(b"source")
        temporary_path.write_bytes(b"invalid")
        task = DownloadTask(
            "invalid-transcode-cleanup",
            "https://example.test/watch/invalid-transcode-cleanup",
            str(self.root),
            status="downloading",
            transcode_encoder="libx264",
        )
        worker = self._worker(task)
        discarded: list[bool] = []

        class LockedPrepared:
            def __init__(self, path: Path) -> None:
                self.temporary_path = path

            def discard(self) -> None:
                discarded.append(True)
                raise PermissionError("temporary file is locked")

        with patch(
            "app.core.download_service.probe_video_stream",
            return_value=VideoStreamInfo("vp9", 1920, 1080, 20.0, 30.0, True),
        ), patch(
            "app.core.download_service.prepare_transcode_media",
            return_value=LockedPrepared(temporary_path),
        ), patch(
            "app.core.download_service.validate_media_file",
            side_effect=RuntimeError("validation failed"),
        ), patch.object(worker, "_cleanup_warning") as cleanup_warning:
            with self.assertRaisesRegex(RuntimeError, "validation failed"):
                worker._build_validated_transcode(
                    {"duration": 20.0},
                    str(media_path),
                    None,
                    "C:/tools/ffprobe.exe",
                    "C:/tools/ffmpeg.exe",
                    False,
                    lambda _percent, _encoder: None,
                )

        self.assertEqual(discarded, [True])
        cleanup_warning.assert_called_once()
        self.assertIn("保留供检查", cleanup_warning.call_args.args[1])

    def test_post_commit_transcode_cleanup_failure_keeps_download_completed(self) -> None:
        media_path = self.root / "cleanup-warning.mp4"
        media_path.write_bytes(b"downloaded-media")
        FakeYoutubeDL.payload = {
            "id": "cleanup-warning",
            "title": "清理失败仍完成",
            "duration": 20,
            "_filename": str(media_path),
            "requested_downloads": [
                {"filepath": str(media_path), "vcodec": "vp9", "acodec": "opus"}
            ],
        }
        task = DownloadTask(
            "cleanup-warning-task",
            "https://example.test/watch/cleanup-warning",
            str(self.root),
            playlist_mode="single",
            status="downloading",
            transcode_encoder="h264_nvenc",
        )
        worker = self._worker(task)
        completed: list[MediaItem] = []
        failures: list[str] = []
        stages: list[dict] = []
        worker.completed.connect(lambda _task_id, item: completed.append(item))
        worker.failed.connect(lambda _task_id, error: failures.append(error))
        worker.progress.connect(lambda _task_id, payload: stages.append(dict(payload)))

        with patch("app.core.download_service.yt_dlp", self._fake_ytdlp()), patch(
            "app.core.download_service.deno_runtime_path", return_value=""
        ), patch(
            "app.core.download_service.ffmpeg_runtime_path", return_value="C:/tools/ffmpeg.exe"
        ), patch(
            "app.core.download_service.ffprobe_runtime_path", return_value="C:/tools/ffprobe.exe"
        ), patch(
            "app.core.download_service.probe_video_stream",
            return_value=VideoStreamInfo("vp9", 1920, 1080, 20.0, 30.0, True, 1),
        ), patch(
            "app.core.download_service.prepare_transcode_media",
            return_value=FakePreparedTranscode(media_path, "h264_nvenc"),
        ), patch(
            "app.core.download_service.validate_media_file",
            return_value=validation_result(media_path),
        ), patch.object(
            FakePublishedTranscode,
            "finalize",
            side_effect=RuntimeError("simulated locked backup"),
        ):
            worker.run()

        self.assertEqual(failures, [])
        self.assertEqual(len(completed), 1)
        self.assertEqual(self.db.list_download_tasks()[0]["status"], "completed")
        self.assertEqual(len(self.db.list_media()), 1)
        self.assertTrue(any(
            item.get("stage") == "completed"
            and "旧文件清理失败" in str(item.get("completion_warning") or "")
            for item in stages
        ))

    def test_completion_rollback_cleanup_errors_do_not_mask_original_commit_failure(self) -> None:
        media_path = self.root / "rollback-cleanup-errors.mp4"
        media_path.write_bytes(b"downloaded-media")
        task = DownloadTask(
            "rollback-cleanup-errors",
            "https://example.test/watch/rollback-cleanup-errors",
            str(self.root),
            status="downloading",
        )
        worker = self._worker(task)
        cleanup_calls: list[str] = []

        class FailingPublished:
            final_path = media_path

            def rollback(self):
                cleanup_calls.append("rollback-first")
                raise RuntimeError("rollback failed")

        class FirstPrepared:
            temporary_path = media_path

            def commit(self):
                return FailingPublished()

            def discard(self):
                cleanup_calls.append("discard-first")

        class FailingCommitPrepared:
            temporary_path = self.root / "failed-commit.tmp"

            def commit(self):
                raise sqlite3.OperationalError("database publication failed")

            def discard(self):
                cleanup_calls.append("discard-failing")
                raise RuntimeError("discard failed")

        class RemainingPrepared:
            temporary_path = self.root / "remaining.tmp"

            def commit(self):
                raise AssertionError("must not commit after the first failure")

            def discard(self):
                cleanup_calls.append("discard-remaining")

        prepared = [FirstPrepared(), FailingCommitPrepared(), RemainingPrepared()]
        worker._pending_transcodes.extend(prepared)  # type: ignore[arg-type]
        item = MediaItem(
            source_url=task.url,
            title="Rollback cleanup",
            video_path=str(media_path),
        )

        with self.assertRaisesRegex(
            sqlite3.OperationalError,
            "database publication failed",
        ):
            worker._commit_completed_media(
                [item],
                prepared,  # type: ignore[arg-type]
                [],
            )

        self.assertEqual(
            cleanup_calls,
            ["rollback-first", "discard-failing", "discard-remaining"],
        )
        self.assertEqual(self.db.list_download_tasks()[0]["status"], "downloading")
        self.assertEqual(self.db.list_media(), [])
        log_events = worker.logs.read(task.id)
        self.assertTrue(any(event.get("category") == "文件/回滚" for event in log_events))
        self.assertTrue(any(
            event.get("category") == "文件/清理"
            and "未提交转码文件失败" in str(event.get("message") or "")
            for event in log_events
        ))

    def test_enabled_opening_cover_passes_saved_frame_count_to_final_ffmpeg_step(self) -> None:
        media_path = self.root / "opening-cover.mp4"
        cover_path = self.root / "opening-cover.webp"
        media_path.write_bytes(b"downloaded-media")
        cover_path.write_bytes(b"cover-image")
        FakeYoutubeDL.payload = {
            "id": "opening-cover",
            "title": "片头封面测试",
            "duration": 20,
            "_filename": str(media_path),
            "requested_downloads": [
                {"filepath": str(media_path), "vcodec": "h264", "acodec": "aac"}
            ],
        }
        task = DownloadTask(
            "opening-cover-task",
            "https://example.test/watch/opening-cover",
            str(self.root),
            playlist_mode="single",
            status="downloading",
            options_json={
                "prepend_cover_enabled": True,
                "prepend_cover_frames": 5,
            },
        )
        worker = self._worker(task)

        def fake_transcode(path, _ffmpeg, codec, device, **kwargs):
            self.assertEqual(Path(path), media_path)
            self.assertEqual((codec, device), ("original", "auto"))
            self.assertEqual(Path(kwargs["cover_path"]), cover_path)
            self.assertEqual(kwargs["prepend_cover_frames"], 5)
            self.assertEqual(kwargs["source_codec"], "h264")
            self.assertEqual(kwargs["source_frame_rate"], 30.0)
            self.assertTrue(kwargs["source_has_audio"])
            return FakePreparedTranscode(media_path, "libx264")

        with patch("app.core.download_service.yt_dlp", self._fake_ytdlp()), patch(
            "app.core.download_service.deno_runtime_path", return_value=""
        ), patch("app.core.download_service.ffmpeg_runtime_path", return_value="C:/tools/ffmpeg.exe"), patch(
            "app.core.download_service.ffprobe_runtime_path", return_value="C:/tools/ffprobe.exe"
        ), patch(
            "app.core.download_service.probe_video_stream",
            return_value=VideoStreamInfo("h264", 1920, 1080, 20.0, 30.0, True),
        ), patch(
            "app.core.download_service.prepare_transcode_media", side_effect=fake_transcode
        ) as transcode, patch(
            "app.core.download_service.validate_media_file", return_value=validation_result(media_path)
        ):
            worker.run()

        transcode.assert_called_once()
        self.assertTrue(cover_path.exists())

    def test_failed_optional_transcode_keeps_original_media_and_completes_task(self) -> None:
        media_path = self.root / "downloaded-original.webm"
        media_path.write_bytes(b"complete-original-media")
        FakeYoutubeDL.payload = {
            "id": "keep-original",
            "title": "保留原始成品",
            "duration": 30,
            "_filename": str(media_path),
            "requested_downloads": [
                {"filepath": str(media_path), "vcodec": "vp9", "acodec": "opus"}
            ],
        }
        task = DownloadTask(
            "transcode-warning-task",
            "https://example.test/watch/keep-original",
            str(self.root),
            playlist_mode="single",
            status="downloading",
            transcode_encoder="h264_nvenc",
        )
        worker = self._worker(task)
        completed: list[MediaItem] = []
        failures: list[str] = []
        stages: list[dict] = []
        worker.completed.connect(lambda _task_id, item: completed.append(item))
        worker.failed.connect(lambda _task_id, error: failures.append(error))
        worker.progress.connect(lambda _task_id, payload: stages.append(dict(payload)))

        with patch("app.core.download_service.yt_dlp", self._fake_ytdlp()), patch(
            "app.core.download_service.deno_runtime_path", return_value=""
        ), patch("app.core.download_service.ffmpeg_runtime_path", return_value="C:/tools/ffmpeg.exe"), patch(
            "app.core.download_service.ffprobe_runtime_path", return_value="C:/tools/ffprobe.exe"
        ), patch(
            "app.core.download_service.prepare_transcode_media",
            side_effect=TranscodeError("NVENC API version is not supported"),
        ), patch(
            "app.core.download_service.validate_media_file",
            return_value=validation_result(media_path),
        ) as validate:
            worker.run()

        self.assertEqual(failures, [])
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0].video_path, str(media_path))
        self.assertTrue(media_path.is_file())
        validate.assert_called_once()
        self.assertEqual(validate.call_args.args[0], media_path)
        row = self.db.list_download_tasks()[0]
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["progress"], 100.0)
        self.assertEqual(row["media_path"], str(media_path))
        self.assertEqual(self.db.list_media()[0].video_path, str(media_path))
        self.assertTrue(any(
            item.get("stage") == "completed"
            and "转换失败" in str(item.get("stage_text") or "")
            and item.get("completion_warning")
            for item in stages
        ))
        warning_events = [
            event for event in worker.logs.read(task.id)
            if event.get("level") == "warning" and event.get("category") == "格式/转换"
        ]
        self.assertEqual(len(warning_events), 1)
        self.assertIn("已保留原始下载文件", warning_events[0]["message"])

    def test_unexpected_optional_transcode_setup_error_falls_back_to_original(self) -> None:
        media_path = self.root / "optional-setup-error.webm"
        media_path.write_bytes(b"complete-original-media")
        task = DownloadTask(
            "optional-setup-error",
            "https://example.test/watch/optional-setup-error",
            str(self.root),
            status="downloading",
            transcode_encoder="h264_nvenc",
        )
        worker = self._worker(task)
        warnings: list[str] = []

        with patch(
            "app.core.download_service.ffmpeg_runtime_path",
            side_effect=OSError("configured FFmpeg became unavailable"),
        ):
            result = worker._prepare_optional_transcode(
                {"title": "Optional setup failure"},
                str(media_path),
                None,
                "C:/tools/ffprobe.exe",
                warnings,
            )

        self.assertEqual(result, (str(media_path), media_path, None, None))
        self.assertEqual(len(warnings), 1)
        self.assertIn("已保留原始下载文件", warnings[0])
        warning_events = [
            event for event in worker.logs.read(task.id)
            if event.get("category") == "格式/转换"
            and event.get("level") == "warning"
        ]
        self.assertEqual(len(warning_events), 1)
        self.assertEqual(
            (warning_events[0].get("details") or {}).get("error_type"),
            "OSError",
        )

    def test_optional_transcode_cancellation_propagates_without_false_warning(self) -> None:
        media_path = self.root / "optional-transcode-canceled.webm"
        media_path.write_bytes(b"complete-original-media")
        task = DownloadTask(
            "optional-transcode-canceled",
            "https://example.test/watch/optional-transcode-canceled",
            str(self.root),
            status="downloading",
            transcode_encoder="h264_nvenc",
        )
        worker = self._worker(task)
        warnings: list[str] = []

        with patch.object(
            worker,
            "_build_validated_transcode",
            side_effect=InterruptedError("用户取消格式转换"),
        ):
            with self.assertRaisesRegex(InterruptedError, "用户取消格式转换"):
                worker._prepare_optional_transcode(
                    {"title": "Canceled optional conversion"},
                    str(media_path),
                    None,
                    "C:/tools/ffprobe.exe",
                    warnings,
                )

        self.assertEqual(warnings, [])
        self.assertTrue(media_path.is_file())
        self.assertFalse(any(
            event.get("level") == "warning"
            and "已保留原始下载文件" in str(event.get("message") or "")
            for event in worker.logs.read(task.id)
        ))

    def test_completed_media_conversion_skips_file_already_in_target_codec(self) -> None:
        self.assertFalse(hasattr(CompletedMediaTranscodeWorker, "_sha256"))
        media_path = self.root / "already-h264.mp4"
        media_path.write_bytes(b"h264-media")
        worker = CompletedMediaTranscodeWorker(
            "already-target",
            str(media_path),
            "ffmpeg.exe",
            "ffprobe.exe",
            "h264_nvenc",
        )
        skipped: list[dict] = []
        failures: list[str] = []
        worker.skipped.connect(lambda _task_id, payload: skipped.append(dict(payload)))
        worker.failed.connect(lambda _task_id, error: failures.append(error))

        with patch(
            "app.core.completed_conversion.probe_video_stream",
            return_value=VideoStreamInfo("h264", 1920, 1080, 12.0),
        ), patch("app.core.completed_conversion.prepare_transcode_media") as transcode:
            worker.run()

        self.assertEqual(failures, [])
        self.assertEqual(skipped[0]["reason"], "already_target")
        self.assertEqual(skipped[0]["codec"], "h264")
        transcode.assert_not_called()
        self.assertEqual(media_path.read_bytes(), b"h264-media")

    def test_manual_conversion_cleanup_failure_still_finishes_and_removes_workspace(self) -> None:
        media_path = self.root / "manual-source.mp4"
        media_path.write_bytes(b"manual-source")
        processing_root = self.root / "processing"

        class ReleaseFailManager:
            def __init__(self) -> None:
                self.acquire_count = 0
                self.release_count = 0

            def acquire(self, *_args, **_kwargs):
                self.acquire_count += 1
                return object()

            def release(self, _reservation) -> bool:
                self.release_count += 1
                if self.release_count == 1:
                    raise RuntimeError("simulated release failure")
                return True

        manager = ReleaseFailManager()
        worker = CompletedMediaTranscodeWorker(
            "manual-cleanup",
            str(media_path),
            "ffmpeg.exe",
            "ffprobe.exe",
            "libx265",
            disk_capacity_manager=manager,
            processing_temp_dir=str(processing_root),
        )
        completed: list[dict] = []
        failures: list[str] = []
        finished: list[bool] = []
        worker.completed.connect(lambda _task_id, payload: completed.append(dict(payload)))
        worker.failed.connect(lambda _task_id, error: failures.append(error))
        worker.finished.connect(lambda: finished.append(True))

        with patch(
            "app.core.completed_conversion.probe_video_stream",
            return_value=VideoStreamInfo("h264", 1920, 1080, 12.0, has_audio=True),
        ), patch(
            "app.core.completed_conversion.prepare_transcode_media",
            return_value=FakePreparedTranscode(media_path, "libx265"),
        ), patch(
            "app.core.completed_conversion.validate_media_file",
            return_value=validation_result(media_path),
        ):
            worker.run()

        workspace = processing_temp_workspace_path(
            processing_root,
            worker.task_id,
            "manual-transcode",
        )
        self.assertEqual(failures, [])
        self.assertEqual(len(completed), 1)
        self.assertEqual(finished, [True])
        self.assertEqual(manager.acquire_count, 1)
        self.assertEqual(manager.release_count, 2)
        self.assertEqual(worker._disk_lease.active_count, 0)
        self.assertIsNotNone(workspace)
        assert workspace is not None
        self.assertFalse(workspace.exists())

    def test_manual_conversion_keeps_capacity_reserved_through_atomic_publish(self) -> None:
        media_path = self.root / "manual-capacity-scope.webm"
        media_path.write_bytes(b"source-media")

        class RecordingManager:
            def __init__(self) -> None:
                self.release_count = 0

            def acquire(self, *_args, **_kwargs):
                return object()

            def release(self, _reservation) -> bool:
                self.release_count += 1
                return True

        manager = RecordingManager()
        worker = CompletedMediaTranscodeWorker(
            "manual-capacity-scope",
            str(media_path),
            "ffmpeg.exe",
            "ffprobe.exe",
            "libx265",
            disk_capacity_manager=manager,
        )
        active_at_commit: list[int] = []

        class CapacityAwarePrepared(FakePreparedTranscode):
            def commit(self):
                active_at_commit.append(worker._disk_lease.active_count)
                return super().commit()

        completed: list[dict] = []
        failures: list[str] = []
        worker.completed.connect(lambda _task_id, payload: completed.append(dict(payload)))
        worker.failed.connect(lambda _task_id, error: failures.append(error))

        with patch(
            "app.core.completed_conversion.probe_video_stream",
            return_value=VideoStreamInfo("h264", 1920, 1080, 12.0, has_audio=True),
        ), patch(
            "app.core.completed_conversion.prepare_transcode_media",
            return_value=CapacityAwarePrepared(media_path, "libx265"),
        ), patch(
            "app.core.completed_conversion.validate_media_file",
            return_value=validation_result(media_path),
        ):
            worker.run()

        self.assertEqual(failures, [])
        self.assertEqual(len(completed), 1)
        self.assertEqual(active_at_commit, [1])
        self.assertEqual(worker._disk_lease.active_count, 0)
        self.assertEqual(manager.release_count, 1)

    def test_manual_conversion_disk_wait_cancellation_is_not_reported_as_failure(self) -> None:
        media_path = self.root / "manual-disk-cancel.webm"
        media_path.write_bytes(b"source-media")

        class CanceledManager:
            @staticmethod
            def acquire(*_args, **_kwargs):
                raise DiskCapacityError(
                    DiskCapacityErrorCode.CANCELLED,
                    "等待磁盘空间时任务已取消。",
                    "需要时可重新开始转换。",
                )

            @staticmethod
            def release(_reservation) -> bool:
                return False

        worker = CompletedMediaTranscodeWorker(
            "manual-disk-cancel",
            str(media_path),
            "ffmpeg.exe",
            "ffprobe.exe",
            "libx265",
            disk_capacity_manager=CanceledManager(),
        )
        canceled: list[str] = []
        failures: list[str] = []
        worker.canceled.connect(canceled.append)
        worker.failed.connect(lambda _task_id, error: failures.append(error))

        with patch(
            "app.core.completed_conversion.probe_video_stream",
            return_value=VideoStreamInfo("h264", 1920, 1080, 12.0),
        ), patch("app.core.completed_conversion.prepare_transcode_media") as transcode:
            worker.run()

        self.assertEqual(canceled, [worker.task_id])
        self.assertEqual(failures, [])
        transcode.assert_not_called()

    def test_manual_conversion_validation_cancellation_is_not_reported_as_failure(self) -> None:
        media_path = self.root / "manual-validation-cancel.webm"
        media_path.write_bytes(b"source-media")
        worker = CompletedMediaTranscodeWorker(
            "manual-validation-cancel",
            str(media_path),
            "ffmpeg.exe",
            "ffprobe.exe",
            "libx265",
        )
        canceled: list[str] = []
        failures: list[str] = []
        worker.canceled.connect(canceled.append)
        worker.failed.connect(lambda _task_id, error: failures.append(error))

        with patch(
            "app.core.completed_conversion.probe_video_stream",
            return_value=VideoStreamInfo("h264", 1920, 1080, 12.0),
        ), patch(
            "app.core.completed_conversion.prepare_transcode_media",
            return_value=FakePreparedTranscode(media_path, "libx265"),
        ), patch(
            "app.core.completed_conversion.validate_media_file",
            side_effect=MediaValidationError(
                MediaValidationErrorCode.CANCELLED,
                "媒体成品校验已取消。",
                "需要时可重新开始该任务。",
            ),
        ):
            worker.run()

        self.assertEqual(canceled, [worker.task_id])
        self.assertEqual(failures, [])
        self.assertEqual(worker._disk_lease.active_count, 0)

    def test_manual_conversion_progress_rejects_non_finite_or_invalid_percent(self) -> None:
        worker = CompletedMediaTranscodeWorker(
            "manual-invalid-progress",
            str(self.root / "video.mp4"),
            "ffmpeg.exe",
            "ffprobe.exe",
            "libx264",
        )
        events: list[dict] = []
        worker.progress.connect(lambda _task_id, payload: events.append(dict(payload)))

        worker._progress("transcoding", "invalid nan", float("nan"))
        worker._progress("verifying", "invalid string", "not-a-number")

        self.assertEqual([event["stage_progress"] for event in events], [0.0, 0.0])

    def test_replacing_completed_media_path_updates_task_and_catalog_together(self) -> None:
        old_path = self.root / "source.webm"
        new_path = self.root / "source.mp4"
        old_path.write_bytes(b"source")
        new_path.write_bytes(b"converted")
        task = DownloadTask(
            "replace-media",
            "https://example.test/watch/replace-media",
            str(self.root),
            status="downloading",
        )
        self.db.upsert_download_task(task)
        media = MediaItem(
            source_url=task.url,
            title="Converted",
            video_path=str(old_path),
        )
        completion = DownloadTask(
            task.id,
            task.url,
            task.output_dir,
            status="completed",
            media_path=str(old_path),
        )
        self.db.complete_download_task(completion, media)

        self.db.replace_completed_media_path(
            task.id,
            str(old_path),
            str(new_path),
            transcode_codec="h264",
            transcode_device="gpu",
            transcode_encoder="h264_nvenc",
        )

        task_row = self.db.list_download_tasks()[0]
        media_row = self.db.list_media()[0]
        self.assertEqual(task_row["status"], "completed")
        self.assertEqual(task_row["media_path"], str(new_path))
        self.assertEqual(task_row["transcode_encoder"], "h264_nvenc")
        self.assertEqual(media_row.video_path, str(new_path))
        manifest = self.db.list_download_task_files(task.id)
        self.assertEqual(len(manifest), 1)
        self.assertEqual(manifest[0]["path"], str(new_path))
        self.assertEqual(manifest[0]["kind"], "media")

    def test_replacing_media_path_does_not_rewrite_unrelated_catalog_rows(self) -> None:
        old_path = self.root / "shared.webm"
        new_path = self.root / "shared.mp4"
        old_path.write_bytes(b"source")
        new_path.write_bytes(b"converted")
        task = DownloadTask(
            "replace-owned-media",
            "https://example.test/watch/owned",
            str(self.root),
            status="completed",
            media_path=str(old_path),
        )
        self.db.upsert_download_task(task)
        owned = MediaItem(source_url=task.url, title="Owned", video_path=str(old_path))
        self.db.complete_download_task(task, owned)
        unrelated = MediaItem(
            source_url="https://example.test/watch/unrelated",
            title="Unrelated",
            video_path=str(old_path),
        )
        unrelated_id = self.db.add_media(unrelated)

        self.db.replace_completed_media_path(
            task.id,
            str(old_path),
            str(new_path),
            transcode_codec="h264",
            transcode_device="cpu",
            transcode_encoder="libx264",
        )

        self.assertEqual(self.db.get_media(owned.id).video_path, str(new_path))
        self.assertEqual(self.db.get_media(unrelated_id).video_path, str(old_path))

    def test_replacing_media_path_requires_an_owned_catalog_row(self) -> None:
        old_path = self.root / "unowned-shared.webm"
        new_path = self.root / "unowned-shared.mp4"
        old_path.write_bytes(b"source")
        new_path.write_bytes(b"converted")
        task = DownloadTask(
            "replace-unowned-media",
            "https://example.test/watch/missing-owner",
            str(self.root),
            status="completed",
            media_path=str(old_path),
        )
        self.db.upsert_download_task(task)
        unrelated = MediaItem(
            source_url="https://example.test/watch/unrelated-owner",
            title="Unrelated",
            video_path=str(old_path),
        )
        unrelated_id = self.db.add_media(unrelated)

        with self.assertRaisesRegex(LookupError, "归属不匹配"):
            self.db.replace_completed_media_path(
                task.id,
                str(old_path),
                str(new_path),
                transcode_codec="h264",
                transcode_device="cpu",
                transcode_encoder="libx264",
            )

        self.assertEqual(self.db.get_media(unrelated_id).video_path, str(old_path))
        self.assertEqual(self.db.list_download_tasks()[0]["media_path"], str(old_path))
        self.assertFalse(self.db.conn.in_transaction)

    def test_stale_completed_conversion_cannot_overwrite_newer_media_path(self) -> None:
        old_path = self.root / "stale.webm"
        newer_path = self.root / "newer.mp4"
        attempted_path = self.root / "attempted.mp4"
        for path in (old_path, newer_path, attempted_path):
            path.write_bytes(path.name.encode("utf-8"))
        task = DownloadTask(
            "stale-conversion",
            "https://example.test/watch/stale",
            str(self.root),
            status="completed",
            media_path=str(old_path),
        )
        self.db.upsert_download_task(task)
        media = MediaItem(source_url=task.url, video_path=str(old_path))
        self.db.complete_download_task(task, media)
        with self.db._lock:
            self.db.conn.execute(
                "UPDATE download_tasks SET media_path=? WHERE id=?",
                (str(newer_path), task.id),
            )
            self.db.conn.commit()

        with self.assertRaisesRegex(LookupError, "已经变化"):
            self.db.replace_completed_media_path(
                task.id,
                str(old_path),
                str(attempted_path),
                transcode_codec="h264",
                transcode_device="cpu",
                transcode_encoder="libx264",
            )

        self.assertEqual(self.db.get_media(media.id).video_path, str(old_path))
        self.assertEqual(self.db.list_download_tasks()[0]["media_path"], str(newer_path))
        self.assertFalse(self.db.conn.in_transaction)

    def test_completed_conversion_database_failure_restores_same_path_original(self) -> None:
        media_path = self.root / "rollback.mp4"
        backup_path = self.root / ".rollback.mp4.pre-transcode.bak"
        media_path.write_bytes(b"converted")
        backup_path.write_bytes(b"original")
        task = DownloadTask(
            "rollback-conversion",
            "https://example.test/watch/rollback",
            str(self.root),
            status="completed",
            media_path=str(media_path),
        )
        self.db.upsert_download_task(task)
        service = DownloadService(self.db)
        service.tasks[task.id] = task
        publication = PublishedTranscode(
            source_path=media_path,
            final_path=media_path,
            encoder="libx264",
            preserve_source=False,
            backup_path=backup_path,
        )

        with patch.object(self.db, "replace_completed_media_path", side_effect=RuntimeError("db failed")):
            service._on_completed_conversion_completed(task.id, {
                "old_path": str(media_path),
                "new_path": str(media_path),
                "encoder": "libx264",
                "sha256": "digest",
                "publication": publication,
            })

        self.assertEqual(media_path.read_bytes(), b"original")
        recoveries = list(self.root.glob("rollback.uncommitted*.mp4"))
        self.assertEqual(len(recoveries), 1)
        self.assertEqual(recoveries[0].read_bytes(), b"converted")
        service.shutdown(timeout_ms=0)

    def test_completed_conversion_database_failure_quarantines_new_path(self) -> None:
        old_path = self.root / "rollback-different.webm"
        new_path = self.root / "rollback-different.mp4"
        old_path.write_bytes(b"original")
        new_path.write_bytes(b"converted")
        task = DownloadTask(
            "rollback-different-conversion",
            "https://example.test/watch/rollback-different",
            str(self.root),
            status="completed",
            media_path=str(old_path),
        )
        self.db.upsert_download_task(task)
        service = DownloadService(self.db)
        service._register_task(task)
        publication = PublishedTranscode(
            source_path=old_path,
            final_path=new_path,
            encoder="libx264",
            preserve_source=False,
        )

        with patch.object(
            self.db,
            "replace_completed_media_path",
            side_effect=RuntimeError("db failed"),
        ):
            service._on_completed_conversion_completed(task.id, {
                "old_path": str(old_path),
                "new_path": str(new_path),
                "encoder": "libx264",
                "publication": publication,
            })

        self.assertEqual(old_path.read_bytes(), b"original")
        self.assertFalse(new_path.exists())
        recoveries = list(self.root.glob("rollback-different.uncommitted*.mp4"))
        self.assertEqual(len(recoveries), 1)
        self.assertEqual(recoveries[0].read_bytes(), b"converted")
        self.assertEqual(task.status, "completed")
        self.assertEqual(task.media_path, str(old_path))
        service.shutdown(timeout_ms=0)

    def test_orphaned_conversion_publication_is_rolled_back(self) -> None:
        media_path = self.root / "orphaned-publication.mp4"
        backup_path = self.root / ".orphaned-publication.mp4.pre-transcode.bak"
        media_path.write_bytes(b"converted")
        backup_path.write_bytes(b"original")
        publication = PublishedTranscode(
            source_path=media_path,
            final_path=media_path,
            encoder="libx264",
            preserve_source=False,
            backup_path=backup_path,
        )
        service = DownloadService(self.db)

        service._on_completed_conversion_completed("missing-task", {
            "old_path": str(media_path),
            "new_path": str(media_path),
            "encoder": "libx264",
            "publication": publication,
        })

        self.assertEqual(media_path.read_bytes(), b"original")
        self.assertFalse(backup_path.exists())
        recoveries = list(self.root.glob("orphaned-publication.uncommitted*.mp4"))
        self.assertEqual(len(recoveries), 1)
        self.assertEqual(recoveries[0].read_bytes(), b"converted")
        service.shutdown(timeout_ms=0)

    def test_conversion_thread_cleanup_waits_for_terminal_handler(self) -> None:
        old_path = self.root / "queued-conversion.webm"
        new_path = self.root / "queued-conversion.mp4"
        old_path.write_bytes(b"old")
        new_path.write_bytes(b"new")
        task = DownloadTask(
            "queued-conversion-cleanup",
            "https://example.test/queued-conversion-cleanup",
            str(self.root),
            status="completed",
            media_path=str(old_path),
        )
        self.db.upsert_download_task(task)
        self.db.complete_download_task(
            task,
            MediaItem(source_url=task.url, title=task.title, video_path=str(old_path)),
        )
        service = DownloadService(self.db)
        service._register_task(task)
        service.conversion_threads[task.id] = QThread()
        service.conversion_workers[task.id] = object()  # type: ignore[assignment]
        service._pending_deletes[task.id] = False
        publication = PublishedTranscode(
            source_path=old_path,
            final_path=new_path,
            encoder="libx264",
            preserve_source=False,
        )

        service._defer_conversion_thread_finished(task.id)
        service._on_completed_conversion_completed(task.id, {
            "old_path": str(old_path),
            "new_path": str(new_path),
            "encoder": "libx264",
            "publication": publication,
        })
        self.assertEqual(task.media_path, str(new_path))
        QCoreApplication.processEvents()

        self.assertNotIn(task.id, service.tasks)
        self.assertNotIn(task.id, service.conversion_threads)
        self.assertNotIn(task.id, service.conversion_workers)
        self.assertEqual(self.db.list_download_tasks(), [])
        self.assertTrue(new_path.is_file())
        self.assertFalse(old_path.exists())
        service.shutdown(timeout_ms=0)

    def test_old_conversion_cleanup_cannot_remove_replacement_runtime(self) -> None:
        media_path = self.root / "replacement-conversion.mp4"
        media_path.write_bytes(b"media")
        task = DownloadTask(
            "replacement-conversion-runtime",
            "https://example.test/replacement-conversion-runtime",
            str(self.root),
            status="processing",
            media_path=str(media_path),
        )
        self.db.upsert_download_task(task)
        service = DownloadService(self.db)
        service._register_task(task)
        old_thread = QThread()
        replacement_thread = QThread()
        replacement_worker = object()
        service.conversion_threads[task.id] = old_thread
        service.conversion_workers[task.id] = object()  # type: ignore[assignment]

        service._defer_conversion_thread_finished(task.id, old_thread)
        service.conversion_threads[task.id] = replacement_thread
        service.conversion_workers[task.id] = replacement_worker  # type: ignore[assignment]
        QCoreApplication.processEvents()

        self.assertIs(service.conversion_threads[task.id], replacement_thread)
        self.assertIs(service.conversion_workers[task.id], replacement_worker)
        self.assertEqual(task.status, "processing")
        self.assertEqual(len(service._deferred_conversion_finishes), 0)
        service.conversion_threads.clear()
        service.conversion_workers.clear()
        replacement_thread.deleteLater()
        QCoreApplication.processEvents()
        service.shutdown(timeout_ms=0)

    def test_conversion_thread_exit_without_outcome_restores_completed_task(self) -> None:
        media_path = self.root / "conversion-no-outcome.mp4"
        media_path.write_bytes(b"media")
        task = DownloadTask(
            "conversion-no-outcome",
            "https://example.test/conversion-no-outcome",
            str(self.root),
            status="completed",
            progress=100.0,
            media_path=str(media_path),
        )
        self.db.upsert_download_task(task)
        service = DownloadService(self.db)
        service._register_task(task)
        task.status = "processing"
        task.stage = "transcoding"
        task.stage_text = "正在转换视频格式"
        service._sync_task_indexes(task)
        thread = QThread()
        service.conversion_threads[task.id] = thread
        service.conversion_workers[task.id] = object()  # type: ignore[assignment]
        failures: list[str] = []
        service.conversion_failed.connect(
            lambda _task_id, error: failures.append(error)
        )

        service._conversion_thread_finished(task.id)

        self.assertEqual(task.status, "completed")
        self.assertEqual(task.stage, "completed")
        self.assertIn("意外结束", task.completion_warning)
        self.assertEqual(service._task_index.states[task.id][1], "completed")
        self.assertNotIn(task.id, service.conversion_threads)
        self.assertNotIn(task.id, service.conversion_workers)
        self.assertEqual(failures, [task.completion_warning])
        self.assertEqual(str(self.db.list_download_tasks()[0]["status"]), "completed")
        thread.deleteLater()
        QCoreApplication.processEvents()
        service.shutdown(timeout_ms=0)

    def test_manual_conversion_cancel_is_runtime_only_and_restart_safe(self) -> None:
        media_path = self.root / "conversion-runtime-cancel.mp4"
        media_path.write_bytes(b"media")
        task = DownloadTask(
            "conversion-runtime-cancel",
            "https://example.test/conversion-runtime-cancel",
            str(self.root),
            status="completed",
            progress=100.0,
            media_path=str(media_path),
        )
        self.db.upsert_download_task(task)
        service = DownloadService(self.db)
        service._register_task(task)
        task.status = "processing"
        task.stage = "transcoding"
        service._sync_task_indexes(task)
        cancel_calls: list[bool] = []
        service.conversion_workers[task.id] = SimpleNamespace(
            cancel=lambda: cancel_calls.append(True)
        )

        service.cancel(task.id)

        self.assertEqual(cancel_calls, [True])
        self.assertEqual(task.status, "canceling")
        self.assertEqual(task.stage, "canceling")
        durable = self.db.list_download_tasks()[0]
        self.assertEqual(str(durable["status"]), "completed")
        self.assertEqual(str(durable["media_path"]), str(media_path))

        # Even if the queued canceled signal is lost during teardown, thread
        # cleanup must restore the completed task rather than strand it in a
        # runtime-only canceling state.
        service._conversion_thread_finished(task.id)
        self.assertEqual(task.status, "completed")
        self.assertEqual(task.completion_warning, "")
        self.assertNotIn(task.id, service.conversion_workers)
        service.shutdown(timeout_ms=0)

    def test_manual_conversion_delete_failure_keeps_completed_record_retryable(self) -> None:
        media_path = self.root / "conversion-delete-retry.mp4"
        media_path.write_bytes(b"media")
        task = DownloadTask(
            "conversion-delete-retry",
            "https://example.test/conversion-delete-retry",
            str(self.root),
            status="completed",
            progress=100.0,
            media_path=str(media_path),
        )
        self.db.upsert_download_task(task)
        service = DownloadService(self.db)
        service._register_task(task)
        task.status = "processing"
        task.stage = "transcoding"
        service._sync_task_indexes(task)
        cancel_calls: list[bool] = []
        service.conversion_workers[task.id] = SimpleNamespace(
            cancel=lambda: cancel_calls.append(True)
        )

        self.assertTrue(service.delete_task(task.id, delete_files=False))
        self.assertEqual(cancel_calls, [True])
        self.assertEqual(task.status, "canceling")
        self.assertEqual(str(self.db.list_download_tasks()[0]["status"]), "completed")

        with patch.object(
            service,
            "_remove_task_record",
            side_effect=sqlite3.OperationalError("database busy"),
        ):
            service._conversion_thread_finished(task.id)

        self.assertIn(task.id, service.tasks)
        self.assertEqual(task.status, "completed")
        self.assertNotIn(task.id, service.conversion_workers)
        self.assertNotIn(task.id, service._pending_deletes)
        self.assertEqual(str(self.db.list_download_tasks()[0]["status"]), "completed")
        self.assertTrue(media_path.is_file())
        self.assertTrue(any(
            event.get("category") == "数据库/删除"
            and "可重新删除" in str(event.get("message") or "")
            for event in service.logs.read(task.id)
        ))
        service.shutdown(timeout_ms=0)

    def test_conversion_thread_start_failure_restores_completed_task(self) -> None:
        media_path = self.root / "conversion-start-failure.mp4"
        media_path.write_bytes(b"media")
        task = DownloadTask(
            "conversion-start-failure",
            "https://example.test/conversion-start-failure",
            str(self.root),
            status="completed",
            progress=100.0,
            media_path=str(media_path),
        )
        self.db.upsert_download_task(task)
        service = DownloadService(self.db)
        service._register_task(task)
        failures: list[str] = []
        service.conversion_failed.connect(
            lambda _task_id, error: failures.append(error)
        )

        with patch(
            "app.core.download_service.ffmpeg_runtime_path",
            return_value="ffmpeg.exe",
        ), patch(
            "app.core.download_service.ffprobe_runtime_path",
            return_value="ffprobe.exe",
        ), patch(
            "app.core.download_service.QThread.start",
            side_effect=RuntimeError("thread resource exhausted"),
        ):
            started = service.convert_completed_task(task.id, "libx264")

        self.assertFalse(started)
        self.assertEqual(task.status, "completed")
        self.assertEqual(task.stage, "completed")
        self.assertIn("无法启动格式转换", task.completion_warning)
        self.assertNotIn(task.id, service.conversion_threads)
        self.assertNotIn(task.id, service.conversion_workers)
        self.assertEqual(len(failures), 1)
        self.assertIn("thread resource exhausted", failures[0])
        row = self.db.list_download_tasks()[0]
        self.assertEqual(str(row["status"]), "completed")
        service.shutdown(timeout_ms=0)

    def test_shutdown_rejects_new_manual_conversion_without_creating_runtime(self) -> None:
        media_path = self.root / "conversion-during-shutdown.mp4"
        media_path.write_bytes(b"media")
        task = DownloadTask(
            "conversion-during-shutdown",
            "https://example.test/conversion-during-shutdown",
            str(self.root),
            status="completed",
            progress=100.0,
            media_path=str(media_path),
        )
        self.db.upsert_download_task(task)
        service = DownloadService(self.db)
        service._register_task(task)
        failures: list[str] = []
        service.conversion_failed.connect(
            lambda _task_id, error: failures.append(error)
        )
        service.request_shutdown()

        with patch.object(service, "_prepare_completed_conversion_runtime") as prepare:
            started = service.convert_completed_task(task.id, "libx264")

        self.assertFalse(started)
        prepare.assert_not_called()
        self.assertEqual(task.status, "completed")
        self.assertNotIn(task.id, service.conversion_threads)
        self.assertNotIn(task.id, service.conversion_workers)
        self.assertEqual(failures, ["下载服务正在退出，无法开始格式转换"])
        service.shutdown(timeout_ms=0)

    def test_manual_conversion_keeps_durable_completion_until_atomic_publish(self) -> None:
        old_path = self.root / "manual-conversion.webm"
        new_path = self.root / "manual-conversion.mp4"
        old_path.write_bytes(b"original")
        new_path.write_bytes(b"converted")
        task = DownloadTask(
            "manual-conversion-state",
            "https://example.test/manual-conversion-state",
            str(self.root),
            status="completed",
            progress=100.0,
            media_path=str(old_path),
        )
        self.db.upsert_download_task(task)
        self.db.complete_download_task(
            task,
            MediaItem(source_url=task.url, title=task.title, video_path=str(old_path)),
        )
        service = DownloadService(self.db)
        service._register_task(task)

        with patch(
            "app.core.download_service.ffmpeg_runtime_path",
            return_value="ffmpeg.exe",
        ), patch(
            "app.core.download_service.ffprobe_runtime_path",
            return_value="ffprobe.exe",
        ), patch(
            "app.core.download_service.QThread.start",
            return_value=None,
        ):
            self.assertTrue(service.convert_completed_task(task.id, "libx264"))

        self.assertEqual(task.status, "processing")
        before_publish = self.db.list_download_tasks()[0]
        self.assertEqual(str(before_publish["status"]), "completed")
        self.assertEqual(str(before_publish["media_path"]), str(old_path))

        publication = PublishedTranscode(
            source_path=old_path,
            final_path=new_path,
            encoder="libx264",
            preserve_source=False,
        )
        service._on_completed_conversion_completed(task.id, {
            "old_path": str(old_path),
            "new_path": str(new_path),
            "encoder": "libx264",
            "publication": publication,
        })

        self.assertEqual(task.status, "completed")
        self.assertEqual(task.media_path, str(new_path))
        after_publish = self.db.list_download_tasks()[0]
        self.assertEqual(str(after_publish["status"]), "completed")
        self.assertEqual(str(after_publish["media_path"]), str(new_path))
        self.assertFalse(old_path.exists())
        self.assertTrue(new_path.exists())

        worker = service.conversion_workers.pop(task.id)
        thread = service.conversion_threads.pop(task.id)
        delete_unstarted_worker(worker, thread)
        service.shutdown(timeout_ms=0)

    def test_manual_conversion_wiring_failure_leaves_no_runtime_or_state_change(self) -> None:
        media_path = self.root / "conversion-wiring-failure.mp4"
        media_path.write_bytes(b"media")
        task = DownloadTask(
            "conversion-wiring-failure",
            "https://example.test/conversion-wiring-failure",
            str(self.root),
            status="completed",
            progress=100.0,
            media_path=str(media_path),
        )
        self.db.upsert_download_task(task)
        service = DownloadService(self.db)
        service._register_task(task)
        failures: list[str] = []
        service.conversion_failed.connect(
            lambda _task_id, error: failures.append(error)
        )

        with patch(
            "app.core.download_service.ffmpeg_runtime_path",
            return_value="ffmpeg.exe",
        ), patch(
            "app.core.download_service.ffprobe_runtime_path",
            return_value="ffprobe.exe",
        ), patch.object(
            CompletedMediaTranscodeWorker,
            "moveToThread",
            side_effect=RuntimeError("wiring failed"),
        ):
            started = service.convert_completed_task(task.id, "libx264")

        self.assertFalse(started)
        self.assertEqual(task.status, "completed")
        self.assertNotIn(task.id, service.conversion_threads)
        self.assertNotIn(task.id, service.conversion_workers)
        self.assertEqual(len(failures), 1)
        self.assertIn("wiring failed", failures[0])
        self.assertEqual(str(self.db.list_download_tasks()[0]["status"]), "completed")
        service.shutdown(timeout_ms=0)

    def test_conversion_success_signal_survives_log_storage_failure(self) -> None:
        old_path = self.root / "conversion-log-failure.webm"
        new_path = self.root / "conversion-log-failure.mp4"
        old_path.write_bytes(b"original")
        new_path.write_bytes(b"converted")
        task = DownloadTask(
            "conversion-log-failure",
            "https://example.test/conversion-log-failure",
            str(self.root),
            status="completed",
            progress=100.0,
            media_path=str(old_path),
        )
        self.db.upsert_download_task(task)
        self.db.complete_download_task(
            task,
            MediaItem(source_url=task.url, title=task.title, video_path=str(old_path)),
        )
        service = DownloadService(self.db)
        service._register_task(task)
        outcomes: list[tuple[str, str, bool]] = []
        service.conversion_finished.connect(
            lambda task_id, result, skipped: outcomes.append(
                (task_id, result, skipped)
            )
        )
        publication = PublishedTranscode(
            source_path=old_path,
            final_path=new_path,
            encoder="libx264",
            preserve_source=False,
        )

        with patch.object(
            service.logs,
            "write",
            side_effect=RuntimeError("log database unavailable"),
        ), patch.object(
            service.logs,
            "flush",
            side_effect=RuntimeError("log flush unavailable"),
        ):
            service._on_completed_conversion_completed(task.id, {
                "old_path": str(old_path),
                "new_path": str(new_path),
                "encoder": "libx264",
                "publication": publication,
            })

        self.assertEqual(outcomes, [(task.id, "libx264", False)])
        self.assertEqual(task.status, "completed")
        self.assertEqual(task.media_path, str(new_path))
        self.assertFalse(old_path.exists())
        self.assertTrue(new_path.exists())
        service.shutdown(timeout_ms=0)

    def test_conversion_failure_signal_survives_log_storage_failure(self) -> None:
        media_path = self.root / "conversion-failure-log.mp4"
        media_path.write_bytes(b"media")
        task = DownloadTask(
            "conversion-failure-log",
            "https://example.test/conversion-failure-log",
            str(self.root),
            status="processing",
            media_path=str(media_path),
        )
        self.db.upsert_download_task(task)
        service = DownloadService(self.db)
        service._register_task(task)
        failures: list[tuple[str, str]] = []
        service.conversion_failed.connect(
            lambda task_id, error: failures.append((task_id, error))
        )

        with patch.object(
            service.logs,
            "write",
            side_effect=RuntimeError("log database unavailable"),
        ), patch.object(
            service.logs,
            "flush",
            side_effect=RuntimeError("log flush unavailable"),
        ):
            service._on_completed_conversion_failed(task.id, "encoder failed")

        self.assertEqual(failures, [(task.id, "encoder failed")])
        self.assertEqual(task.status, "completed")
        self.assertIn("已保留原文件", task.completion_warning)
        service.shutdown(timeout_ms=0)

    def test_service_does_not_overwrite_worker_atomic_completion_with_stale_state(self) -> None:
        task = DownloadTask(
            "service-task",
            "https://example.test/watch/service",
            str(self.root),
            status="downloading",
        )
        self.db.upsert_download_task(task)
        media = MediaItem(
            source_url=task.url,
            title="已校验视频",
            video_path=str(self.root / "service.mp4"),
        )
        completion = DownloadTask(
            task.id,
            task.url,
            task.output_dir,
            status="completed",
            progress=100.0,
            media_path=media.video_path,
        )
        self.db.complete_download_task(completion, media)
        service = DownloadService(self.db)
        service.tasks[task.id] = task

        with patch.object(self.db, "upsert_download_task") as stale_write:
            service._on_media_completed(task.id, media)

        stale_write.assert_not_called()
        self.assertEqual(task.status, "completed")
        self.assertEqual(task.progress, 100.0)
        self.assertEqual(self.db.list_download_tasks()[0]["status"], "completed")
        service.shutdown(timeout_ms=0)

    def test_delete_paused_task_with_files_removes_ytdlp_partial_output_family(self) -> None:
        base = self.root / "Large Video [video-id]"
        current = base.with_name(base.name + ".f702.mp4")
        related = (
            current.with_name(current.name + ".part"),
            base.with_name(base.name + ".f251.webm"),
            base.with_name(base.name + ".info.json"),
            base.with_name(base.name + ".webp"),
            base.with_name(base.name + ".temp.mp4"),
        )
        for path in related:
            path.write_bytes(b"partial")
        unrelated = self.root / "Other Video [other-id].f702.mp4.part"
        unrelated.write_bytes(b"keep")

        task = DownloadTask(
            "delete-partial",
            "https://example.test/watch/video-id",
            str(self.root),
            status="paused",
            current_filename=str(current),
        )
        self.db.upsert_download_task(task)
        service = DownloadService(self.db)
        service.logs = DownloadLogService(self.root / "logs")
        service.tasks[task.id] = task

        self.assertTrue(service.delete_task(task.id, delete_files=True))

        self.assertTrue(all(not path.exists() for path in related))
        self.assertTrue(unrelated.is_file())
        self.assertEqual(self.db.list_download_tasks(), [])
        service.shutdown(timeout_ms=0)

    def test_partial_artifact_scan_uses_task_subdirectory_and_strict_names(self) -> None:
        task_dir = self.root / "One Task"
        task_dir.mkdir()
        base = task_dir / "Nested Video [video-id]"
        current = base.with_name(base.name + ".f702.mp4")
        managed = {
            current.with_name(current.name + ".part"),
            base.with_name(base.name + ".f251.webm"),
            base.with_name(base.name + ".temp.mp4"),
            base.with_name(base.name + ".info.json"),
            base.with_name(base.name + ".zh-CN.vtt"),
        }
        user_files = {
            base.with_name(base.name + ".family.jpg"),
            base.with_name(base.name + ".favorite.mp4"),
            base.with_name(base.name + ".personal-note.txt"),
            base.with_name(base.name + ".custom.json"),
        }
        root_decoy = self.root / (base.name + ".f251.webm")
        for path in (*managed, *user_files, root_decoy):
            path.write_bytes(b"data")
        task = DownloadTask(
            "nested-partial-family",
            "https://example.test/watch/video-id",
            str(self.root),
            status="paused",
            current_filename=str(current),
            options_json={"organize_task_folder": True},
        )

        artifacts = task_download_artifact_paths(task)

        self.assertTrue(
            all(
                any(
                    actual.exists() and os.path.samefile(expected, actual)
                    for actual in artifacts
                )
                for expected in managed
            )
        )
        self.assertTrue(
            all(
                not any(
                    actual.exists() and os.path.samefile(user_file, actual)
                    for actual in artifacts
                )
                for user_file in user_files
            )
        )
        self.assertFalse(
            any(
                actual.exists() and os.path.samefile(root_decoy, actual)
                for actual in artifacts
            )
        )

    def test_delete_partial_task_preserves_same_prefix_user_files(self) -> None:
        base = self.root / "User Files [video-id]"
        current = base.with_name(base.name + ".f137.mp4")
        managed = (
            current.with_name(current.name + ".part"),
            base.with_name(base.name + ".f140.m4a"),
        )
        user_files = (
            base.with_name(base.name + ".family.jpg"),
            base.with_name(base.name + ".favorite.mp4"),
            base.with_name(base.name + ".personal-note.txt"),
        )
        for path in (*managed, *user_files):
            path.write_bytes(b"data")
        task = DownloadTask(
            "strict-partial-delete",
            "https://example.test/watch/video-id",
            str(self.root),
            status="paused",
            current_filename=str(current),
        )
        self.db.upsert_download_task(task)
        service = DownloadService(self.db)
        service.logs = DownloadLogService(self.root / "logs")
        service.tasks[task.id] = task

        self.assertTrue(service.delete_task(task.id, delete_files=True))

        self.assertTrue(all(not path.exists() for path in managed))
        self.assertTrue(all(path.is_file() for path in user_files))
        service.shutdown(timeout_ms=0)

    def test_delete_partial_task_rejects_thumbnail_outside_output_root(self) -> None:
        outside = self.root.parent / f"{self.root.name}-external-thumbnail.jpg"
        outside.write_bytes(b"user-image")
        self.addCleanup(outside.unlink, missing_ok=True)
        task = DownloadTask(
            "outside-partial-thumbnail",
            "https://example.test/watch/outside-thumbnail",
            str(self.root),
            status="paused",
            thumbnail_path=str(outside),
        )
        self.db.upsert_download_task(task)
        service = DownloadService(self.db)
        service.logs = DownloadLogService(self.root / "logs")
        service.tasks[task.id] = task

        self.assertTrue(service.delete_task(task.id, delete_files=True))

        self.assertEqual(outside.read_bytes(), b"user-image")
        self.assertEqual(self.db.list_download_tasks(), [])
        service.shutdown(timeout_ms=0)

    def test_delete_partial_thumbnail_symlink_unlinks_only_the_link(self) -> None:
        outside = self.root.parent / f"{self.root.name}-thumbnail-target.jpg"
        outside.write_bytes(b"user-image")
        self.addCleanup(outside.unlink, missing_ok=True)
        link = self.root / "preview.thumb.jpg"
        try:
            link.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"file symlinks are unavailable: {exc}")
        task = DownloadTask(
            "partial-thumbnail-symlink",
            "https://example.test/watch/thumbnail-link",
            str(self.root),
            status="paused",
            thumbnail_path=str(link),
        )
        self.db.upsert_download_task(task)
        service = DownloadService(self.db)
        service.logs = DownloadLogService(self.root / "logs")
        service.tasks[task.id] = task

        self.assertTrue(service.delete_task(task.id, delete_files=True))

        self.assertFalse(link.exists())
        self.assertFalse(link.is_symlink())
        self.assertEqual(outside.read_bytes(), b"user-image")
        service.shutdown(timeout_ms=0)

    def test_completed_task_deletion_uses_exact_file_manifest(self) -> None:
        media_path = self.root / "same-prefix.mp4"
        cover_path = self.root / "same-prefix.jpg"
        subtitle_path = self.root / "same-prefix.zh-CN.vtt"
        unrelated = self.root / "same-prefix.personal-note.txt"
        for path in (media_path, cover_path, subtitle_path, unrelated):
            path.write_bytes(b"data")
        task = DownloadTask(
            "manifest-delete",
            "https://example.test/watch/manifest",
            str(self.root),
            status="downloading",
        )
        self.db.upsert_download_task(task)
        item = MediaItem(
            source_url=task.url,
            title="manifest",
            video_path=str(media_path),
            thumbnail_path=str(cover_path),
        )
        completion = DownloadTask(
            task.id,
            task.url,
            task.output_dir,
            status="completed",
            media_path=str(media_path),
            thumbnail_path=str(cover_path),
        )
        self.db.complete_download_task_batch(completion, [item], [
            (str(media_path), "media", True),
            (str(cover_path), "thumbnail", True),
            (str(subtitle_path), "subtitle", True),
        ])
        completion.options_json = task.options_json
        service = DownloadService(self.db)
        service.logs = DownloadLogService(self.root / "logs")
        service.tasks[task.id] = completion

        self.assertTrue(service.delete_task(task.id, delete_files=True))

        self.assertFalse(media_path.exists())
        self.assertFalse(cover_path.exists())
        self.assertFalse(subtitle_path.exists())
        self.assertTrue(unrelated.exists())
        service.shutdown(timeout_ms=0)

    def test_completed_manifest_ignores_same_stem_user_files(self) -> None:
        media_path = self.root / "owned.mp4"
        expected = {
            media_path: "media",
            self.root / "owned.webp": "thumbnail",
            self.root / "owned.info.json": "info_json",
            self.root / "owned.description": "description",
            self.root / "owned.zh-CN.vtt": "subtitle",
        }
        unrelated = (
            self.root / "owned.personal-note.txt",
            self.root / "owned.custom.json",
            self.root / "owned.notes.xml",
            self.root / "owned.personal.jpg",
            self.root / "owned.en.srt",
            self.root / "owned.zh-CN.notes.vtt",
        )
        for path in (*expected, *unrelated):
            path.write_bytes(b"data")
        outside = self.root.parent / f"{self.root.name}-outside.jpg"
        outside.write_bytes(b"outside")
        self.addCleanup(outside.unlink, missing_ok=True)

        manifest = completed_task_file_manifest(
            str(self.root),
            [MediaItem(
                source_url="https://example.test/watch/owned",
                video_path=str(media_path),
                thumbnail_path=str(outside),
            )],
            DownloadOptions(
                write_thumbnail=True,
                write_description=True,
                write_info_json=True,
            ),
            subtitle_language="zh-CN",
        )

        manifest_by_kind = {
            kind: Path(path)
            for path, kind, managed in manifest
            if managed
        }
        expected_by_kind = {kind: path for path, kind in expected.items()}
        self.assertEqual(set(manifest_by_kind), set(expected_by_kind))
        for kind, expected_path in expected_by_kind.items():
            self.assertTrue(os.path.samefile(manifest_by_kind[kind], expected_path))
        self.assertTrue(
            all(
                not any(
                    os.path.samefile(path, manifest_path)
                    for manifest_path in manifest_by_kind.values()
                )
                for path in unrelated
            )
        )
        self.assertFalse(
            any(
                os.path.samefile(outside, manifest_path)
                for manifest_path in manifest_by_kind.values()
            )
        )

    def test_completed_entry_does_not_claim_generic_json_as_info_metadata(self) -> None:
        media_path = self.root / "metadata.mp4"
        generic_json = self.root / "metadata.json"
        media_path.write_bytes(b"media")
        generic_json.write_text('{"user": "owned"}', encoding="utf-8")
        worker = DownloadWorker(
            "metadata-paths",
            "https://example.test/watch/metadata",
            str(self.root),
            self.db,
        )

        paths = worker._completed_entry_paths(
            {"filepath": str(media_path)},
            lambda _entry: str(media_path),
        )

        self.assertIsNone(paths.info_json)
        self.assertTrue(generic_json.is_file())

    def test_manifest_deletion_unlinks_in_root_symlink_without_deleting_target(self) -> None:
        media_path = self.root / "symlink-media.mp4"
        media_path.write_bytes(b"media")
        outside = self.root.parent / f"{self.root.name}-outside-cover.jpg"
        outside.write_bytes(b"user-file")
        self.addCleanup(outside.unlink, missing_ok=True)
        link = self.root / "symlink-media.jpg"
        try:
            link.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"file symlinks are unavailable: {exc}")

        task = DownloadTask(
            "manifest-symlink",
            "https://example.test/watch/symlink",
            str(self.root),
            status="downloading",
            media_path=str(media_path),
        )
        item = MediaItem(
            source_url=task.url,
            video_path=str(media_path),
            thumbnail_path=str(link),
        )
        manifest = completed_task_file_manifest(
            str(self.root),
            [item],
            DownloadOptions(),
        )
        self.db.upsert_download_task(task)
        task.status = "completed"
        self.db.complete_download_task_batch(task, [item], manifest)
        service = DownloadService(self.db)
        service.logs = DownloadLogService(self.root / "logs")
        service.tasks[task.id] = task

        self.assertTrue(service.delete_task(task.id, delete_files=True))

        self.assertFalse(media_path.exists())
        self.assertFalse(link.exists())
        self.assertFalse(link.is_symlink())
        self.assertEqual(outside.read_bytes(), b"user-file")
        service.shutdown(timeout_ms=0)

    def test_filename_template_rejects_directory_escape_forms(self) -> None:
        self.assertEqual(
            validate_filename_template("%(title)s [%(id)s].%(ext)s"),
            "%(title)s [%(id)s].%(ext)s",
        )
        for unsafe in (
            "../outside/%(title)s.%(ext)s",
            "C:\\outside\\%(title)s.%(ext)s",
            "\\\\server\\share\\%(title)s.%(ext)s",
            "/absolute/%(title)s.%(ext)s",
        ):
            with self.subTest(unsafe=unsafe), self.assertRaises(ValueError):
                validate_filename_template(unsafe)

    def test_deleted_task_is_not_recreated_by_pending_progress_flush(self) -> None:
        task = DownloadTask(
            "delete-pending-write",
            "https://example.test/watch/pending",
            str(self.root),
            status="paused",
        )
        self.db.upsert_download_task(task)
        service = DownloadService(self.db)
        service.logs = DownloadLogService(self.root / "logs")
        service.tasks[task.id] = task
        service._progress_persistence.pending[task.id] = task

        self.assertTrue(service.delete_task(task.id))
        service._flush_progress_persists()

        self.assertEqual(self.db.list_download_tasks(), [])
        service.shutdown(timeout_ms=0)

    def test_deleting_nested_collection_forgets_its_full_in_memory_subtree(self) -> None:
        root = DownloadTask(
            "service-root",
            "https://example.test/collection/root",
            str(self.root),
            task_kind="collection",
            root_task_id="service-root",
            status="completed",
        )
        nested = DownloadTask(
            "service-nested",
            "https://example.test/collection/nested",
            str(self.root),
            task_kind="collection",
            parent_task_id=root.id,
            root_task_id=root.id,
            collection_index=1,
            status="completed",
        )
        grandchild = DownloadTask(
            "service-grandchild",
            "https://example.test/watch/grandchild",
            str(self.root),
            parent_task_id=nested.id,
            root_task_id=root.id,
            collection_index=1,
            status="completed",
        )
        sibling = DownloadTask(
            "service-sibling",
            "https://example.test/watch/sibling",
            str(self.root),
            parent_task_id=root.id,
            root_task_id=root.id,
            collection_index=2,
            status="completed",
        )
        self.db.upsert_download_tasks((root, nested, grandchild, sibling))
        service = DownloadService(self.db)
        for task in (root, nested, grandchild, sibling):
            service._register_task(task)
        deleted: list[str] = []
        service.task_deleted.connect(deleted.append)

        self.assertTrue(service.delete_task(nested.id))

        self.assertEqual(set(deleted), {nested.id, grandchild.id})
        self.assertEqual(set(service.tasks), {root.id, sibling.id})
        self.assertEqual(
            {task.id for task in service.collection_children(root.id)},
            {sibling.id},
        )
        self.assertEqual(service.collection_children(nested.id), [])
        self.assertEqual(
            {row["id"] for row in self.db.list_download_tasks()},
            {root.id, sibling.id},
        )
        service.shutdown(timeout_ms=0)

    def test_missing_database_row_during_thread_finish_removes_memory_ghost(self) -> None:
        task = DownloadTask(
            "missing-row-at-thread-finish",
            "https://example.test/missing-row-at-thread-finish",
            str(self.root),
            status="downloading",
        )
        self.db.upsert_download_task(task)
        service = DownloadService(self.db)
        service._register_task(task)
        service.workers[task.id] = object()  # type: ignore[assignment]
        service.threads[task.id] = object()  # type: ignore[assignment]
        deleted: list[str] = []
        service.task_deleted.connect(deleted.append)
        self.db.delete_download_task(task.id)

        service._thread_finished(task.id)

        self.assertNotIn(task.id, service.tasks)
        self.assertNotIn(task.id, service.workers)
        self.assertNotIn(task.id, service.threads)
        self.assertEqual(deleted, [task.id])
        self.assertEqual(self.db.list_download_tasks(), [])
        self.assertEqual(service.task_statistics()["total"], 0)
        service.shutdown(timeout_ms=0)

    def test_final_state_persist_failure_does_not_hold_download_slot(self) -> None:
        task = DownloadTask(
            "final-state-persist-failure",
            "https://example.test/final-state-persist-failure",
            str(self.root),
            status="downloading",
        )
        self.db.upsert_download_task(task)
        service = DownloadService(self.db)
        service._register_task(task)
        service.workers[task.id] = object()  # type: ignore[assignment]
        service.threads[task.id] = object()  # type: ignore[assignment]
        finished: list[tuple[str, str]] = []
        service.task_finished.connect(
            lambda task_id, status, _error: finished.append((task_id, status))
        )

        with patch.object(
            service,
            "_persist",
            side_effect=sqlite3.OperationalError("database busy"),
        ):
            service._thread_finished(task.id)

        self.assertEqual(task.status, "failed")
        self.assertNotIn(task.id, service.workers)
        self.assertNotIn(task.id, service.threads)
        self.assertEqual(finished, [(task.id, "failed")])
        self.assertEqual(service._task_index.states[task.id][1], "failed")
        service.shutdown(timeout_ms=0)

    def test_database_delete_failure_keeps_task_queue_pending_write_and_files(self) -> None:
        partial = self.root / "Delete Retry [delete-retry].f137.mp4.part"
        partial.write_bytes(b"partial")
        task = DownloadTask(
            "delete-retry",
            "https://example.test/watch/delete-retry",
            str(self.root),
            status="paused",
            current_filename=str(partial.with_suffix("")),
        )
        self.db.upsert_download_task(task)
        service = DownloadService(self.db)
        service.logs = DownloadLogService(self.root / "logs")
        service._register_task(task)
        service.queue.append(task.id)
        service._progress_persistence.pending[task.id] = task
        deleted_signals: list[str] = []
        service.task_deleted.connect(deleted_signals.append)

        with patch.object(
            self.db,
            "delete_download_task",
            side_effect=RuntimeError("database busy"),
        ):
            with self.assertRaisesRegex(RuntimeError, "database busy"):
                service.delete_task(task.id, delete_files=True)

        self.assertIs(service.tasks[task.id], task)
        self.assertIn(task.id, service.queue)
        self.assertIs(service._progress_persistence.pending[task.id], task)
        self.assertTrue(partial.is_file())
        self.assertEqual([row["id"] for row in self.db.list_download_tasks()], [task.id])
        self.assertEqual(deleted_signals, [])

        original_delete = self.db.delete_download_task

        def committed_delete(*args, **kwargs):
            self.assertTrue(partial.is_file())
            return original_delete(*args, **kwargs)

        with patch.object(self.db, "delete_download_task", side_effect=committed_delete):
            self.assertTrue(service.delete_task(task.id, delete_files=True))

        self.assertNotIn(task.id, service.tasks)
        self.assertNotIn(task.id, service.queue)
        self.assertNotIn(task.id, service._progress_persistence.pending)
        self.assertFalse(partial.exists())
        self.assertEqual(self.db.list_download_tasks(), [])
        self.assertEqual(deleted_signals, [task.id])
        service.shutdown(timeout_ms=0)

    def test_reset_task_cache_clears_all_task_derived_timers_and_queues(self) -> None:
        task = DownloadTask(
            "reset-derived-state",
            "https://example.test/reset-derived-state",
            str(self.root),
            status="paused",
        )
        service = DownloadService(self.db)
        service._register_task(task)
        service.queue.append(task.id)
        service._progress_persistence.pending[task.id] = task
        service._pending_collection_refreshes.add("old-parent")
        service._pending_collection_deletes["old-parent"] = True
        service._collection_delete_root_by_child[task.id] = "old-parent"
        service._discard_tasks.add(task.id)
        service._pending_deletes[task.id] = True
        service._pending_runtime_retries.add(task.id)
        deferred_thread = QThread()
        service._deferred_thread_finishes.add(deferred_thread)
        service._materialization_parents.append("old-parent")
        service._deferred_restore_rows.append({"id": "stale-row"})
        service._restore_refresh_parents.add("old-parent")
        service._restore_latest_media[task.url] = MediaItem(video_path="stale.mp4")
        service._last_progress_emit[task.id] = 1.0
        service._progress_persistence.persisted_at[task.id] = 1.0
        service._progress_flush_timer.start(10_000)
        service._collection_refresh_timer.start(10_000)
        service._materialization_timer.start(10_000)
        service._restore_timer.start(10_000)

        service.reset_task_cache()
        QCoreApplication.processEvents()

        self.assertEqual(service.tasks, {})
        self.assertEqual(list(service.queue), [])
        self.assertEqual(service._progress_persistence.pending, {})
        self.assertEqual(service._pending_collection_refreshes, set())
        self.assertEqual(service._pending_collection_deletes, {})
        self.assertEqual(service._collection_delete_root_by_child, {})
        self.assertEqual(service._discard_tasks, set())
        self.assertEqual(service._pending_deletes, {})
        self.assertEqual(service._pending_runtime_retries, set())
        self.assertEqual(service._deferred_thread_finishes, set())
        self.assertEqual(list(service._materialization_parents), [])
        self.assertEqual(list(service._deferred_restore_rows), [])
        self.assertEqual(service._restore_refresh_parents, set())
        self.assertEqual(service._restore_latest_media, {})
        self.assertEqual(service._last_progress_emit, {})
        self.assertEqual(service._progress_persistence.persisted_at, {})
        self.assertFalse(service._progress_flush_timer.isActive())
        self.assertFalse(service._collection_refresh_timer.isActive())
        self.assertFalse(service._materialization_timer.isActive())
        self.assertFalse(service._restore_timer.isActive())
        deferred_thread.deleteLater()
        QCoreApplication.processEvents()
        service.shutdown(timeout_ms=0)

    def test_restore_self_heals_one_invalid_options_row_without_losing_other_tasks(self) -> None:
        good = DownloadTask(
            "restore-good-row",
            "https://example.test/restore-good-row",
            str(self.root),
            status="paused",
        )
        damaged = DownloadTask(
            "restore-invalid-options",
            "https://example.test/restore-invalid-options",
            str(self.root),
            status="paused",
        )
        self.db.upsert_download_tasks([good, damaged])
        with self.db._lock:
            self.db.conn.execute(
                "UPDATE download_tasks SET options_json='{' WHERE id=?",
                (damaged.id,),
            )
            self.db.conn.commit()
        service = DownloadService(self.db)
        service._start_next = lambda: None  # type: ignore[method-assign]

        restored = service.restore_tasks()

        self.assertEqual({task.id for task in restored}, {good.id, damaged.id})
        self.assertEqual(service.tasks[good.id].status, "paused")
        repaired = service.tasks[damaged.id]
        self.assertEqual(repaired.status, "failed")
        self.assertIn("options_json", repaired.error)
        row = next(row for row in self.db.list_download_tasks() if row["id"] == damaged.id)
        self.assertEqual(row["status"], "failed")
        self.assertIsInstance(__import__("json").loads(row["options_json"]), dict)
        service.shutdown(timeout_ms=0)

    def test_restore_normalizes_invalid_numeric_fields_instead_of_aborting(self) -> None:
        task = DownloadTask(
            "restore-invalid-numbers",
            "https://example.test/restore-invalid-numbers",
            str(self.root),
            status="paused",
        )
        self.db.upsert_download_task(task)
        with self.db._lock:
            self.db.conn.execute(
                """UPDATE download_tasks
                SET collection_index='bad', progress='bad', speed_bps='nan',
                    downloaded_bytes='bad', total_bytes=-1
                WHERE id=?""",
                (task.id,),
            )
            self.db.conn.commit()
        service = DownloadService(self.db)
        service._start_next = lambda: None  # type: ignore[method-assign]

        restored = service.restore_tasks()

        self.assertEqual(len(restored), 1)
        repaired = restored[0]
        self.assertEqual(repaired.status, "failed")
        self.assertEqual(repaired.collection_index, 0)
        self.assertEqual(repaired.progress, 0.0)
        self.assertEqual(repaired.speed_bps, 0.0)
        self.assertEqual(repaired.downloaded_bytes, 0)
        self.assertEqual(repaired.total_bytes, 0)
        self.assertIn("collection_index", repaired.error)
        self.assertIn("progress", repaired.error)
        service.shutdown(timeout_ms=0)

    def test_restore_rejects_invalid_nested_option_shapes(self) -> None:
        task = DownloadTask(
            "restore-invalid-option-shape",
            "https://example.test/restore-invalid-option-shape",
            str(self.root),
            status="paused",
        )
        self.db.upsert_download_task(task)
        with self.db._lock:
            self.db.conn.execute(
                "UPDATE download_tasks SET options_json=? WHERE id=?",
                ('{"sponsorblock_categories": 1}', task.id),
            )
            self.db.conn.commit()
        service = DownloadService(self.db)
        service._start_next = lambda: None  # type: ignore[method-assign]

        restored = service.restore_tasks()

        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0].status, "failed")
        self.assertIn("options_json", restored[0].error)
        service.shutdown(timeout_ms=0)

    def test_restore_promotes_orphan_child_and_prevents_hidden_download(self) -> None:
        orphan = DownloadTask(
            "restore-orphan-child",
            "https://example.test/restore-orphan-child",
            str(self.root),
            parent_task_id="missing-parent",
            root_task_id="missing-parent",
            status="queued",
        )
        self.db.upsert_download_task(orphan)
        service = DownloadService(self.db)
        service._start_next = lambda: None  # type: ignore[method-assign]

        restored = service.restore_tasks()

        self.assertEqual(len(restored), 1)
        repaired = restored[0]
        self.assertEqual(repaired.parent_task_id, "")
        self.assertEqual(repaired.root_task_id, "")
        self.assertEqual(repaired.status, "failed")
        self.assertIn("父合集记录不存在", repaired.error)
        self.assertEqual(list(service.queue), [])
        row = self.db.list_download_tasks()[0]
        self.assertEqual(row["parent_task_id"], "")
        self.assertEqual(row["status"], "failed")
        service.shutdown(timeout_ms=0)

    def test_restore_breaks_collection_parent_cycle_and_keeps_tasks_visible(self) -> None:
        first = DownloadTask(
            "restore-cycle-a",
            "https://example.test/restore-cycle-a",
            str(self.root),
            task_kind="collection",
            parent_task_id="restore-cycle-b",
            root_task_id="restore-cycle-a",
            status="parsing_collection",
        )
        second = DownloadTask(
            "restore-cycle-b",
            "https://example.test/restore-cycle-b",
            str(self.root),
            task_kind="collection",
            parent_task_id="restore-cycle-a",
            root_task_id="restore-cycle-a",
            status="parsing_collection",
        )
        self.db.upsert_download_tasks([first, second])
        service = DownloadService(self.db)
        service._start_next = lambda: None  # type: ignore[method-assign]

        restored = service.restore_tasks()

        self.assertEqual({task.id for task in restored}, {first.id, second.id})
        for task in restored:
            self.assertEqual(task.parent_task_id, "")
            self.assertEqual(task.root_task_id, task.id)
            self.assertEqual(task.status, "failed")
            self.assertIn("循环引用", task.error)
        self.assertEqual(service.task_statistics(top_level_only=True)["total"], 2)
        service.shutdown(timeout_ms=0)

    def test_restore_continues_when_repair_persist_and_log_both_fail(self) -> None:
        interrupted = DownloadTask(
            "restore-write-failure",
            "https://example.test/restore-write-failure",
            str(self.root),
            status="downloading",
        )
        healthy = DownloadTask(
            "restore-write-healthy",
            "https://example.test/restore-write-healthy",
            str(self.root),
            status="paused",
        )
        self.db.upsert_download_tasks([interrupted, healthy])
        service = DownloadService(self.db)
        service._start_next = lambda: None  # type: ignore[method-assign]

        with patch.object(
            self.db,
            "update_download_task",
            side_effect=sqlite3.OperationalError("read only"),
        ), patch.object(
            service.logs,
            "write",
            side_effect=PermissionError("log locked"),
        ):
            restored = service.restore_tasks()

        self.assertEqual({task.id for task in restored}, {interrupted.id, healthy.id})
        self.assertEqual(service.tasks[interrupted.id].status, "paused")
        self.assertEqual(service.tasks[healthy.id].status, "paused")
        service.shutdown(timeout_ms=0)

    def test_restore_marks_completed_video_deleted_when_no_media_file_can_be_found(self) -> None:
        task = DownloadTask(
            "restore-missing-completed-media",
            "https://example.test/restore-missing-completed-media",
            str(self.root),
            status="completed",
            progress=100,
            media_path="",
        )
        self.db.upsert_download_task(task)
        service = DownloadService(self.db)
        service._start_next = lambda: None  # type: ignore[method-assign]

        restored = service.restore_tasks()

        self.assertEqual(restored[0].status, "deleted")
        row = self.db.list_download_tasks()[0]
        self.assertEqual(row["status"], "deleted")
        service.shutdown(timeout_ms=0)

    def test_restore_recovers_deleted_video_from_existing_media_catalog_file(self) -> None:
        video = self.root / "recovered.mp4"
        thumbnail = self.root / "recovered.jpg"
        video.write_bytes(b"video")
        thumbnail.write_bytes(b"cover")
        task = DownloadTask(
            "restore-recovered-media",
            "https://example.test/restore-recovered-media",
            str(self.root),
            status="deleted",
            progress=100,
            media_path="",
        )
        self.db.upsert_download_task(task)
        self.db.add_media(MediaItem(
            source_url=task.url,
            video_path=str(video),
            thumbnail_path=str(thumbnail),
        ))
        service = DownloadService(self.db)
        service._start_next = lambda: None  # type: ignore[method-assign]

        restored = service.restore_tasks()

        recovered = restored[0]
        self.assertEqual(recovered.status, "completed")
        self.assertEqual(recovered.media_path, str(video))
        self.assertEqual(recovered.thumbnail_path, str(thumbnail))
        row = self.db.list_download_tasks()[0]
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["media_path"], str(video))
        service.shutdown(timeout_ms=0)

    def test_restore_survives_corrupt_media_catalog_tags(self) -> None:
        video = self.root / "corrupt-tags.mp4"
        video.write_bytes(b"video")
        task = DownloadTask(
            "restore-corrupt-media-tags",
            "https://example.test/restore-corrupt-media-tags",
            str(self.root),
            status="completed",
            progress=100,
            media_path="",
        )
        self.db.upsert_download_task(task)
        media_id = self.db.add_media(MediaItem(
            source_url=task.url,
            video_path=str(video),
            tags=["valid-before-corruption"],
        ))
        with self.db._lock:
            self.db.conn.execute(
                "UPDATE media_items SET tags='{' WHERE id=?",
                (media_id,),
            )
            self.db.conn.commit()
        service = DownloadService(self.db)
        service._start_next = lambda: None  # type: ignore[method-assign]

        restored = service.restore_tasks()

        self.assertEqual(restored[0].status, "completed")
        self.assertEqual(restored[0].media_path, str(video))
        self.assertEqual(self.db.get_media(media_id).tags, [])
        self.assertEqual(self.db.list_media()[0].tags, [])
        service.shutdown(timeout_ms=0)

    def test_restore_does_not_treat_media_directory_as_completed_file(self) -> None:
        media_directory = self.root / "not-a-video"
        media_directory.mkdir()
        task = DownloadTask(
            "restore-media-directory",
            "https://example.test/restore-media-directory",
            str(self.root),
            status="completed",
            progress=100,
            media_path=str(media_directory),
        )
        self.db.upsert_download_task(task)
        service = DownloadService(self.db)
        service._start_next = lambda: None  # type: ignore[method-assign]

        restored = service.restore_tasks()

        self.assertEqual(restored[0].status, "deleted")
        service.shutdown(timeout_ms=0)

    def test_discarded_active_task_is_not_recreated_by_pending_progress_flush(self) -> None:
        task = DownloadTask(
            "discard-pending-progress",
            "https://example.test/discard-pending-progress",
            str(self.root),
            status="downloading",
            progress=60,
        )
        self.db.upsert_download_task(task)
        service = DownloadService(self.db)
        service._register_task(task)
        service._progress_persistence.pending[task.id] = task
        service._discard_tasks.add(task.id)

        service._thread_finished(task.id)
        service._flush_progress_persists()

        self.assertNotIn(task.id, service.tasks)
        self.assertFalse(any(row["id"] == task.id for row in self.db.list_download_tasks()))
        service.shutdown(timeout_ms=0)

    def test_failed_signal_keeps_status_and_stage_consistent_before_cleanup(self) -> None:
        task = DownloadTask(
            "failed-terminal-state",
            "https://example.test/failed-terminal-state",
            str(self.root),
            status="downloading",
        )
        self.db.upsert_download_task(task)
        service = DownloadService(self.db)
        service._register_task(task)

        service._on_failed(task.id, "network failed")

        self.assertEqual(task.status, "failed")
        self.assertEqual(task.stage, "failed")
        row = next(row for row in self.db.list_download_tasks() if row["id"] == task.id)
        self.assertEqual(row["status"], "failed")
        service._thread_finished(task.id)
        self.assertEqual(task.status, "failed")
        service.shutdown(timeout_ms=0)

    def test_thread_finish_is_idempotent_for_one_worker_run(self) -> None:
        task = DownloadTask(
            "idempotent-thread-finish",
            "https://example.test/idempotent-thread-finish",
            str(self.root),
            status="downloading",
        )
        self.db.upsert_download_task(task)
        service = DownloadService(self.db)
        service._register_task(task)
        finished: list[tuple[str, str, str]] = []
        service.task_finished.connect(
            lambda task_id, status, error: finished.append((task_id, status, error))
        )

        service._thread_finished(task.id)
        service._thread_finished(task.id)

        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0][:2], (task.id, "failed"))
        self.assertIn("未收到完成或失败结果", finished[0][2])
        service.shutdown(timeout_ms=0)

    def test_canceling_manual_format_selection_stays_canceled_after_thread_exit(self) -> None:
        task = DownloadTask(
            "manual-format-canceled",
            "https://example.test/manual-format-canceled",
            str(self.root),
            status="waiting_selection",
        )
        self.db.upsert_download_task(task)
        service = DownloadService(self.db)
        service._register_task(task)
        selections: list[tuple[str, str, str]] = []
        service.workers[task.id] = SimpleNamespace(
            set_format_selector=lambda selector, *, content_mode, audio_format: selections.append(
                (selector, content_mode, audio_format)
            )
        )

        applied = service.set_format_selection(task.id, {"selector": ""})
        service._thread_finished(task.id)

        self.assertTrue(applied)
        self.assertEqual(selections, [("", "", "")])
        self.assertEqual(task.status, "canceled")
        self.assertEqual(task.stage, "canceled")
        self.assertEqual(task.error, "")
        row = next(row for row in self.db.list_download_tasks() if row["id"] == task.id)
        self.assertEqual(row["status"], "canceled")
        service.shutdown(timeout_ms=0)

    def test_manual_format_selection_reports_expired_worker_without_mutation(self) -> None:
        task = DownloadTask(
            "manual-format-expired",
            "https://example.test/manual-format-expired",
            str(self.root),
            status="waiting_selection",
            stage="waiting_selection",
        )
        self.db.upsert_download_task(task)
        service = DownloadService(self.db)
        service._register_task(task)

        applied = service.set_format_selection(task.id, {
            "selector": "137+bestaudio",
            "content_mode": "video",
        })

        self.assertFalse(applied)
        self.assertEqual(task.status, "waiting_selection")
        self.assertEqual(task.stage, "waiting_selection")
        self.assertEqual(task.format_selector, "")
        service.shutdown(timeout_ms=0)

    def test_worker_exit_without_outcome_is_persisted_as_failure_not_success(self) -> None:
        task = DownloadTask(
            "worker-exit-without-outcome",
            "https://example.test/worker-exit-without-outcome",
            str(self.root),
            status="downloading",
        )
        self.db.upsert_download_task(task)
        service = DownloadService(self.db)
        service._register_task(task)
        service.workers[task.id] = object()  # type: ignore[assignment]

        service._thread_finished(task.id)

        self.assertEqual(task.status, "failed")
        self.assertEqual(task.stage, "failed")
        self.assertIn("未收到完成或失败结果", task.error)
        row = next(row for row in self.db.list_download_tasks() if row["id"] == task.id)
        self.assertEqual(row["status"], "failed")
        self.assertNotIn(task.id, service.workers)
        service.shutdown(timeout_ms=0)

    def test_completed_signal_wins_over_shutdown_pause_request(self) -> None:
        task = DownloadTask(
            "completed-during-shutdown",
            "https://example.test/completed-during-shutdown",
            str(self.root),
            status="downloading",
            pause_requested=True,
            stage="reconnecting",
            stage_progress=40,
            reconnect_message="2 秒后重试",
        )
        self.db.upsert_download_task(task)
        service = DownloadService(self.db)
        service._register_task(task)

        service._on_media_completed(task.id, MediaItem(
            source_url=task.url,
            title="Completed",
            video_path=str(self.root / "completed.mp4"),
        ))
        service._thread_finished(task.id)

        self.assertEqual(task.status, "completed")
        self.assertEqual(task.stage, "completed")
        self.assertFalse(task.pause_requested)
        self.assertFalse(task.cancel_requested)
        self.assertEqual(task.stage_progress, 100.0)
        self.assertEqual(task.reconnect_message, "")
        service.shutdown(timeout_ms=0)

    def test_paused_finish_clears_stale_reconnect_presentation(self) -> None:
        task = DownloadTask(
            "pause-clears-reconnect",
            "https://example.test/pause-clears-reconnect",
            str(self.root),
            status="downloading",
            pause_requested=True,
            stage="reconnecting",
            stage_progress=65,
            reconnect_message="5 秒后重试",
        )
        self.db.upsert_download_task(task)
        service = DownloadService(self.db)
        service._register_task(task)

        service._thread_finished(task.id)

        self.assertEqual(task.status, "paused")
        self.assertEqual(task.stage_progress, 0.0)
        self.assertEqual(task.reconnect_message, "")
        service.shutdown(timeout_ms=0)

    def test_late_old_worker_signals_do_not_mutate_replacement_runtime(self) -> None:
        task = DownloadTask(
            "stale-worker-signals",
            "https://example.test/stale-worker-signals",
            str(self.root),
            title="Replacement task",
            status="downloading",
            progress=10,
        )
        self.db.upsert_download_task(task)
        service = DownloadService(self.db)
        service._register_task(task)
        old_worker = object()
        replacement_worker = object()
        service.workers[task.id] = replacement_worker  # type: ignore[assignment]

        with patch.object(DownloadService, "sender", return_value=old_worker):
            service._on_progress(task.id, {
                "downloaded_bytes": 90,
                "total_bytes": 100,
            })
            service._on_failed(task.id, "old worker failed")
            service._on_media_completed(task.id, MediaItem(
                source_url=task.url,
                title="Old result",
                video_path="old.mp4",
            ))

        self.assertEqual(task.status, "downloading")
        self.assertEqual(task.progress, 10)
        self.assertEqual(task.title, "Replacement task")
        self.assertEqual(task.media_path, "")
        service.workers.clear()
        service.shutdown(timeout_ms=0)

    def test_format_ready_database_busy_still_opens_selection_and_retries(self) -> None:
        task = DownloadTask(
            "format-ready-database-busy",
            "https://example.test/format-ready-database-busy",
            str(self.root),
            status="downloading",
        )
        self.db.upsert_download_task(task)
        service = DownloadService(self.db)
        service._register_task(task)
        emitted: list[tuple[str, dict]] = []
        service.formats_ready.connect(
            lambda task_id, payload: emitted.append((task_id, dict(payload)))
        )

        with patch.object(
            self.db,
            "update_download_task",
            side_effect=sqlite3.OperationalError("database busy"),
        ):
            service._on_formats_ready(task.id, {
                "title": "Selectable video",
                "formats": [{"format_id": "best"}],
            })

        self.assertEqual(task.status, "waiting_selection")
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0][0], task.id)
        self.assertIs(service._progress_persistence.pending[task.id], task)
        service._progress_flush_timer.stop()
        service.shutdown(timeout_ms=0)

    def test_pending_delete_database_failure_releases_worker_slot_and_keeps_retryable_record(self) -> None:
        task = DownloadTask(
            "pending-delete-database-failure",
            "https://example.test/pending-delete-database-failure",
            str(self.root),
            status="canceling",
            cancel_requested=True,
        )
        self.db.upsert_download_task(task)
        service = DownloadService(self.db)
        service._register_task(task)
        service.workers[task.id] = object()  # type: ignore[assignment]
        service._pending_deletes[task.id] = False

        with patch.object(
            service,
            "_remove_task_record",
            side_effect=sqlite3.OperationalError("database busy"),
        ), patch.object(service, "_start_next") as start_next:
            service._thread_finished(task.id)

        self.assertIn(task.id, service.tasks)
        self.assertEqual(task.status, "canceled")
        self.assertNotIn(task.id, service.workers)
        self.assertNotIn(task.id, service._pending_deletes)
        start_next.assert_called_once_with()
        self.assertTrue(any(
            event.get("category") == "数据库/删除"
            and "删除任务记录失败" in str(event.get("message") or "")
            for event in service.logs.read(task.id)
        ))
        service.shutdown(timeout_ms=0)

    def test_pending_delete_and_log_failure_still_release_runtime(self) -> None:
        task = DownloadTask(
            "pending-delete-log-failure",
            "https://example.test/pending-delete-log-failure",
            str(self.root),
            status="canceling",
            cancel_requested=True,
        )
        self.db.upsert_download_task(task)
        service = DownloadService(self.db)
        service._register_task(task)
        service.workers[task.id] = object()  # type: ignore[assignment]
        service.threads[task.id] = object()  # type: ignore[assignment]
        service._pending_deletes[task.id] = False

        with patch.object(
            service,
            "_remove_task_record",
            side_effect=sqlite3.OperationalError("database busy"),
        ), patch.object(
            service.logs,
            "write",
            side_effect=PermissionError("log locked"),
        ), patch.object(service, "_start_next") as start_next:
            service._thread_finished(task.id)

        self.assertNotIn(task.id, service.workers)
        self.assertNotIn(task.id, service.threads)
        self.assertEqual(task.status, "canceled")
        self.assertNotIn(task.id, service._pending_deletes)
        start_next.assert_called_once_with()
        service.shutdown(timeout_ms=0)

    def test_download_thread_start_failure_marks_task_failed_and_cleans_runtime(self) -> None:
        task = DownloadTask(
            "download-start-failure",
            "https://example.test/download-start-failure",
            str(self.root),
            status="queued",
        )
        self.db.upsert_download_task(task)
        service = DownloadService(self.db)
        service._register_task(task)
        service.queue.append(task.id)
        finished: list[tuple[str, str]] = []
        service.task_finished.connect(
            lambda task_id, status, _error: finished.append((task_id, status))
        )

        with patch(
            "app.core.download_service.QThread.start",
            side_effect=RuntimeError("thread resource exhausted"),
        ):
            service._start_next()

        self.assertEqual(task.status, "failed")
        self.assertEqual(task.stage, "failed")
        self.assertIn("无法启动下载线程", task.error)
        self.assertNotIn(task.id, service.threads)
        self.assertNotIn(task.id, service.workers)
        self.assertNotIn(task.id, service._disk_leases)
        self.assertEqual(finished, [(task.id, "failed")])
        service.shutdown(timeout_ms=0)

    def test_download_start_failure_cleanup_survives_database_and_log_failures(self) -> None:
        task = DownloadTask(
            "download-start-cleanup-failure",
            "https://example.test/download-start-cleanup-failure",
            str(self.root),
            status="downloading",
        )
        self.db.upsert_download_task(task)
        service = DownloadService(self.db)
        service._register_task(task)

        class RetainedLease:
            def __init__(self) -> None:
                self.active_count = 1
                self.release_count = 0

            def release_all(self) -> int:
                self.release_count += 1
                self.active_count = 0
                return 1

        lease = RetainedLease()
        runtime = SimpleNamespace(thread=object(), worker=object(), disk_lease=lease)
        service.threads[task.id] = runtime.thread  # type: ignore[assignment]
        service.workers[task.id] = runtime.worker  # type: ignore[assignment]
        service._disk_leases[task.id] = lease  # type: ignore[assignment]
        finished: list[tuple[str, str]] = []
        service.task_finished.connect(
            lambda task_id, status, _error: finished.append((task_id, status))
        )

        with patch(
            "app.core.download_service.delete_unstarted_worker",
        ), patch.object(
            service,
            "_persist",
            side_effect=sqlite3.OperationalError("database unavailable"),
        ), patch.object(
            service.logs,
            "write",
            side_effect=OSError("log unavailable"),
        ), patch.object(
            service.logs,
            "flush",
            side_effect=OSError("log unavailable"),
        ):
            service._finalize_download_start_failure(
                task,
                RuntimeError("thread start failed"),
                runtime,  # type: ignore[arg-type]
            )

        self.assertEqual(task.status, "failed")
        self.assertIn("thread start failed", task.error)
        self.assertEqual(service._task_index.states[task.id][1], "failed")
        self.assertEqual(lease.release_count, 1)
        self.assertEqual(lease.active_count, 0)
        self.assertNotIn(task.id, service.threads)
        self.assertNotIn(task.id, service.workers)
        self.assertNotIn(task.id, service._disk_leases)
        self.assertEqual(finished, [(task.id, "failed")])
        service.shutdown(timeout_ms=0)

    def test_download_scheduler_ignores_stale_queue_ids_before_valid_task(self) -> None:
        task = DownloadTask(
            "valid-after-stale-queue-id",
            "https://example.test/valid-after-stale-queue-id",
            str(self.root),
            status="queued",
        )
        self.db.upsert_download_task(task)
        service = DownloadService(self.db)
        service._register_task(task)
        service.queue.extend(("missing-task-id", task.id))

        with patch("app.core.download_service.QThread.start", autospec=True):
            service._start_next()

        self.assertEqual(list(service.queue), [])
        self.assertEqual(task.status, "downloading")
        self.assertIn(task.id, service.workers)
        self.assertNotIn("missing-task-id", service.workers)
        service._thread_finished(task.id)
        service.shutdown(timeout_ms=0)

    def test_missing_durable_queue_row_does_not_starve_later_task(self) -> None:
        missing = DownloadTask(
            "missing-durable-queue-row",
            "https://example.test/missing-durable-queue-row",
            str(self.root),
            status="queued",
        )
        following = DownloadTask(
            "following-valid-queue-row",
            "https://example.test/following-valid-queue-row",
            str(self.root),
            status="queued",
        )
        self.db.upsert_download_tasks((missing, following))
        service = DownloadService(self.db)
        service._register_task(missing)
        service._register_task(following)
        service.queue.extend((missing.id, following.id))
        self.db.delete_download_task(missing.id)
        deleted: list[str] = []
        service.task_deleted.connect(deleted.append)

        with patch("app.core.download_service.QThread.start", autospec=True):
            service._start_next()

        self.assertNotIn(missing.id, service.tasks)
        self.assertEqual(deleted, [missing.id])
        self.assertEqual(following.status, "downloading")
        self.assertIn(following.id, service.workers)
        self.assertEqual(list(service.queue), [])

        service._thread_finished(following.id)
        service.shutdown(timeout_ms=0)

    def test_download_worker_constructor_failure_does_not_leave_task_downloading(self) -> None:
        task = DownloadTask(
            "download-worker-constructor-failure",
            "https://example.test/download-worker-constructor-failure",
            str(self.root),
            status="queued",
        )
        self.db.upsert_download_task(task)
        service = DownloadService(self.db)
        service._register_task(task)
        service.queue.append(task.id)
        finished: list[tuple[str, str, str]] = []
        service.task_finished.connect(
            lambda task_id, status, error: finished.append((task_id, status, error))
        )

        with patch(
            "app.core.download_service.DownloadWorker",
            side_effect=RuntimeError("invalid runtime configuration"),
        ):
            service._start_next()

        self.assertEqual(task.status, "failed")
        self.assertEqual(task.stage, "failed")
        self.assertIn("invalid runtime configuration", task.error)
        self.assertNotIn(task.id, service.workers)
        self.assertNotIn(task.id, service.threads)
        self.assertNotIn(task.id, service._disk_leases)
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0][:2], (task.id, "failed"))
        service.shutdown(timeout_ms=0)

    def test_download_start_persist_failure_restores_queue_and_status_for_retry(self) -> None:
        task = DownloadTask(
            "download-start-persist-failure",
            "https://example.test/download-start-persist-failure",
            str(self.root),
            status="queued",
        )
        self.db.upsert_download_task(task)
        service = DownloadService(self.db)
        service._register_task(task)
        service.queue.append(task.id)

        with patch.object(
            service,
            "_persist",
            side_effect=sqlite3.OperationalError("database busy"),
        ), patch.object(
            service.logs,
            "write",
            side_effect=OSError("log unavailable"),
        ), patch("app.core.download_service.QTimer.singleShot") as retry_timer:
            service._start_next()

        self.assertEqual(task.status, "queued")
        self.assertEqual(task.stage, "queued")
        self.assertEqual(list(service.queue), [task.id])
        self.assertEqual(service._task_index.states[task.id][1], "queued")
        self.assertNotIn(task.id, service.workers)
        retry_timer.assert_called_once()
        self.assertEqual(retry_timer.call_args.args[0], 1000)
        service.shutdown(timeout_ms=0)

    def test_persistently_blocked_queue_row_rotates_behind_later_tasks(self) -> None:
        blocked = DownloadTask(
            "blocked-queue-row",
            "https://example.test/blocked-queue-row",
            str(self.root),
            status="queued",
        )
        following = DownloadTask(
            "queue-row-after-blocked",
            "https://example.test/queue-row-after-blocked",
            str(self.root),
            status="queued",
        )
        self.db.upsert_download_tasks((blocked, following))
        service = DownloadService(self.db, max_concurrent=1)
        service._register_task(blocked)
        service._register_task(following)
        service.queue.extend((blocked.id, following.id))
        original_persist = service._persist

        def persist(task: DownloadTask) -> None:
            if task.id == blocked.id:
                raise sqlite3.OperationalError("row remains blocked")
            original_persist(task)

        with patch.object(
            service,
            "_persist",
            side_effect=persist,
        ), patch.object(
            service,
            "_create_download_runtime",
            side_effect=RuntimeError("following task reached runtime creation"),
        ), patch("app.core.download_service.QTimer.singleShot") as retry_timer:
            service._start_next()
            self.assertEqual(list(service.queue), [following.id, blocked.id])
            retry_timer.call_args.args[1]()

        self.assertEqual(following.status, "failed")
        self.assertIn("reached runtime creation", following.error)
        self.assertNotIn(following.id, service.workers)
        self.assertEqual(blocked.status, "queued")
        self.assertEqual(list(service.queue), [blocked.id])

        service.shutdown(timeout_ms=0)

    def test_cancel_and_pause_persist_failures_restore_task_before_worker_side_effects(self) -> None:
        for action in ("cancel", "pause"):
            with self.subTest(action=action):
                task = DownloadTask(
                    f"{action}-persist-rollback",
                    f"https://example.test/{action}-persist-rollback",
                    str(self.root),
                    status="downloading",
                    stage="downloading",
                    stage_text="正在下载",
                )
                self.db.upsert_download_task(task)
                service = DownloadService(self.db)
                service._register_task(task)
                cancel_reasons: list[str] = []
                service.workers[task.id] = SimpleNamespace(
                    cancel=lambda reason: cancel_reasons.append(reason)
                )

                with patch.object(
                    service,
                    "_persist",
                    side_effect=sqlite3.OperationalError("database busy"),
                ):
                    with self.assertRaises(sqlite3.OperationalError):
                        getattr(service, action)(task.id)

                self.assertEqual(task.status, "downloading")
                self.assertEqual(task.stage, "downloading")
                self.assertEqual(task.stage_text, "正在下载")
                self.assertFalse(task.cancel_requested)
                self.assertFalse(task.pause_requested)
                self.assertEqual(cancel_reasons, [])
                service.workers.clear()
                service.shutdown(timeout_ms=0)

    def test_queue_state_mutations_wait_for_durable_commit(self) -> None:
        cases = (
            ("cancel", "queued", "queued"),
            ("pause", "queued", "queued"),
            ("resume", "paused", "paused"),
            ("retry", "failed", "failed"),
        )
        for action, initial_status, expected_status in cases:
            with self.subTest(action=action):
                task = DownloadTask(
                    f"{action}-queue-persist-rollback",
                    f"https://example.test/{action}-queue-persist-rollback",
                    str(self.root),
                    status=initial_status,
                    stage=initial_status,
                    error="original error" if initial_status == "failed" else "",
                    progress=42.0,
                )
                task.options_json["_storage_preview"] = {"path": "original"}
                task.speed_samples = (10.0, 20.0)
                self.db.upsert_download_task(task)
                service = DownloadService(self.db)
                service._register_task(task)
                if initial_status == "queued":
                    service.queue.append(task.id)

                with patch.object(
                    service,
                    "_persist",
                    side_effect=sqlite3.OperationalError("database busy"),
                ), patch.object(service, "_start_next") as start_next:
                    with self.assertRaises(sqlite3.OperationalError):
                        getattr(service, action)(task.id)

                self.assertEqual(task.status, expected_status)
                self.assertEqual(task.progress, 42.0)
                self.assertEqual(list(task.speed_samples), [10.0, 20.0])
                self.assertEqual(task.options_json["_storage_preview"], {"path": "original"})
                self.assertEqual(
                    list(service.queue),
                    [task.id] if initial_status == "queued" else [],
                )
                start_next.assert_not_called()
                service.shutdown(timeout_ms=0)

    def test_format_selection_and_active_delete_wait_for_durable_commit(self) -> None:
        format_task = DownloadTask(
            "format-selection-persist-rollback",
            "https://example.test/format-selection-persist-rollback",
            str(self.root),
            status="waiting_selection",
            stage="waiting_selection",
            format_selector="old-selector",
            options_json={"content_mode": "video", "audio_format": "mp3"},
        )
        delete_task = DownloadTask(
            "active-delete-persist-rollback",
            "https://example.test/active-delete-persist-rollback",
            str(self.root),
            status="downloading",
            stage="downloading",
        )
        self.db.upsert_download_tasks((format_task, delete_task))
        service = DownloadService(self.db)
        service._register_task(format_task)
        service._register_task(delete_task)
        selections: list[tuple[str, str, str]] = []
        cancel_reasons: list[str] = []
        service.workers[format_task.id] = SimpleNamespace(
            set_format_selector=lambda selector, *, content_mode, audio_format: selections.append(
                (selector, content_mode, audio_format)
            )
        )
        service.workers[delete_task.id] = SimpleNamespace(
            cancel=lambda reason: cancel_reasons.append(reason)
        )

        with patch.object(
            service,
            "_persist",
            side_effect=sqlite3.OperationalError("database busy"),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                service.set_format_selection(format_task.id, {
                    "selector": "new-selector",
                    "content_mode": "audio",
                    "audio_format": "flac",
                })
            with self.assertRaises(sqlite3.OperationalError):
                service.delete_task(delete_task.id, delete_files=True)

        self.assertEqual(format_task.status, "waiting_selection")
        self.assertEqual(format_task.stage, "waiting_selection")
        self.assertEqual(format_task.format_selector, "old-selector")
        restored_options = DownloadOptions.from_mapping(format_task.options_json)
        self.assertEqual(restored_options.content_mode, "video")
        self.assertEqual(restored_options.audio_format, "mp3")
        self.assertEqual(selections, [])
        self.assertEqual(delete_task.status, "downloading")
        self.assertFalse(delete_task.cancel_requested)
        self.assertNotIn(delete_task.id, service._pending_deletes)
        self.assertEqual(cancel_reasons, [])
        service.workers.clear()
        service.shutdown(timeout_ms=0)

    def test_many_thread_start_failures_are_iterative_and_do_not_block_later_tasks(self) -> None:
        service = DownloadService(self.db, max_concurrent=2)
        tasks = [
            DownloadTask(
                f"iterative-start-failure-{index}",
                f"https://example.test/iterative-start-failure/{index}",
                str(self.root),
                status="queued",
            )
            for index in range(20)
        ]
        self.db.upsert_download_tasks(tasks)
        for task in tasks:
            service._register_task(task)
            service.queue.append(task.id)
        finished: list[str] = []
        service.task_finished.connect(
            lambda task_id, _status, _error: finished.append(task_id)
        )

        with patch(
            "app.core.download_service.QThread.start",
            side_effect=RuntimeError("thread resource exhausted"),
        ):
            service._start_next()

        self.assertEqual(list(service.queue), [])
        self.assertEqual(len(finished), len(tasks))
        self.assertTrue(all(task.status == "failed" for task in tasks))
        self.assertEqual(service.workers, {})
        self.assertEqual(service.threads, {})
        self.assertEqual(service._disk_leases, {})
        service.shutdown(timeout_ms=0)

    def test_deferred_thread_finish_allows_queued_failure_to_win(self) -> None:
        task = DownloadTask(
            "late-failure-before-finish",
            "https://example.test/late-failure-before-finish",
            str(self.root),
            status="downloading",
        )
        self.db.upsert_download_task(task)
        service = DownloadService(self.db)
        service._register_task(task)
        service.threads[task.id] = QThread()
        finished: list[str] = []
        service.task_finished.connect(
            lambda _task_id, status, _error: finished.append(status)
        )

        service._defer_thread_finished(task.id)
        service._on_failed(task.id, "late worker failure")
        QCoreApplication.processEvents()

        self.assertEqual(task.status, "failed")
        self.assertEqual(finished, ["failed"])
        service.shutdown(timeout_ms=0)

    def test_retry_requested_before_old_thread_cleanup_runs_after_release(self) -> None:
        task = DownloadTask(
            "retry-after-runtime-release",
            "https://example.test/retry-after-runtime-release",
            str(self.root),
            status="failed",
            error="download failed",
            stage="failed",
        )
        self.db.upsert_download_task(task)
        service = DownloadService(self.db)
        service._register_task(task)
        old_thread = QThread()
        service.threads[task.id] = old_thread
        service.workers[task.id] = object()  # type: ignore[assignment]

        with patch.object(service, "_start_next") as start_next:
            service.retry(task.id)
            service.retry(task.id)

            self.assertEqual(task.status, "failed")
            self.assertEqual(list(service.queue), [])
            self.assertIn(task.id, service._pending_runtime_retries)

            service._thread_finished(task.id, expected_thread=old_thread)

        self.assertEqual(task.status, "queued")
        self.assertEqual(task.error, "")
        self.assertEqual(list(service.queue), [task.id])
        self.assertNotIn(task.id, service._pending_runtime_retries)
        self.assertNotIn(task.id, service.threads)
        self.assertNotIn(task.id, service.workers)
        persisted = next(
            row for row in self.db.list_download_tasks() if row["id"] == task.id
        )
        self.assertEqual(persisted["status"], "queued")
        self.assertGreaterEqual(start_next.call_count, 1)
        old_thread.deleteLater()
        QCoreApplication.processEvents()
        service.shutdown(timeout_ms=0)

    def test_old_download_cleanup_cannot_remove_replacement_runtime(self) -> None:
        task = DownloadTask(
            "replacement-download-runtime",
            "https://example.test/replacement-download-runtime",
            str(self.root),
            status="downloading",
        )
        self.db.upsert_download_task(task)
        service = DownloadService(self.db)
        service._register_task(task)
        old_thread = QThread()
        replacement_thread = QThread()
        replacement_worker = object()
        service.threads[task.id] = old_thread
        service.workers[task.id] = object()  # type: ignore[assignment]

        service._defer_thread_finished(task.id, old_thread)
        service.threads[task.id] = replacement_thread
        service.workers[task.id] = replacement_worker  # type: ignore[assignment]
        QCoreApplication.processEvents()

        self.assertIs(service.threads[task.id], replacement_thread)
        self.assertIs(service.workers[task.id], replacement_worker)
        self.assertEqual(task.status, "downloading")
        self.assertEqual(len(service._deferred_thread_finishes), 0)
        service.threads.clear()
        service.workers.clear()
        replacement_thread.deleteLater()
        QCoreApplication.processEvents()
        service.shutdown(timeout_ms=0)

    def test_ffprobe_uses_sibling_of_configured_ffmpeg(self) -> None:
        configured = self.root / "custom" / "ffmpeg.exe"
        sibling = configured.with_name("ffprobe.exe")
        configured.parent.mkdir()
        configured.write_bytes(b"ffmpeg")
        sibling.write_bytes(b"ffprobe")

        with patch("app.core.download_service.application_dir", return_value=self.root), patch(
            "app.core.download_service.tool_runtime_roots", return_value=[self.root]
        ):
            self.assertEqual(Path(ffprobe_runtime_path(str(configured))), sibling)


if __name__ == "__main__":
    unittest.main()

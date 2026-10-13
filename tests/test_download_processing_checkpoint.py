from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, Qt

from app.core.collection_aggregation import CollectionAggregate, collection_child_contribution, summarize_collection
from app.core.download_progress import StageProgressState, TransferCounterState, merge_stage_progress, merge_transfer_counters
from app.core.download_service import DownloadService, DownloadTask, DownloadWorker
from app.core.download_task_restore import TaskRowReader, restored_status
from app.core.media_validation import MediaValidationResult
from app.core.processing_workspace import processing_temp_workspace
from app.core.transcode_service import PreparedTranscode, TranscodeError
from app.storage.database import Database
from app.storage.models import MediaItem


class ProcessingProgressTest(unittest.TestCase):
    def test_audio_counters_replace_the_finished_video_stream(self) -> None:
        video = merge_transfer_counters(TransferCounterState(), {
            "filename": "video.mp4", "downloaded_bytes": 1000, "total_bytes": 1000,
        })
        audio = merge_transfer_counters(video, {
            "filename": "audio.m4a", "downloaded_bytes": 10, "total_bytes": 100,
        })
        self.assertEqual((audio.progress, audio.visible_progress), (10, 10))
        self.assertEqual((audio.downloaded_bytes, audio.total_bytes), (10, 100))

    def test_unknown_processing_progress_is_indeterminate(self) -> None:
        previous = StageProgressState(stage="downloading_video", stage_progress=100, stage_indeterminate=False)
        merge = merge_stage_progress(previous, {"stage": "verifying"})
        self.assertTrue(merge.state.stage_indeterminate)
        self.assertEqual(merge.state.stage_progress, 0)
        measured = merge_stage_progress(merge.state, {"stage": "transcoding", "stage_progress": 37})
        self.assertFalse(measured.state.stage_indeterminate)
        self.assertEqual(measured.state.stage_progress, 37)

    def test_all_processing_collection_stays_nonterminal(self) -> None:
        aggregate = CollectionAggregate()
        for status in ("completed", "processing", "processing"):
            aggregate.apply(collection_child_contribution(
                parent_id="collection", status=status, speed_bps=100,
                total_bytes=100, downloaded_bytes=100, progress=100,
            ), 1)
        summary = summarize_collection(aggregate, 3)
        self.assertEqual(summary.status, "processing")
        self.assertFalse(summary.terminal)
        self.assertEqual(summary.speed_bps, 0)

    def test_processing_failure_counts_as_failed_child(self) -> None:
        aggregate = CollectionAggregate()
        aggregate.apply(collection_child_contribution(
            parent_id="collection", status="processing_failed", speed_bps=0,
            total_bytes=100, downloaded_bytes=100, progress=99,
        ), 1)
        summary = summarize_collection(aggregate, 1)
        self.assertTrue(summary.terminal)
        self.assertEqual(summary.metadata["failed"], 1)

    def test_restart_never_infers_completion_from_processing(self) -> None:
        statuses = {"processing", "completed", "paused"}
        self.assertEqual(restored_status(TaskRowReader({"status": "processing", "options_json": "{}"}), statuses)[1], "paused")
        self.assertEqual(restored_status(TaskRowReader({"status": "processing", "options_json": json.dumps({"_processing": {"version": 1}})}), statuses)[1], "processing")


class _Thread:
    def __init__(self, task_id: str, starts: list[str]):
        self.task_id, self.starts = task_id, starts

    def start(self) -> None:
        self.starts.append(self.task_id)


class ProcessingCheckpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.output = self.root / "output"
        self.output.mkdir()
        self.db = Database(self.root / "tasks.db")
        self.service = DownloadService(self.db, max_concurrent=2, cover_convert_jpeg=True, cover_jpeg_quality=72)
        self.logs = SimpleNamespace(write=lambda *_args, **_kwargs: None, flush=lambda *_args: None, redact_url=str)
        self.service.logs = self.logs

    def tearDown(self) -> None:
        self.service._shutting_down = True
        for timer in (self.service._progress_flush_timer, self.service._collection_refresh_timer, self.service._restore_timer):
            timer.stop()
        self.service.workers.clear()
        self.service.threads.clear()
        self.service._disk_leases.clear()
        self.db.close()
        self.directory.cleanup()

    def _task(self, task_id: str = "video", *, processing: bool = True, prepared: bool = False) -> DownloadTask:
        source = self.output / f"{task_id}.mp4"
        source.write_bytes(b"original media")
        options = {"processing_temp_dir": str(self.root / "temporary")}
        if processing:
            workspace = processing_temp_workspace(options["processing_temp_dir"], task_id)
            checkpoint = {
                "version": 1, "kind": "download", "state": "waiting",
                "workspace": str(workspace), "source_ip": "test-route",
                "entries": [{"filepath": str(source), "title": task_id, "webpage_url": f"https://example.test/{task_id}", "vcodec": "h264", "acodec": "none"}],
                "prepared_entries": [],
            }
            if prepared:
                temporary = workspace / f".{task_id}.transcoding-1-2.part.mp4"
                temporary.write_bytes(b"converted media")
                transcode = PreparedTranscode(source, source, temporary, "libx264", False)
                checkpoint["prepared_entries"] = [DownloadWorker._prepared_processing_entry(
                    MediaItem(source_url=f"https://example.test/{task_id}", title=task_id, video_path=str(source)), transcode,
                )]
            options["_processing"] = checkpoint
        task = DownloadTask(
            task_id, f"https://example.test/{task_id}", str(self.output),
            options_json=options, status="processing" if processing else "queued",
            transcode_codec="h264", transcode_encoder="libx264",
        )
        self.db.insert_download_task(task)
        self.service._register_task(task)
        return task

    def _worker(self, task: DownloadTask, *, deferred: bool = False) -> DownloadWorker:
        worker = DownloadWorker(
            task.id, task.url, task.output_dir, self.db, options_json=task.options_json,
            transcode_codec=task.transcode_codec, transcode_encoder=task.transcode_encoder,
            log_service=self.logs, defer_processing=deferred,
        )
        worker._source_ip_checked = True
        worker._source_ip = "test-route"
        worker.processing_checkpoint_ready.connect(self.service._on_processing_checkpoint, Qt.DirectConnection)
        worker.progress.connect(self.service._on_progress, Qt.DirectConnection)
        worker.failed.connect(self.service._on_failed, Qt.DirectConnection)
        worker.completed.connect(self.service._on_media_completed, Qt.DirectConnection)
        self.service.workers[task.id] = worker
        if worker._processing_only:
            self.service._processing_task_ids.add(task.id)
        return worker

    @staticmethod
    def _validation(path: str | Path, *_args: object, **_kwargs: object) -> MediaValidationResult:
        path = Path(path)
        return MediaValidationResult(str(path), path.stat().st_size, 1, "mp4", "MP4", 1, 1, 0, 0, 0)

    def _run(self, worker: DownloadWorker, *, fail_commit: bool = False) -> None:
        with ExitStack() as stack:
            stack.enter_context(patch("app.core.download_service.ffmpeg_runtime_path", return_value="ffmpeg"))
            stack.enter_context(patch("app.core.download_service.ffprobe_runtime_path", return_value="ffprobe"))
            stack.enter_context(patch("app.core.download_service.validate_media_file", side_effect=self._validation))
            stack.enter_context(patch("app.core.download_service.probe_video_stream", return_value=SimpleNamespace(has_audio=False, codec="h264")))
            stack.enter_context(patch.object(worker, "_validate_prepared_transcode", side_effect=lambda prepared, *_args: self._validation(prepared.temporary_path)))
            network = stack.enter_context(patch.object(worker, "_run_download_flow", side_effect=AssertionError("processing must not download")))
            if fail_commit:
                stack.enter_context(patch.object(self.db, "complete_download_task_batch", side_effect=RuntimeError("catalog locked")))
            worker.run()
            network.assert_not_called()

    def test_frozen_jpeg_plan_survives_collection_inheritance(self) -> None:
        parent = self.service._build_new_download_task("https://example.test/list", str(self.output), task_kind="collection")
        self.service.cover_convert_jpeg = False
        self.service.cover_jpeg_quality = 100
        child_options = self.service._collection_child_options(parent)
        child = DownloadTask("child", "https://example.test/child", str(self.output), options_json=child_options)
        worker = DownloadWorker(child.id, child.url, child.output_dir, self.db, options_json=child.options_json, cover_convert_jpeg=False, cover_jpeg_quality=100)
        self.assertTrue(worker.cover_convert_jpeg)
        self.assertEqual(worker.cover_jpeg_quality, 72)
        self.assertEqual(child.options_json["_processing_plan"], parent.options_json["_processing_plan"])

    def test_network_handoff_is_durable_before_source_ownership_is_released(self) -> None:
        task = self._task(processing=False)
        worker = self._worker(task, deferred=True)
        worker._processing_workspace = processing_temp_workspace(worker.processing_temp_dir, task.id)
        source = self.output / "video.mp4"
        with patch("app.core.download_service.ffmpeg_runtime_path", return_value="ffmpeg"), patch("app.core.download_service.ffprobe_runtime_path", return_value="ffprobe"):
            worker._complete_download_info({"filepath": str(source), "title": "video"}, lambda _entry: str(source))
        persisted = self.db.list_download_tasks()[0]
        self.assertEqual(persisted["status"], "processing")
        self.assertEqual(json.loads(persisted["options_json"])["_processing"]["entries"][0]["filepath"], str(source))
        self.assertEqual(self.db.list_media(), [])
        self.assertFalse(worker._should_cleanup_processing_workspace())
        self.assertEqual(source.read_bytes(), b"original media")

    def test_processing_failure_and_retry_preserve_source_and_avoid_network(self) -> None:
        task = self._task()
        worker = self._worker(task)
        with patch.object(worker, "_build_validated_transcode", side_effect=TranscodeError("encoder failed")):
            self._run(worker)
        self.service._thread_finished(task.id)
        self.assertEqual(task.status, "processing_failed")
        self.assertEqual(task.failed_step, "transcoding")
        self.assertIn("PROCESSING FAILED", task.stage_text)
        self.assertEqual((self.output / "video.mp4").read_bytes(), b"original media")
        workspace = Path(task.options_json["_processing"]["workspace"])
        self.assertTrue(workspace.is_dir())
        with patch.object(self.service, "_start_next"):
            self.service.retry(task.id)
        self.assertEqual(list(self.service.queue), [])
        self.assertEqual(list(self.service._processing_queue), [task.id])
        self.service._processing_queue.clear()
        retry = self._worker(task)
        temporary = workspace / ".video.transcoding-1-2.part.mp4"
        temporary.write_bytes(b"converted media")
        prepared = PreparedTranscode(self.output / "video.mp4", self.output / "video.mp4", temporary, "libx264", False)
        with patch.object(retry, "_build_validated_transcode", return_value=(prepared, self._validation(temporary))):
            self._run(retry)
        self.service._thread_finished(task.id)
        self.assertEqual(task.status, "completed")
        self.assertEqual(len(self.db.list_media()), 1)
        self.assertEqual((self.output / "video.mp4").read_bytes(), b"converted media")

    def test_restart_restores_publication_then_reuses_validated_output(self) -> None:
        task = self._task(prepared=True)
        checkpoint = task.options_json["_processing"]
        raw = checkpoint["prepared_entries"][0]["transcode"]
        source, temporary = Path(raw["source_path"]), Path(raw["temporary_path"])
        backup = source.with_name(f".{source.name}.pre-transcode-123-456.bak")
        checkpoint["state"] = "publishing"
        checkpoint["publications"] = [{**raw, "backup_path": str(backup)}]
        source.replace(backup)
        temporary.replace(source)
        self.db.update_download_task(task)
        worker = self._worker(task)
        with patch.object(worker, "_build_validated_transcode", side_effect=AssertionError("saved output should be reused")) as transcode:
            self._run(worker)
        transcode.assert_not_called()
        self.assertEqual(task.status, "completed")
        self.assertEqual(source.read_bytes(), b"converted media")
        self.assertFalse(backup.exists())
        self.assertEqual(len(self.db.list_media()), 1)

    def test_service_restore_queues_checkpoint_and_honors_atomic_completion(self) -> None:
        processing = self._task("interrupted")
        completed = self._task("already-committed")
        item = MediaItem(source_url=completed.url, video_path=str(self.output / "already-committed.mp4"))
        self.db.complete_download_task_batch(completed, [item])
        restored_service = DownloadService(self.db)
        restored_service.logs = self.logs
        with patch.object(restored_service, "_start_next"):
            restored_service.restore_tasks()
        self.assertEqual(restored_service.tasks[processing.id].status, "processing")
        self.assertEqual(restored_service.tasks[processing.id].stage, "waiting_processing")
        self.assertEqual(list(restored_service._processing_queue), [processing.id])
        self.assertEqual(restored_service.tasks[completed.id].status, "completed")
        self.assertEqual(list(restored_service.queue), [])

    def test_processing_progress_cannot_overwrite_atomic_completion(self) -> None:
        task = self._task()
        worker = self._worker(task)
        self.service._on_progress(task.id, {"stage": "transcoding", "stage_progress": 80})
        self.assertNotIn(task.id, self.service._progress_persistence.pending)
        self.assertEqual(task.status, "processing")
        self.db.complete_download_task_batch(task, [MediaItem(source_url=task.url, video_path=str(self.output / "video.mp4"))])
        self.service._on_progress(task.id, {"stage": "verifying"})
        self.assertNotIn(task.id, self.service._progress_persistence.pending)
        self.assertEqual(self.db.list_download_tasks()[0]["status"], "completed")
        self.assertTrue(task.stage_indeterminate)

    def test_deleting_only_task_record_preserves_processing_source_and_results(self) -> None:
        task = self._task(prepared=True)
        checkpoint = task.options_json["_processing"]
        self.assertTrue(self.service.delete_task(task.id, delete_files=False))
        self.assertEqual(self.db.list_download_tasks(), [])
        self.assertTrue(Path(checkpoint["entries"][0]["filepath"]).is_file())
        self.assertTrue(Path(checkpoint["prepared_entries"][0]["transcode"]["temporary_path"]).is_file())

    def test_failed_collection_summary_is_not_exposed_ahead_of_database(self) -> None:
        parent = DownloadTask("parent", "https://example.test/list", str(self.output), task_kind="collection", status="processing", options_json={"_collection": {"parsed": 1}})
        self.db.insert_download_task(parent)
        self.service._register_task(parent)
        child = self._task("child")
        child.parent_task_id = parent.id
        self.service._sync_task_indexes(child)
        child.status = "completed"
        self.service._sync_task_indexes(child)
        emitted = []
        self.service.task_updated.connect(emitted.append)
        with patch.object(self.service, "_persist_progress", return_value=False):
            self.service._refresh_collection(parent.id)
        self.assertEqual(parent.status, "processing")
        self.assertEqual(emitted, [])

    def test_catalog_failure_rolls_back_source_and_keeps_processing_result(self) -> None:
        task = self._task(prepared=True)
        raw = task.options_json["_processing"]["prepared_entries"][0]["transcode"]
        worker = self._worker(task)
        self._run(worker, fail_commit=True)
        self.assertEqual(task.status, "processing_failed")
        self.assertEqual(Path(raw["source_path"]).read_bytes(), b"original media")
        self.assertEqual(Path(raw["temporary_path"]).read_bytes(), b"converted media")
        self.assertEqual(self.db.list_media(), [])

    def test_one_processing_task_leaves_both_network_slots_available(self) -> None:
        processing = self._task("processing-one")
        manual = self._task("manual-two")
        manual.options_json["_processing"]["kind"] = "manual"
        self.db.update_download_task(manual)
        downloads = [self._task("network-one", processing=False), self._task("network-two", processing=False)]
        self.service._processing_queue.extend_unique((processing.id, manual.id))
        self.service.queue.extend_unique(task.id for task in downloads)
        starts: list[str] = []
        def runtime(task: DownloadTask):
            return SimpleNamespace(
                thread=_Thread(task.id, starts),
                worker=SimpleNamespace(_processing_only=bool(task.options_json.get("_processing"))),
                disk_lease=SimpleNamespace(release_all=lambda: 0),
            )
        with patch.object(self.service, "_create_download_runtime", side_effect=runtime):
            self.service._start_next()
            self.assertEqual(starts, [processing.id, *(task.id for task in downloads)])
            self.assertEqual(len(self.service._processing_task_ids), 1)
            self.assertEqual(self.service.active_thread_count, 3)
            self.service._start_next()
            self.assertNotIn(manual.id, starts)
            self.service._thread_finished(processing.id)
            self.assertEqual(starts[-1], manual.id)
            self.assertEqual(len(self.service._processing_task_ids), 1)

    def test_manual_conversion_uses_the_same_durable_queue_and_catalog_row(self) -> None:
        task = self._task(processing=False)
        source = self.output / "video.mp4"
        original = MediaItem(source_url=task.url, title="Original title", description="Keep metadata", tags=["keep"], video_path=str(source))
        self.db.complete_download_task_batch(task, [original])
        task.status, task.media_path = "completed", str(source)
        self.service._processing_task_ids.add("occupied")
        results: list[tuple[str, str, bool]] = []
        self.service.conversion_finished.connect(lambda *result: results.append(result))
        with patch("app.core.download_service.ffmpeg_runtime_path", return_value="ffmpeg"), patch("app.core.download_service.ffprobe_runtime_path", return_value="ffprobe"):
            self.assertTrue(self.service.convert_completed_task(task.id, "libx265"))
        self.assertEqual(self.db.list_download_tasks()[0]["status"], "processing")
        self.assertEqual(list(self.service._processing_queue), [task.id])
        self.assertEqual(list(self.service.queue), [])
        self.service._processing_queue.clear()
        self.service._processing_task_ids.clear()
        worker = self._worker(task)
        workspace = Path(task.options_json["_processing"]["workspace"])
        workspace.mkdir(parents=True, exist_ok=True)
        temporary = workspace / ".video.transcoding-1-2.part.mp4"
        temporary.write_bytes(b"converted media")
        prepared = PreparedTranscode(source, source, temporary, "libx265", False)
        with patch.object(worker, "_build_validated_transcode", return_value=(prepared, self._validation(temporary))):
            self._run(worker)
        self.service._thread_finished(task.id)
        media = self.db.list_media()
        self.assertEqual(len(media), 1)
        self.assertEqual((media[0].id, media[0].title, media[0].description, media[0].tags), (original.id, original.title, original.description, original.tags))
        self.assertEqual(task.transcode_encoder, "libx265")
        self.assertEqual(results, [(task.id, "libx265", False)])

    def test_queued_qt_handoff_releases_network_workers_before_processing(self) -> None:
        tasks = [self._task(f"entry-{index}", processing=False) for index in range(3)]
        for task in tasks:
            task.transcode_codec, task.transcode_encoder = "original", ""
            self.db.update_download_task(task)
        processing_started, release_processing = threading.Event(), threading.Event()
        original_prepare = DownloadWorker._prepare_completed_entry
        def prepare(worker: DownloadWorker, *args: object, **kwargs: object):
            processing_started.set()
            if not release_processing.wait(4):
                raise RuntimeError("test processing gate timed out")
            return original_prepare(worker, *args, **kwargs)
        def download(worker: DownloadWorker, *_args: object):
            source = self.output / f"{worker.task_id}.mp4"
            worker._complete_download_info({"filepath": str(source), "title": worker.task_id}, lambda _entry: str(source))
        def wait_until(predicate) -> bool:
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                self.app.processEvents()
                if predicate():
                    return True
                release_processing.wait(0.005)
            return False
        with ExitStack() as stack:
            stack.enter_context(patch.object(DownloadWorker, "_resolve_download_core", return_value=""))
            stack.enter_context(patch.object(DownloadWorker, "_build_ytdlp_options", return_value={}))
            stack.enter_context(patch.object(DownloadWorker, "_configure_cookie_options", return_value=None))
            stack.enter_context(patch.object(DownloadWorker, "_run_download_flow", download))
            stack.enter_context(patch.object(DownloadWorker, "_prepare_completed_entry", prepare))
            stack.enter_context(patch("app.core.download_service.ffmpeg_runtime_path", return_value="ffmpeg"))
            stack.enter_context(patch("app.core.download_service.ffprobe_runtime_path", return_value="ffprobe"))
            stack.enter_context(patch("app.core.download_service.detect_public_ip", return_value="test-route"))
            stack.enter_context(patch("app.core.download_service.validate_media_file", side_effect=self._validation))
            self.service.queue.extend_unique(task.id for task in tasks)
            self.service._start_next()
            try:
                self.assertTrue(wait_until(lambda: processing_started.is_set() and len(self.service.workers) == 1 and len(self.service._processing_queue) == 2))
                self.assertEqual(len(self.service._processing_task_ids), 1)
                self.assertTrue(all(task.status == "processing" for task in tasks))
                self.assertEqual(self.db.list_media(), [])
            finally:
                release_processing.set()
                self.assertTrue(wait_until(lambda: self.service.active_thread_count == 0))
            self.assertTrue(all(task.status == "completed" for task in tasks))
            self.assertEqual(len(self.db.list_media()), 3)

    def test_pausing_and_canceling_waiting_processing_preserves_local_retry(self) -> None:
        task = self._task()
        self.service._processing_queue.append_unique(task.id)
        self.service._processing_task_ids.add("occupied")
        self.service.pause(task.id)
        self.assertEqual(task.status, "paused")
        self.assertEqual(list(self.service._processing_queue), [])
        self.service.resume(task.id)
        self.assertEqual(task.status, "processing")
        self.service.cancel(task.id)
        self.assertEqual(task.status, "canceled")
        self.service.retry(task.id)
        self.assertEqual(task.status, "processing")
        self.assertEqual(list(self.service._processing_queue), [task.id])
        self.assertEqual(list(self.service.queue), [])


if __name__ == "__main__":
    unittest.main()

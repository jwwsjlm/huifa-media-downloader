from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from app.core.download_service import (
    CompletedMediaTranscodeWorker,
    DownloadService,
    DownloadTask,
    DownloadWorker,
)
from app.core.paths import tool_runtime_roots
from app.core.update_service import UpdateWorker
from app.storage.database import Database
from app.storage.models import MediaItem, PublishTask


class PerformancePersistenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_component_update_checks_are_bounded_concurrent_and_ordered(self) -> None:
        names = [f"component-{index}" for index in range(8)]
        worker = UpdateWorker({name: f"owner/{name}" for name in names})
        lock = threading.Lock()
        release = threading.Event()
        first_wave_ready = threading.Event()
        active = 0
        peak = 0

        def probe(name: str, repo: str, _headers: dict[str, str]) -> dict:
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                if active == 4:
                    first_wave_ready.set()
            try:
                self.assertTrue(release.wait(2), "concurrent update probes did not reach the release gate")
                # Deliberately finish in reverse order; the user-facing table
                # must still follow the configured component order.
                time.sleep((len(names) - names.index(name)) * 0.001)
                return {"name": name, "repo": repo}
            finally:
                with lock:
                    active -= 1

        def release_first_wave() -> None:
            if first_wave_ready.wait(2):
                release.set()

        releaser = threading.Thread(target=release_first_wave, name="update-check-test-release")
        releaser.start()
        captured: list[list[dict]] = []
        partial: list[dict] = []
        with patch.object(worker, "_check_component", side_effect=probe):
            worker.result_ready.connect(partial.append)
            worker.finished.connect(captured.append)
            worker.run()
        releaser.join(2)

        self.assertTrue(first_wave_ready.is_set())
        self.assertEqual(peak, 4)
        self.assertEqual([result["name"] for result in captured[0]], names)
        self.assertEqual({result["name"] for result in partial}, set(names))
        self.assertFalse(any(thread.name.startswith("huifa-update-check") for thread in threading.enumerate()))

    def test_component_update_failure_is_isolated_from_other_results(self) -> None:
        names = ["healthy-a", "broken", "healthy-b"]
        worker = UpdateWorker({name: f"owner/{name}" for name in names})

        def probe(name: str, repo: str, _headers: dict[str, str]) -> dict:
            if name == "broken":
                raise RuntimeError("simulated repository failure")
            return {"name": name, "repo": repo}

        completed: list[list[dict]] = []
        failed: list[str] = []
        worker.finished.connect(completed.append)
        worker.failed.connect(failed.append)
        with patch.object(worker, "_check_component", side_effect=probe), patch.object(
            worker,
            "_installed_component",
            return_value=("1.0", "测试运行时", "test.exe"),
        ):
            worker.run()

        self.assertFalse(failed)
        self.assertEqual([result["name"] for result in completed[0]], names)
        self.assertNotIn("error", completed[0][0])
        self.assertIn("simulated repository failure", completed[0][1]["error"])
        self.assertNotIn("error", completed[0][2])

    def test_component_update_cancel_does_not_wait_for_inflight_network_calls(self) -> None:
        worker = UpdateWorker({f"component-{index}": f"owner/repo-{index}" for index in range(4)})
        probe_started = threading.Event()
        release = threading.Event()

        def probe(name: str, repo: str, _headers: dict[str, str]) -> dict:
            probe_started.set()
            release.wait(5)
            return {"name": name, "repo": repo}

        completed: list[list[dict]] = []
        cancelled: list[bool] = []
        worker.finished.connect(completed.append)
        worker.cancelled.connect(lambda: cancelled.append(True))
        caller = threading.Thread(target=worker.run, name="update-check-caller")
        with patch.object(worker, "_check_component", side_effect=probe):
            caller.start()
            self.assertTrue(probe_started.wait(1))
            worker.cancel()
            caller.join(0.5)
            release.set()

        self.app.processEvents()
        self.assertFalse(caller.is_alive())
        self.assertFalse(completed)
        self.assertEqual(cancelled, [True])

    def test_download_worker_events_are_marshaled_to_service_thread(self) -> None:
        app = QCoreApplication.instance() or QCoreApplication([])
        import threading
        main_thread_id = threading.get_ident()
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "app.db")
            service = DownloadService(db)
            progress_thread_ids: list[int] = []
            finished_thread_ids: list[int] = []
            service.task_progress.connect(lambda *_args: progress_thread_ids.append(threading.get_ident()))
            service.task_finished.connect(lambda *_args: finished_thread_ids.append(threading.get_ident()))
            try:
                with patch("app.core.download_service.yt_dlp", None):
                    task_id = service.enqueue("https://example.com/video", directory)
                    deadline = time.monotonic() + 3
                    while (service.threads or not progress_thread_ids or not finished_thread_ids) and time.monotonic() < deadline:
                        app.processEvents()
                        time.sleep(0.002)
                    app.processEvents()

                self.assertFalse(service.threads)
                self.assertTrue(progress_thread_ids)
                self.assertTrue(finished_thread_ids)
                self.assertEqual(set(progress_thread_ids), {main_thread_id})
                self.assertEqual(set(finished_thread_ids), {main_thread_id})
                self.assertEqual(service.tasks[task_id].status, "failed")
            finally:
                service.shutdown(timeout_ms=0)
                db.close()

    def test_worker_progress_is_coalesced_before_entering_the_gui_queue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "app.db")
            worker = DownloadWorker(
                "download-and-transcode",
                "https://example.com/video",
                directory,
                db,
            )
            download_events: list[dict] = []
            worker.progress.connect(lambda _task_id, payload: download_events.append(payload))

            with patch("app.core.download_service.time.monotonic", return_value=10.0):
                for percent in range(1000):
                    worker._set_stage("transcoding", "正在转换视频格式", percent / 10.0)
                worker._set_stage("verifying", "正在校验转换临时成品", 0.0)

            # The first conversion update and the stage transition are
            # immediate, but the 999 redundant same-stage callbacks never
            # enter Qt's queued GUI event stream.
            self.assertEqual(len(download_events), 2)
            self.assertEqual(download_events[0]["stage"], "transcoding")
            self.assertEqual(download_events[1]["stage"], "verifying")

            manual = CompletedMediaTranscodeWorker(
                "manual-conversion",
                str(Path(directory) / "video.mp4"),
                "ffmpeg.exe",
                "ffprobe.exe",
                "libx264",
            )
            conversion_events: list[dict] = []
            manual.progress.connect(lambda _task_id, payload: conversion_events.append(payload))
            with patch("app.core.download_service.time.monotonic", return_value=20.0):
                for percent in range(1000):
                    manual._progress("transcoding", "正在转换视频格式", percent / 10.0)
                manual._progress("transcoding", "正在转换视频格式", 100.0)
                manual._progress("verifying", "正在校验转换后的媒体文件", 0.0)

            self.assertEqual(len(conversion_events), 3)
            self.assertEqual(conversion_events[-1]["stage"], "verifying")
            db.close()

    def test_database_uses_wal_normal_sync_and_busy_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "app.db")
            self.assertEqual(db.conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            self.assertEqual(int(db.conn.execute("PRAGMA synchronous").fetchone()[0]), 1)
            self.assertEqual(int(db.conn.execute("PRAGMA busy_timeout").fetchone()[0]), 5000)
            self.assertEqual(int(db.conn.execute("PRAGMA foreign_keys").fetchone()[0]), 0)
            indexes = {row[1] for row in db.conn.execute("PRAGMA index_list(publish_tasks)").fetchall()}
            self.assertIn("idx_publish_tasks_media_platform_id", indexes)
            db.close()

    def test_publish_distribution_summary_is_reduced_per_media_platform_in_sql(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "app.db")
            tasks = (
                PublishTask(media_id=1, platform="douyin", status="failed", idempotency_key="d-1"),
                PublishTask(media_id=1, platform="douyin", status="pending", idempotency_key="d-2"),
                PublishTask(media_id=1, platform="douyin", status="success", idempotency_key="d-3"),
                PublishTask(media_id=1, platform="douyin", status="failed", idempotency_key="d-4"),
                PublishTask(media_id=1, platform="bilibili", status="pending", idempotency_key="b-1"),
                PublishTask(media_id=1, platform="bilibili", status="failed", idempotency_key="b-2"),
                PublishTask(media_id=2, platform="youtube", status="failed", idempotency_key="y-1"),
                PublishTask(media_id=2, platform="youtube", status="uploading", idempotency_key="y-2"),
            )
            for task in tasks:
                db.add_publish_task(task)

            statements: list[str] = []
            db.conn.set_trace_callback(statements.append)
            summary = db.publish_statuses_by_media()
            db.conn.set_trace_callback(None)

            self.assertEqual(
                summary,
                {
                    1: {"douyin": "success", "bilibili": "failed"},
                    2: {"youtube": "uploading"},
                },
            )
            summary_query = next(statement for statement in statements if "ROW_NUMBER() OVER" in statement)
            self.assertIn("PARTITION BY media_id, platform", summary_query)
            db.close()

    def test_live_distribution_refresh_queries_only_one_media(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "app.db")
            for task in (
                PublishTask(media_id=1, platform="douyin", status="success", idempotency_key="one-d"),
                PublishTask(media_id=1, platform="bilibili", status="failed", idempotency_key="one-b"),
                PublishTask(media_id=2, platform="youtube", status="uploading", idempotency_key="two-y"),
            ):
                db.add_publish_task(task)

            statements: list[str] = []
            db.conn.set_trace_callback(statements.append)
            states = db.publish_statuses_for_media(1)
            db.conn.set_trace_callback(None)

            self.assertEqual(states, {"douyin": "success", "bilibili": "failed"})
            query = next(statement for statement in statements if "ROW_NUMBER() OVER" in statement)
            self.assertIn("media_id=1", query.replace(" ", ""))
            self.assertNotIn("youtube", states)
            db.close()

    def test_download_task_batch_upsert_commits_a_complete_page(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "app.db")
            tasks = [
                DownloadTask(
                    f"task-{index}",
                    f"https://example.com/{index}",
                    directory,
                    title=f"Task {index}",
                    progress=float(index),
                )
                for index in range(75)
            ]
            db.upsert_download_tasks(tasks)
            rows = db.list_download_tasks()
            self.assertEqual(len(rows), 75)
            self.assertEqual({row["id"] for row in rows}, {task.id for task in tasks})
            db.close()

    def test_media_catalog_page_and_distribution_counts_avoid_full_materialization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "app.db")
            media_ids = []
            for index in range(75):
                media_ids.append(
                    db.add_media(
                        MediaItem(
                            source_url=f"https://example.com/media/{index}",
                            title=f"Media {index}",
                            video_path=str(Path(directory) / f"{index}.mp4"),
                        )
                    )
                )
            # Newest-first paging must not deserialize all 75 rows.
            first_page = db.list_media(limit=20, offset=0)
            second_page = db.list_media(limit=20, offset=20)
            self.assertEqual(len(first_page), 20)
            self.assertEqual(len(second_page), 20)
            self.assertEqual(first_page[0].id, media_ids[-1])
            self.assertEqual(second_page[0].id, media_ids[-21])
            self.assertEqual(db.count_media(), 75)

            db.add_publish_task(
                PublishTask(
                    media_id=media_ids[-1],
                    platform="douyin",
                    status="success",
                    idempotency_key="page-douyin",
                )
            )
            db.add_publish_task(
                PublishTask(
                    media_id=media_ids[-2],
                    platform="douyin",
                    status="failed",
                    idempotency_key="page-failed",
                )
            )
            counts = db.media_distribution_counts(("douyin", "bilibili"))
            self.assertEqual(counts["all"], 75)
            self.assertEqual(counts["published"], 1)
            self.assertEqual(counts["retry_needed"], 1)
            self.assertEqual(counts["complete"], 0)
            self.assertEqual(counts["needs_distribution"], 75)
            db.close()

    def test_publish_status_page_query_only_returns_requested_media_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "app.db")
            first = db.add_media(MediaItem(source_url="https://example.com/first", title="first"))
            second = db.add_media(MediaItem(source_url="https://example.com/second", title="second"))
            db.add_publish_task(
                PublishTask(media_id=first, platform="douyin", status="success", idempotency_key="first")
            )
            db.add_publish_task(
                PublishTask(media_id=second, platform="bilibili", status="pending", idempotency_key="second")
            )
            self.assertEqual(
                db.publish_statuses_for_media_ids((first,)),
                {first: {"douyin": "success"}},
            )
            db.close()

    def test_publish_queue_page_and_count_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "app.db")
            for index in range(125):
                db.add_publish_task(
                    PublishTask(
                        media_id=index + 1,
                        platform="douyin" if index % 2 else "bilibili",
                        status="pending",
                        title=f"Publish {index}",
                        idempotency_key=f"queue-{index}",
                    )
                )
            first = db.list_publish_tasks(limit=50, offset=0)
            second = db.list_publish_tasks(limit=50, offset=50)
            scoped = db.list_publish_tasks(limit=10, offset=0, media_id=100)
            self.assertEqual(len(first), 50)
            self.assertEqual(len(second), 50)
            self.assertEqual(len(scoped), 1)
            self.assertEqual(int(first[0]["id"]), 125)
            self.assertEqual(db.count_publish_tasks(), 125)
            self.assertEqual(db.count_publish_tasks(media_id=100), 1)
            db.close()

    def test_progress_updates_are_coalesced_before_one_database_transaction(self) -> None:
        QCoreApplication.instance() or QCoreApplication([])
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "app.db")
            service = DownloadService(db)
            first = DownloadTask("first", "https://example.com/1", directory, progress=10)
            second = DownloadTask("second", "https://example.com/2", directory, progress=20)
            service._progress_persistence.persisted_at = {
                "first": time.monotonic(),
                "second": time.monotonic(),
            }
            with patch.object(db, "update_download_tasks", wraps=db.update_download_tasks) as batch:
                service._persist_progress(first)
                service._persist_progress(second)
                self.assertEqual(batch.call_count, 0)
                service._flush_progress_persists()
                self.assertEqual(batch.call_count, 1)
                self.assertEqual({task.id for task in batch.call_args.args[0]}, {"first", "second"})
            service.shutdown(timeout_ms=0)
            db.close()

    def test_failed_progress_batch_is_retained_and_retried(self) -> None:
        QCoreApplication.instance() or QCoreApplication([])
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "app.db")
            service = DownloadService(db)
            first = DownloadTask("retry-first", "https://example.com/1", directory, progress=10)
            second = DownloadTask("retry-second", "https://example.com/2", directory, progress=20)
            db.upsert_download_tasks([first, second])
            service._register_task(first)
            service._register_task(second)
            service._progress_persistence.persisted_at = {
                first.id: time.monotonic(),
                second.id: time.monotonic(),
            }
            first.progress = 30
            second.progress = 40
            service._persist_progress(first)
            service._persist_progress(second)
            original = db.update_download_tasks

            with patch.object(
                db,
                "update_download_tasks",
                side_effect=[RuntimeError("database busy"), None],
            ) as batch:
                service._flush_progress_persists()
                self.assertEqual(
                    set(service._progress_persistence.pending),
                    {first.id, second.id},
                )
                self.assertTrue(service._progress_flush_timer.isActive())
                service._progress_flush_timer.stop()
                batch.side_effect = lambda tasks: original(tasks)
                service._flush_progress_persists()

            self.assertEqual(service._progress_persistence.pending, {})
            rows = {row["id"]: row for row in db.list_download_tasks()}
            self.assertEqual(float(rows[first.id]["progress"]), 30.0)
            self.assertEqual(float(rows[second.id]["progress"]), 40.0)
            service.shutdown(timeout_ms=0)
            db.close()

    def test_immediate_progress_persist_failure_does_not_block_ui_update(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "app.db")
            task = DownloadTask(
                "immediate-progress-failure",
                "https://example.com/progress-failure",
                directory,
                status="downloading",
            )
            db.upsert_download_task(task)
            service = DownloadService(db)
            service._register_task(task)
            emitted: list[dict] = []
            service.task_progress.connect(
                lambda task_id, payload: emitted.append(dict(payload))
                if task_id == task.id else None
            )

            with patch.object(
                db,
                "update_download_task",
                side_effect=RuntimeError("database busy"),
            ) as single:
                service._on_progress(task.id, {
                    "downloaded_bytes": 50,
                    "total_bytes": 100,
                    "status": "downloading",
                })
                service._on_progress(task.id, {
                    "downloaded_bytes": 60,
                    "total_bytes": 100,
                    "status": "downloading",
                })

            self.assertEqual(single.call_count, 1)
            self.assertGreaterEqual(len(emitted), 1)
            self.assertIs(service._progress_persistence.pending[task.id], task)
            self.assertTrue(service._progress_flush_timer.isActive())
            service._progress_flush_timer.stop()
            service.shutdown(timeout_ms=0)
            db.close()

    def test_progress_batch_isolates_deleted_row_and_commits_healthy_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "app.db")
            healthy = DownloadTask(
                "healthy-progress",
                "https://example.com/healthy-progress",
                directory,
                progress=10,
            )
            stale = DownloadTask(
                "stale-progress",
                "https://example.com/stale-progress",
                directory,
                progress=20,
            )
            db.upsert_download_tasks([healthy, stale])
            db.delete_download_task(stale.id)
            healthy.progress = 75
            stale.progress = 80
            service = DownloadService(db)
            service._progress_persistence.pending[healthy.id] = healthy
            service._progress_persistence.pending[stale.id] = stale

            service._flush_progress_persists()

            rows = {row["id"]: row for row in db.list_download_tasks()}
            self.assertEqual(float(rows[healthy.id]["progress"]), 75.0)
            self.assertNotIn(stale.id, rows)
            self.assertEqual(service._progress_persistence.pending, {})
            service.shutdown(timeout_ms=0)
            db.close()

    def test_shutdown_retries_transient_progress_flush_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "app.db")
            task = DownloadTask(
                "shutdown-progress-retry",
                "https://example.com/shutdown-progress-retry",
                directory,
                progress=10,
            )
            db.upsert_download_task(task)
            task.progress = 90
            service = DownloadService(db)
            service._progress_persistence.pending[task.id] = task
            original = db.update_download_tasks
            attempts = 0

            def transient_failure(tasks):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise RuntimeError("database busy")
                return original(tasks)

            with patch.object(
                db,
                "update_download_tasks",
                side_effect=transient_failure,
            ) as batch:
                service.shutdown(timeout_ms=0)

            self.assertEqual(batch.call_count, 2)
            self.assertEqual(service._progress_persistence.pending, {})
            row = db.list_download_tasks()[0]
            self.assertEqual(float(row["progress"]), 90.0)
            db.close()

    def test_velopack_style_layout_prefers_persistent_tool_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app_root = root / "current"
            data_root = root / "data"
            app_root.mkdir()
            data_root.mkdir()
            roots = tool_runtime_roots(app_root, data_root)
            self.assertEqual(roots[:2], [data_root.resolve(), app_root.resolve()])

    def test_restore_completed_tasks_uses_one_batched_media_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Database(root / "app.db")
            tasks = []
            for index in range(20):
                url = f"https://example.com/video/{index}"
                video_path = str(root / f"video-{index}.mp4")
                Path(video_path).write_bytes(b"media")
                db.add_media(MediaItem(source_url=url, title=f"Video {index}", video_path=video_path))
                task = DownloadTask(f"restore-{index}", url, directory, status="completed")
                db.upsert_download_task(task)
                tasks.append(task)

            service = DownloadService(db)
            with patch.object(db, "latest_media_by_source_urls", wraps=db.latest_media_by_source_urls) as batch, patch.object(
                db, "get_latest_media_for_url", side_effect=AssertionError("N+1 lookup must not run")
            ):
                restored = service.restore_tasks()
            self.assertEqual(batch.call_count, 1)
            self.assertEqual(len(restored), 20)
            self.assertTrue(all(task.media_path for task in restored))
            service.shutdown(timeout_ms=0)
            db.close()

    def test_download_task_statistics_and_speed_use_incremental_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "app.db")
            service = DownloadService(db)
            parent = DownloadTask(
                "parent", "https://example.com/list", directory,
                task_kind="collection", status="downloading", speed_bps=5_000,
            )
            child = DownloadTask(
                "child", "https://example.com/video", directory,
                parent_task_id=parent.id, root_task_id=parent.id,
                status="downloading", speed_bps=5_000,
            )
            service._register_task(parent)
            service._register_task(child)

            top_level_stats = service.task_statistics(top_level_only=True)
            self.assertEqual(top_level_stats["total"], 1)
            self.assertEqual(top_level_stats["pausable"], 1)
            self.assertEqual(top_level_stats["resumable"], 0)
            self.assertEqual(top_level_stats["cleanable"], 0)
            self.assertEqual(service.task_statistics()["active"], 2)
            self.assertEqual(service.total_speed_bps(), 5_000)

            child.status = "completed"
            child.speed_bps = 0
            service._sync_task_indexes(child)
            self.assertEqual(service.task_statistics()["completed"], 1)
            self.assertEqual(service.total_speed_bps(), 0)

            service._unregister_task(child.id)
            self.assertEqual(service.task_statistics()["total"], 1)
            service.shutdown(timeout_ms=0)
            db.close()

    def test_post_download_transcode_progress_does_not_commit_on_every_callback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "app.db")
            service = DownloadService(db)
            task = DownloadTask(
                "transcode-with-parallel-download",
                "https://example.com/video",
                directory,
                status="downloading",
                progress=100.0,
                stage="transcoding",
                stage_progress=1.0,
            )
            service._register_task(task)
            service._progress_persistence.persisted_at[task.id] = time.monotonic()

            with patch.object(db, "update_download_task", wraps=db.update_download_task) as single, patch.object(
                db, "update_download_tasks", wraps=db.update_download_tasks
            ) as batch:
                for percent in range(2, 22, 2):
                    service._on_progress(task.id, {
                        "stage": "transcoding",
                        "stage_text": "正在转换视频格式",
                        "stage_progress": float(percent),
                        "transcode_encoder": "h264_nvenc",
                    })
                self.assertEqual(single.call_count, 0)
                self.assertEqual(batch.call_count, 0)
                service._flush_progress_persists()
                self.assertEqual(batch.call_count, 1)

            service.shutdown(timeout_ms=0)
            db.close()

    def test_manual_conversion_progress_is_transient_and_coalesced_for_ui(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "app.db")
            service = DownloadService(db)
            task = DownloadTask(
                "manual-transcode-with-download",
                "https://example.com/video",
                directory,
                status="processing",
                progress=100.0,
                stage="transcoding",
                stage_progress=1.0,
            )
            service._register_task(task)
            service._progress_persistence.persisted_at[task.id] = time.monotonic()
            service._last_progress_emit[task.id] = time.monotonic()
            emitted: list[dict] = []
            service.task_progress.connect(
                lambda task_id, payload: emitted.append(payload) if task_id == task.id else None
            )

            with patch.object(db, "update_download_task", wraps=db.update_download_task) as single:
                for percent in range(2, 22, 2):
                    service._on_completed_conversion_progress(task.id, {
                        "stage": "transcoding",
                        "stage_text": "正在转换视频格式",
                        "stage_progress": float(percent),
                        "transcode_encoder": "h264_nvenc",
                    })
                self.assertEqual(single.call_count, 0)
                self.assertEqual(emitted, [])
                self.assertNotIn(task.id, service._progress_persistence.pending)
                self.assertEqual(service._task_index.states[task.id][1], "processing")

            service.shutdown(timeout_ms=0)
            db.close()

    def test_progress_stage_transition_bypasses_ui_throttle_and_clears_stale_speed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "app.db")
            service = DownloadService(db)
            task = DownloadTask(
                "local-processing-stage",
                "https://example.com/video",
                directory,
                status="downloading",
                progress=100.0,
                stage="downloading",
                speed="8.0 MiB/s",
                speed_bps=8 * 1024 * 1024,
                eta="00:01",
            )
            task.speed_samples.extend((8 * 1024 * 1024, 8 * 1024 * 1024))
            service._register_task(task)
            service._progress_persistence.persisted_at[task.id] = time.monotonic()
            service._last_progress_emit[task.id] = time.monotonic()
            emitted: list[dict] = []
            service.task_progress.connect(
                lambda task_id, payload: emitted.append(payload) if task_id == task.id else None
            )

            service._on_progress(task.id, {
                "stage": "transcoding",
                "stage_text": "正在转换视频格式",
                "stage_progress": 0.0,
            })

            self.assertEqual(len(emitted), 1)
            self.assertEqual(task.stage, "transcoding")
            self.assertEqual(task.speed_bps, 0.0)
            self.assertEqual(task.speed, "")
            self.assertEqual(task.eta, "")
            self.assertFalse(task.speed_samples)
            self.assertEqual(service.total_speed_bps(), 0.0)

            service.shutdown(timeout_ms=0)
            db.close()

    def test_every_stage_or_stream_transition_invalidates_previous_rate_estimate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "app.db")
            service = DownloadService(db)

            for index, next_stage in enumerate((
                "downloading_audio",
                "reconnecting",
                "waiting_disk",
                "parsing",
            )):
                with self.subTest(next_stage=next_stage):
                    task = DownloadTask(
                        f"rate-reset-{index}",
                        "https://example.com/video",
                        directory,
                        status="downloading",
                        stage="downloading_video",
                        speed="8.0 MiB/s",
                        speed_bps=8 * 1024 * 1024,
                        eta="00:10",
                    )
                    task.speed_samples.extend((7.0, 8.0))

                    changed = service._apply_stage_progress(task, {
                        "stage": next_stage,
                        "stage_text": next_stage,
                    })

                    self.assertTrue(changed)
                    self.assertEqual(task.speed_bps, 0.0)
                    self.assertEqual(task.speed, "")
                    self.assertEqual(task.eta, "")
                    self.assertFalse(task.speed_samples)

            same_stage = DownloadTask(
                "same-stage-rate",
                "https://example.com/video",
                directory,
                status="downloading",
                stage="downloading",
                speed="4.0 MiB/s",
                speed_bps=4 * 1024 * 1024,
                eta="00:20",
            )
            same_stage.speed_samples.extend((4.0, 4.0))
            changed = service._apply_stage_progress(same_stage, {
                "stage": "downloading",
                "stage_text": "Still downloading",
            })
            self.assertFalse(changed)
            self.assertEqual(same_stage.speed_bps, 4 * 1024 * 1024)
            self.assertEqual(same_stage.eta, "00:20")
            self.assertEqual(list(same_stage.speed_samples), [4.0, 4.0])

            service.shutdown(timeout_ms=0)
            db.close()

    def test_progress_payload_with_invalid_numbers_does_not_break_task_updates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "app.db")
            service = DownloadService(db)
            task = DownloadTask(
                "malformed-progress",
                "https://example.com/video",
                directory,
                status="downloading",
                stage="downloading",
                progress=35.0,
                downloaded_bytes=350,
                total_bytes=1_000,
                speed_bps=1_024,
                video_progress=25.0,
                retry_count=2,
                retry_total=5,
            )
            db.insert_download_task(task)
            service._register_task(task)

            service._on_progress(task.id, {
                "stage": "downloading",
                "stage_progress": float("nan"),
                "retry_count": "invalid",
                "retry_total": float("inf"),
                "elapsed_seconds": "invalid",
                "total_bytes": float("inf"),
                "downloaded_bytes": "invalid",
                "_percent_str": "unknown%",
                "speed": "invalid",
                "stream_kind": "video",
                "stream_progress": float("nan"),
                "storage_preview": {
                    "temporary_bytes": "invalid",
                    "final_bytes": float("inf"),
                },
                "info_dict": "invalid",
            })

            self.assertEqual(task.progress, 35.0)
            self.assertEqual(task.downloaded_bytes, 350)
            self.assertEqual(task.total_bytes, 1_000)
            self.assertEqual(task.video_progress, 25.0)
            self.assertEqual(task.retry_count, 2)
            self.assertEqual(task.retry_total, 5)
            self.assertEqual(task.options_json["_storage_preview"]["temporary_bytes"], 0)
            self.assertEqual(task.options_json["_storage_preview"]["final_bytes"], 0)

            service.shutdown(timeout_ms=0)
            db.close()

    def test_progress_payload_can_explicitly_reset_retry_counters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "app.db")
            service = DownloadService(db)
            task = DownloadTask(
                "retry-reset",
                "https://example.com/video",
                directory,
                status="downloading",
                stage="reconnecting",
                retry_count=2,
                retry_total=5,
                reconnect_message="2 秒后重试",
            )
            service._register_task(task)

            service._on_progress(task.id, {
                "stage": "parsing",
                "stage_text": "正在重新解析",
                "retry_count": 0,
                "retry_total": 0,
            })

            self.assertEqual(task.retry_count, 0)
            self.assertEqual(task.retry_total, 0)
            self.assertEqual(task.reconnect_message, "")

            service.shutdown(timeout_ms=0)
            db.close()

    def test_progress_numeric_fallbacks_tolerate_corrupted_task_values(self) -> None:
        self.assertEqual(DownloadService._progress_int("invalid", "also-invalid"), 0)
        self.assertEqual(DownloadService._progress_int(float("inf"), float("nan")), 0)
        huge_counter = 10**30 + 123
        self.assertEqual(DownloadService._progress_int(huge_counter), huge_counter)
        self.assertEqual(DownloadService._progress_float("invalid", "also-invalid"), 0.0)
        self.assertEqual(
            DownloadService._progress_float(float("-inf"), float("nan")),
            0.0,
        )

        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "app.db")
            service = DownloadService(db)
            task = DownloadTask(
                "corrupted-progress-default",
                "https://example.com/video",
                directory,
                status="downloading",
            )
            task.video_progress = "not-a-number"
            db.insert_download_task(task)
            service._register_task(task)

            service._on_progress(task.id, {
                "stream_kind": "video",
                "stream_progress": float("nan"),
            })

            self.assertEqual(task.video_progress, 0.0)
            service.shutdown(timeout_ms=0)
            db.close()

    def test_duplicate_progress_is_rejected_before_mutating_transfer_state(self) -> None:
        class _CancelProbe:
            def __init__(self) -> None:
                self.reasons: list[str] = []

            def cancel(self, reason: str) -> None:
                self.reasons.append(reason)

        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "app.db")
            service = DownloadService(db)
            canonical = DownloadTask(
                "canonical-progress-task",
                "https://example.com/watch/original",
                directory,
                status="downloading",
                source_key="example:video-1",
                title="Resolved video",
            )
            duplicate = DownloadTask(
                "duplicate-progress-task",
                "https://short.example/video-1",
                directory,
                status="downloading",
                stage="downloading",
                stage_text="Downloading",
                progress=5.0,
            )
            service._register_task(canonical)
            service._register_task(duplicate)
            cancel_probe = _CancelProbe()
            service.workers[duplicate.id] = cancel_probe

            service._on_progress(duplicate.id, {
                "stage": "transcoding",
                "stage_text": "Should not be applied",
                "stage_progress": 60.0,
                "downloaded_bytes": 600,
                "total_bytes": 1_000,
                "selected_quality": "4K",
                "storage_preview": {"known": True, "temporary_bytes": 1_000},
                "info_dict": {
                    "extractor_key": "Example",
                    "id": "video-1",
                    "title": "Resolved video",
                },
            })

            self.assertEqual(cancel_probe.reasons, ["discard"])
            self.assertIn(duplicate.id, service._discard_tasks)
            self.assertEqual(duplicate.source_key, "example:video-1")
            self.assertEqual(duplicate.title, "Resolved video")
            self.assertEqual(duplicate.stage, "downloading")
            self.assertEqual(duplicate.stage_text, "Downloading")
            self.assertEqual(duplicate.progress, 5.0)
            self.assertEqual(duplicate.downloaded_bytes, 0)
            self.assertEqual(duplicate.total_bytes, 0)
            self.assertEqual(duplicate.selected_quality, "")
            self.assertNotIn("_storage_preview", duplicate.options_json)

            service.workers.clear()
            service.shutdown(timeout_ms=0)
            db.close()


if __name__ == "__main__":
    unittest.main()

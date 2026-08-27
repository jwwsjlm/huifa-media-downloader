from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QThread

from app.core.download_service import DownloadService, DownloadTask
from app.core.publish_service import AccountWorker, PublishService, PublishWorker
from app.core.update_service import UpdateService, _UpdateRuntime
from app.storage.database import Database
from app.storage.models import MediaItem, PublishTask


class FakeThread:
    def __init__(self, running: bool = True) -> None:
        self.running = running
        self.interrupted = False
        self.quit_requested = False

    def isRunning(self) -> bool:
        return self.running

    def requestInterruption(self) -> None:
        self.interrupted = True

    def quit(self) -> None:
        self.quit_requested = True

    def wait(self, _timeout: int) -> bool:
        return not self.running


class FakeDownloadWorker:
    def __init__(self) -> None:
        self.reasons: list[str] = []

    def cancel(self, reason: str = "cancel") -> None:
        self.reasons.append(reason)


class FakeWorker:
    def __init__(self) -> None:
        self.cancelled = 0

    def cancel(self) -> None:
        self.cancelled += 1


class ShutdownLifecycleTests(unittest.TestCase):
    def test_download_shutdown_is_non_blocking_idempotent_and_stops_new_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "app.db")
            service = DownloadService(db)
            task = DownloadTask("active", "https://example.com/video", directory, status="downloading")
            worker = FakeDownloadWorker()
            thread = FakeThread()
            db.upsert_download_task(task)
            service._register_task(task)
            service.workers[task.id] = worker
            service.threads[task.id] = thread

            self.assertFalse(service.shutdown(timeout_ms=0))
            self.assertEqual(worker.reasons, ["shutdown"])
            self.assertTrue(thread.interrupted)
            self.assertTrue(thread.quit_requested)
            self.assertEqual(task.status, "暂停中")
            self.assertEqual(service.active_thread_count, 1)

            # Polling from the GUI must not repeatedly write/cancel the task.
            self.assertFalse(service.shutdown(timeout_ms=0))
            self.assertEqual(worker.reasons, ["shutdown"])
            with self.assertRaisesRegex(RuntimeError, "程序正在退出"):
                service.enqueue("https://example.com/new", directory)

            thread.running = False
            self.assertFalse(service.shutdown(timeout_ms=0))
            service._thread_finished(task.id)
            self.assertTrue(service.shutdown(timeout_ms=0))
            db.close()

    def test_download_shutdown_cancels_every_worker_when_state_and_log_writes_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "app.db")
            service = DownloadService(db)
            workers: dict[str, FakeDownloadWorker] = {}
            threads: dict[str, FakeThread] = {}
            for task_id in ("first", "second"):
                task = DownloadTask(
                    task_id,
                    f"https://example.com/{task_id}",
                    directory,
                    status="downloading",
                )
                db.upsert_download_task(task)
                service._register_task(task)
                workers[task_id] = FakeDownloadWorker()
                threads[task_id] = FakeThread()
            service.workers.update(workers)  # type: ignore[arg-type]
            service.threads.update(threads)  # type: ignore[arg-type]

            with patch.object(
                service,
                "_persist",
                side_effect=RuntimeError("database unavailable"),
            ), patch.object(
                service.logs,
                "write",
                side_effect=OSError("log unavailable"),
            ), patch.object(
                service.logs,
                "flush",
                side_effect=OSError("log unavailable"),
            ):
                service.request_shutdown()

            self.assertEqual(workers["first"].reasons, ["shutdown"])
            self.assertEqual(workers["second"].reasons, ["shutdown"])
            self.assertTrue(all(thread.interrupted for thread in threads.values()))
            self.assertTrue(all(thread.quit_requested for thread in threads.values()))
            self.assertTrue(all(task.status == "暂停中" for task in service.tasks.values()))

            service.workers.clear()
            service.threads.clear()
            self.assertTrue(service.shutdown(timeout_ms=0))
            db.close()

    def test_publish_shutdown_preserves_live_threads_until_they_finish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "app.db")
            service = PublishService(db)
            worker = FakeWorker()
            thread = FakeThread()
            service.workers[7] = worker
            service.threads[7] = thread

            self.assertFalse(service.shutdown(timeout_ms=0))
            self.assertEqual(worker.cancelled, 1)
            self.assertEqual(service.active_thread_count, 1)
            self.assertFalse(service.shutdown(timeout_ms=0))
            self.assertEqual(worker.cancelled, 1)

            thread.running = False
            self.assertFalse(service.shutdown(timeout_ms=0))
            service._thread_finished(7)
            self.assertTrue(service.shutdown(timeout_ms=0))
            db.close()

    def test_publish_thread_start_failure_marks_task_failed_and_cleans_runtime(self) -> None:
        QCoreApplication.instance() or QCoreApplication([])
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "app.db")
            media_id = db.add_media(MediaItem(
                source_url="https://example.com/publish-start-failure",
                title="Publish start failure",
                video_path=str(Path(directory) / "video.mp4"),
            ))
            task_id = db.add_publish_task(PublishTask(
                media_id=media_id,
                platform="douyin",
                idempotency_key="publish-start-failure",
            ))
            service = PublishService(db)
            events: list[tuple[int, str, str]] = []
            service.status.connect(
                lambda item_id, status, result: events.append((item_id, status, result))
            )

            with patch(
                "app.core.publish_service.QThread.start",
                side_effect=RuntimeError("thread resource exhausted"),
            ):
                service.run_task(task_id)

            row = db.get_publish_task(task_id)
            self.assertEqual(row["status"], "failed")
            self.assertIn("thread resource exhausted", row["result"])
            self.assertNotIn(task_id, service.threads)
            self.assertNotIn(task_id, service.workers)
            self.assertEqual(events[-1][1], "failed")
            service.shutdown(timeout_ms=0)
            db.close()

    def test_publish_runtime_wiring_failure_marks_task_failed_without_leaking_objects(self) -> None:
        QCoreApplication.instance() or QCoreApplication([])
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "app.db")
            media_id = db.add_media(MediaItem(
                source_url="https://example.com/publish-wiring-failure",
                title="Publish wiring failure",
                video_path=str(Path(directory) / "video.mp4"),
            ))
            task_id = db.add_publish_task(PublishTask(
                media_id=media_id,
                platform="douyin",
                idempotency_key="publish-wiring-failure",
            ))
            service = PublishService(db)
            events: list[tuple[int, str, str]] = []
            service.status.connect(
                lambda item_id, status, result: events.append((item_id, status, result))
            )
            worker = PublishWorker(
                task_id,
                db.get_publish_task(task_id),
                db.get_media(media_id),
            )

            with patch(
                "app.core.publish_service.PublishWorker",
                return_value=worker,
            ), patch.object(
                worker,
                "moveToThread",
                side_effect=RuntimeError("signal wiring failed"),
            ), patch(
                "app.core.publish_service.delete_unstarted_worker",
            ) as delete_worker:
                service.run_task(task_id)

            delete_worker.assert_called_once()
            self.assertEqual(service.threads, {})
            self.assertEqual(service.workers, {})
            row = db.get_publish_task(task_id)
            self.assertEqual(row["status"], "failed")
            self.assertIn("signal wiring failed", row["result"])
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0][1], "failed")
            db.close()

    def test_publish_thread_cleanup_waits_for_queued_result(self) -> None:
        QCoreApplication.instance() or QCoreApplication([])
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "app.db")
            task_id = db.add_publish_task(PublishTask(
                media_id=1,
                platform="douyin",
                status="pending",
                idempotency_key="publish-deferred-result",
            ))
            service = PublishService(db)
            service.threads[task_id] = QThread()
            service.workers[task_id] = FakeWorker()  # type: ignore[assignment]

            service._defer_publish_thread_finished(task_id)
            service._on_result(task_id, True, "published")
            QCoreApplication.processEvents()

            row = db.get_publish_task(task_id)
            self.assertEqual(row["status"], "success")
            self.assertEqual(row["result"], "published")
            self.assertNotIn(task_id, service.threads)
            self.assertNotIn(task_id, service.workers)
            service.shutdown(timeout_ms=0)
            db.close()

    def test_publish_retry_requested_before_cleanup_runs_after_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "app.db")
            task_id = db.add_publish_task(PublishTask(
                media_id=1,
                platform="douyin",
                status="failed",
                result="upload failed",
                idempotency_key="publish-retry-after-release",
            ))
            service = PublishService(db)
            service.threads[task_id] = FakeThread()  # type: ignore[assignment]
            service.workers[task_id] = FakeWorker()  # type: ignore[assignment]

            with patch.object(service, "run_task") as run_task:
                service.retry_task(task_id)
                service.retry_task(task_id)

                self.assertIn(task_id, service._pending_publish_retries)
                run_task.assert_not_called()
                service._thread_finished(task_id)

            self.assertNotIn(task_id, service._pending_publish_retries)
            self.assertNotIn(task_id, service.threads)
            self.assertNotIn(task_id, service.workers)
            run_task.assert_called_once_with(task_id)
            db.close()

    def test_account_thread_start_failure_reports_result_and_cleans_runtime(self) -> None:
        QCoreApplication.instance() or QCoreApplication([])
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "app.db")
            service = PublishService(db)
            events: list[tuple[str, str, str, bool, str]] = []
            service.account_status.connect(
                lambda *values: events.append(values)  # type: ignore[arg-type]
            )

            with patch(
                "app.core.publish_service.QThread.start",
                side_effect=RuntimeError("thread resource exhausted"),
            ):
                started = service.run_account_action("douyin", "default", "check")

            self.assertFalse(started)
            self.assertEqual(service.account_threads, {})
            self.assertEqual(service.account_workers, {})
            self.assertEqual(len(events), 1)
            self.assertFalse(events[0][3])
            self.assertIn("thread resource exhausted", events[0][4])
            service.shutdown(timeout_ms=0)
            db.close()

    def test_account_action_running_reports_owned_worker_or_thread(self) -> None:
        QCoreApplication.instance() or QCoreApplication([])
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "app.db")
            service = PublishService(db)
            key = "browser:download"

            self.assertFalse(service.is_account_action_running("browser", "download"))
            service.account_workers[key] = FakeWorker()  # type: ignore[assignment]
            self.assertTrue(service.is_account_action_running("browser", "download"))
            service.account_workers.clear()
            service.account_threads[key] = FakeThread()  # type: ignore[assignment]
            self.assertTrue(service.is_account_action_running("browser", "download"))

            service.account_threads.clear()
            service.shutdown(timeout_ms=0)
            db.close()

    def test_account_runtime_wiring_failure_reports_once_without_leaking_objects(self) -> None:
        QCoreApplication.instance() or QCoreApplication([])
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "app.db")
            service = PublishService(db)
            events: list[tuple[str, str, str, bool, str]] = []
            service.account_status.connect(
                lambda *values: events.append(values)  # type: ignore[arg-type]
            )
            worker = AccountWorker("douyin", "default", "check")

            with patch(
                "app.core.publish_service.AccountWorker",
                return_value=worker,
            ), patch.object(
                worker,
                "moveToThread",
                side_effect=RuntimeError("signal wiring failed"),
            ), patch(
                "app.core.publish_service.delete_unstarted_worker",
            ) as delete_worker:
                started = service.run_account_action("douyin", "default", "check")

            self.assertFalse(started)
            delete_worker.assert_called_once()
            self.assertEqual(service.account_threads, {})
            self.assertEqual(service.account_workers, {})
            self.assertEqual(len(events), 1)
            self.assertFalse(events[0][3])
            self.assertIn("signal wiring failed", events[0][4])
            db.close()

    def test_deferred_publish_cleanup_does_not_remove_replacement_runtime(self) -> None:
        QCoreApplication.instance() or QCoreApplication([])
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "app.db")
            service = PublishService(db)
            old_thread = QThread()
            replacement_thread = QThread()
            replacement_worker = FakeWorker()
            service._deferred_publish_finishes.add(old_thread)
            service.threads[7] = replacement_thread
            service.workers[7] = replacement_worker  # type: ignore[assignment]

            service._complete_deferred_publish_thread_finish(7, old_thread)

            self.assertIs(service.threads[7], replacement_thread)
            self.assertIs(service.workers[7], replacement_worker)
            self.assertNotIn(old_thread, service._deferred_publish_finishes)
            service._thread_finished(7)
            replacement_thread.deleteLater()
            QCoreApplication.processEvents()
            db.close()

    def test_deferred_account_cleanup_does_not_remove_replacement_runtime(self) -> None:
        QCoreApplication.instance() or QCoreApplication([])
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "app.db")
            service = PublishService(db)
            old_thread = QThread()
            replacement_thread = QThread()
            replacement_worker = FakeWorker()
            service._deferred_account_finishes.add(old_thread)
            service.account_threads["douyin:default"] = replacement_thread
            service.account_workers["douyin:default"] = replacement_worker  # type: ignore[assignment]

            service._complete_deferred_account_thread_finish(
                "douyin:default",
                old_thread,
            )

            self.assertIs(service.account_threads["douyin:default"], replacement_thread)
            self.assertIs(service.account_workers["douyin:default"], replacement_worker)
            self.assertNotIn(old_thread, service._deferred_account_finishes)
            service._account_thread_finished("douyin:default")
            replacement_thread.deleteLater()
            QCoreApplication.processEvents()
            db.close()

    def test_restore_requeues_only_genuinely_queued_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "app.db")
            statuses = ("queued", "downloading", "canceling", "暂停中", "waiting_selection", "paused")
            for status in statuses:
                db.upsert_download_task(
                    DownloadTask(
                        f"restore-{status}",
                        f"https://example.com/{status}",
                        directory,
                        status=status,
                    )
                )

            service = DownloadService(db)
            with patch.object(service, "_start_next") as start_next:
                restored = service.restore_tasks()

            restored_by_id = {task.id: task for task in restored}
            self.assertEqual(list(service.queue), ["restore-queued"])
            self.assertEqual(restored_by_id["restore-queued"].status, "queued")
            for status in ("downloading", "canceling", "暂停中", "waiting_selection", "paused"):
                self.assertEqual(restored_by_id[f"restore-{status}"].status, "paused")
            start_next.assert_called_once_with()
            rows = {row["id"]: row for row in db.list_download_tasks()}
            self.assertEqual(rows["restore-waiting_selection"]["status"], "paused")
            service.shutdown(timeout_ms=0)
            db.close()

    def test_tool_update_shutdown_never_force_terminates_live_thread(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = UpdateService(directory)
            worker = FakeWorker()
            thread = FakeThread()
            service._runtimes["check"] = _UpdateRuntime("check", thread, worker)

            self.assertFalse(service.shutdown(timeout_ms=0))
            self.assertEqual(worker.cancelled, 1)
            self.assertTrue(service.runtime_active("check"))
            self.assertEqual(service.active_thread_count, 1)

            thread.running = False
            self.assertTrue(service.shutdown(timeout_ms=0))
            self.assertFalse(service.runtime_active("check"))

    def test_tool_update_thread_start_failures_release_each_runtime(self) -> None:
        QCoreApplication.instance() or QCoreApplication([])
        with tempfile.TemporaryDirectory() as directory:
            service = UpdateService(directory)
            check_errors: list[str] = []
            route_errors: list[str] = []
            download_errors: list[str] = []
            install_errors: list[str] = []
            service.failed.connect(check_errors.append)
            service.route_probe_failed.connect(route_errors.append)
            service.download_failed.connect(download_errors.append)
            service.install_failed.connect(install_errors.append)

            with patch(
                "app.core.update_service.QThread.start",
                side_effect=RuntimeError("thread resource exhausted"),
            ):
                self.assertFalse(service.check(repos={}))
                self.assertFalse(service.probe_download_routes(
                    routes=(service.available_download_routes()[0],),
                ))
                service.download_asset({
                    "name": "deno.zip",
                    "browser_download_url": (
                        "https://github.com/denoland/deno/releases/download/v1/deno.zip"
                    ),
                    "digest": "sha256:" + "a" * 64,
                }, "Deno")
                service._download_component = "Deno"
                service._asset_downloaded(str(Path(directory) / "deno.zip"))

            self.assertEqual(service.active_thread_count, 0)
            for errors in (check_errors, route_errors, download_errors, install_errors):
                self.assertEqual(len(errors), 1)
                self.assertIn("thread resource exhausted", errors[0])

    def test_tool_update_wiring_failures_never_publish_busy_runtime(self) -> None:
        QCoreApplication.instance() or QCoreApplication([])
        with tempfile.TemporaryDirectory() as directory:
            service = UpdateService(directory)
            check_errors: list[str] = []
            route_errors: list[str] = []
            download_errors: list[str] = []
            install_errors: list[str] = []
            service.failed.connect(check_errors.append)
            service.route_probe_failed.connect(route_errors.append)
            service.download_failed.connect(download_errors.append)
            service.install_failed.connect(install_errors.append)

            with patch.object(
                service,
                "_connect_runtime_lifecycle",
                side_effect=RuntimeError("signal wiring failed"),
            ):
                self.assertFalse(service.check(repos={}))
                self.assertFalse(service.probe_download_routes(
                    routes=(service.available_download_routes()[0],),
                ))
                service.download_asset({
                    "name": "deno.zip",
                    "browser_download_url": (
                        "https://github.com/denoland/deno/"
                        "releases/download/v1/deno.zip"
                    ),
                    "digest": "sha256:" + "a" * 64,
                }, "Deno")
                service._download_component = "Deno"
                service._asset_downloaded(str(Path(directory) / "deno.zip"))

            self.assertEqual(service.active_thread_count, 0)
            self.assertEqual(service._download_component, "")
            self.assertFalse(service.runtime_active())
            for errors in (
                check_errors,
                route_errors,
                download_errors,
                install_errors,
            ):
                self.assertEqual(len(errors), 1)
                self.assertIn("signal wiring failed", errors[0])

    def test_tool_update_late_cleanup_does_not_clear_replacement_runtime(self) -> None:
        QCoreApplication.instance() or QCoreApplication([])
        with tempfile.TemporaryDirectory() as directory:
            service = UpdateService(directory)
            stale_thread = QThread()
            replacement_thread = QThread()
            replacement_worker = object()
            service._runtimes["download"] = _UpdateRuntime(
                "download",
                replacement_thread,
                replacement_worker,  # type: ignore[arg-type]
            )
            service._download_component = "Deno"

            service._clear_runtime_references("download", stale_thread)

            self.assertIs(service._runtimes["download"].thread, replacement_thread)
            self.assertIs(service._runtimes["download"].worker, replacement_worker)
            self.assertEqual(service._download_component, "Deno")
            service._clear_runtime_references("download", replacement_thread)
            stale_thread.deleteLater()
            replacement_thread.deleteLater()

    def test_tool_update_cleanup_waits_for_queued_result(self) -> None:
        QCoreApplication.instance() or QCoreApplication([])
        with tempfile.TemporaryDirectory() as directory:
            service = UpdateService(directory)
            thread = QThread()
            service._runtimes["check"] = _UpdateRuntime(
                "check",
                thread,
                object(),  # type: ignore[arg-type]
            )
            results = [{"name": "Deno", "latest": "2.5.0"}]
            finished: list[list[dict[str, str]]] = []
            service.finished.connect(finished.append)

            service._defer_runtime_cleanup("check", thread)
            service._check_completed(results)
            self.assertEqual(service.active_thread_count, 1)
            QCoreApplication.processEvents()

            self.assertEqual(finished, [results])
            self.assertEqual(service.last_results, results)
            self.assertFalse(service.runtime_active("check"))
            self.assertEqual(service.active_thread_count, 0)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QThread

from app.core.application_update_service import ApplicationUpdateService
from app.core.application_updater import (
    ApplicationUpdate,
    UpdateConfirmationRequired,
    UpdateDownloadCancelled,
    VelopackApplicationUpdater,
    VelopackUpdaterConfig,
)
from app.core.update_receipt import INSTALL_INTENT_FILENAME


def make_update(*, downloaded: bool = False, version: str = "0.2.0") -> ApplicationUpdate:
    return ApplicationUpdate(
        token=f"update-{version}",
        current_version="0.1.0",
        version=version,
        package_id="Huifa.VideoDownloader",
        file_name=f"Huifa.VideoDownloader-{version}-full.nupkg",
        size_bytes=42 * 1024 * 1024,
        sha256="a" * 64,
        release_notes_markdown="# 更新内容\n\n- 提升稳定性",
        is_downgrade=False,
        is_portable=True,
        downloaded=downloaded,
    )


class FakeUpdater:
    def __init__(self, config: VelopackUpdaterConfig) -> None:
        self.config = config
        self.check_result: ApplicationUpdate | None = None
        self.check_error: Exception | None = None
        self.pending_result: ApplicationUpdate | None = None
        self.pending_error: Exception | None = None
        self.download_error: Exception | None = None
        self.download_progress = [0, 35, 78, 100]
        self.check_calls = 0
        self.pending_calls = 0
        self.download_calls: list[ApplicationUpdate] = []
        self.schedule_calls: list[tuple[ApplicationUpdate, bool, bool]] = []
        self.schedule_error: Exception | None = None
        self.check_started = threading.Event()
        self.release_check: threading.Event | None = None

    def check_for_updates(self) -> ApplicationUpdate | None:
        self.check_calls += 1
        self.check_started.set()
        if self.release_check is not None:
            self.release_check.wait(2.0)
        if self.check_error is not None:
            raise self.check_error
        return self.check_result

    def pending_restart(self) -> ApplicationUpdate | None:
        self.pending_calls += 1
        if self.pending_error is not None:
            raise self.pending_error
        return self.pending_result

    def download_update(self, update, progress_callback, cancel_callback=None):
        self.download_calls.append(update)
        if self.download_error is not None:
            raise self.download_error
        for value in self.download_progress:
            if cancel_callback is not None and cancel_callback():
                raise RuntimeError("download cancelled")
            progress_callback(value)
        # The service must defensively mark the immutable UI model as ready
        # even when an adapter implementation returns the original object.
        return update

    def schedule_install_on_exit(self, update, *, confirmed, restart):
        if self.schedule_error is not None:
            raise self.schedule_error
        self.schedule_calls.append((update, confirmed, restart))


class RecordingFactory:
    def __init__(self) -> None:
        self.instances: list[FakeUpdater] = []

    def __call__(self, config: VelopackUpdaterConfig) -> FakeUpdater:
        updater = FakeUpdater(config)
        self.instances.append(updater)
        return updater


class CancellableDownloadUpdater(FakeUpdater):
    def __init__(self, config: VelopackUpdaterConfig) -> None:
        super().__init__(config)
        self.download_started = threading.Event()
        self.cancel_observed = threading.Event()

    def download_update(self, update, progress_callback, cancel_callback=None):
        self.download_calls.append(update)
        self.download_started.set()
        progress_callback(15)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if cancel_callback is not None and cancel_callback():
                self.cancel_observed.set()
                raise UpdateDownloadCancelled("paused")
            time.sleep(0.005)
        raise RuntimeError("download cancellation was not delivered")


class UnmanagedVelopackModule:
    """Minimal SDK shape for a source checkout not managed by Velopack."""

    def GithubSource(self, repository, access_token=None, prerelease=False):
        return repository, access_token, prerelease

    def UpdateOptions(self, allow_downgrade, maximum_deltas, channel=None):
        return allow_downgrade, maximum_deltas, channel

    def UpdateManager(self, _source, _options):
        raise RuntimeError("This application is not installed or managed by Velopack")


class ApplicationUpdateServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.services: list[ApplicationUpdateService] = []

    def tearDown(self) -> None:
        for service in self.services:
            updater = service._updater
            release = getattr(updater, "release_check", None)
            if release is not None:
                release.set()
            self.assertTrue(service.shutdown(timeout_ms=2000))
        self._pump_events(lambda: all(not service.busy for service in self.services))

    def create_service(self, factory=None) -> ApplicationUpdateService:
        service = ApplicationUpdateService(
            Path(self.temporary.name) / f"state-{len(self.services)}",
            updater_factory=factory,
        )
        self.services.append(service)
        return service

    def _pump_events(self, predicate, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                self.app.processEvents()
                return
            time.sleep(0.002)
        self.fail("等待 Qt 更新线程结束超时")

    def _wait_idle(self, service: ApplicationUpdateService) -> None:
        self._pump_events(
            lambda: service._thread is None and service._worker is None and not service.busy
        )

    def test_configure_builds_expected_config_reuses_equal_config_and_resets_state(self) -> None:
        factory = RecordingFactory()
        service = self.create_service(factory)

        service.configure(
            "huifa/yt-release",
            prerelease=True,
            access_token="private-token",
            channel="win-beta",
        )
        self.assertEqual(len(factory.instances), 1)
        config = factory.instances[0].config
        self.assertEqual(config.repository, "huifa/yt-release")
        self.assertTrue(config.prerelease)
        self.assertEqual(config.access_token, "private-token")
        self.assertEqual(config.channel, "win-beta")

        existing = make_update()
        service.current_update = existing
        service.configure(
            "huifa/yt-release",
            prerelease=True,
            access_token="private-token",
            channel="win-beta",
        )
        self.assertEqual(len(factory.instances), 1)
        self.assertIs(service.current_update, existing)

        service.configure("huifa/yt-release", channel="win")
        self.assertEqual(len(factory.instances), 2)
        self.assertIsNone(service.current_update)

    def test_empty_channel_follows_the_channel_embedded_in_the_install(self) -> None:
        factory = RecordingFactory()
        service = self.create_service(factory)

        service.configure("huifa/yt-release", channel="")

        self.assertEqual(len(factory.instances), 1)
        self.assertIsNone(factory.instances[0].config.channel)

    def test_failed_reconfigure_preserves_previous_working_configuration(self) -> None:
        created: list[FakeUpdater] = []

        def factory(config: VelopackUpdaterConfig) -> FakeUpdater:
            if config.repository == "broken/project":
                raise ValueError("仓库地址无效")
            updater = FakeUpdater(config)
            created.append(updater)
            return updater

        service = self.create_service(factory)
        service.configure("huifa/yt-release")
        original_config = service._config
        original_updater = service._updater
        existing = make_update()
        service.current_update = existing

        with self.assertRaisesRegex(ValueError, "仓库地址无效"):
            service.configure("broken/project")

        self.assertIs(service._config, original_config)
        self.assertIs(service._updater, original_updater)
        self.assertIs(service.current_update, existing)
        self.assertEqual(created, [original_updater])

    def test_clear_configuration_disables_the_previous_update_feed(self) -> None:
        factory = RecordingFactory()
        service = self.create_service(factory)
        service.configure("huifa/yt-release")
        service.current_update = make_update()

        service.clear_configuration()

        self.assertIsNone(service._config)
        self.assertIsNone(service._updater)
        self.assertIsNone(service.current_update)
        self.assertFalse(service.check())
        self.assertFalse(service.restore_pending_restart())

    def test_clear_configuration_detaches_after_the_running_update_exits(self) -> None:
        factory = RecordingFactory()
        service = self.create_service(factory)
        service.configure("huifa/yt-release")
        updater = factory.instances[0]
        updater.release_check = threading.Event()
        self.assertTrue(service.check())
        self.assertTrue(updater.check_started.wait(1.0))

        self.assertFalse(service.clear_configuration())
        self.assertIsNotNone(service._config)
        self.assertIs(service._updater, updater)
        updater.release_check.set()
        self._wait_idle(service)
        self.assertIsNone(service._config)
        self.assertIsNone(service._updater)
        self.assertIsNone(service.current_update)

    def test_unconfigured_and_busy_operations_are_rejected_without_starting_work(self) -> None:
        factory = RecordingFactory()
        service = self.create_service(factory)
        self.assertFalse(service.check())
        self.assertFalse(service.download())
        self.assertFalse(service.restore_pending_restart())

        service.configure("huifa/yt-release")
        updater = factory.instances[0]
        updater.release_check = threading.Event()
        self.assertTrue(service.check())
        self.assertTrue(updater.check_started.wait(1.0))
        self.assertFalse(service.check())
        self.assertFalse(service.download(make_update()))
        with self.assertRaisesRegex(RuntimeError, "仍在运行"):
            service.schedule_install_and_restart(
                make_update(downloaded=True),
                confirmed=True,
            )
        with self.assertRaisesRegex(RuntimeError, "暂时无法切换更新配置"):
            service.configure("other/project")
        updater.release_check.set()
        self._wait_idle(service)

    def test_manual_no_update_clears_stale_result_and_does_not_consume_auto_throttle(self) -> None:
        factory = RecordingFactory()
        service = self.create_service(factory)
        service.configure("huifa/yt-release")
        service.current_update = make_update()
        no_updates: list[bool] = []
        failures: list[str] = []
        busy_changes: list[bool] = []
        service.no_update.connect(lambda: no_updates.append(True))
        service.failed.connect(failures.append)
        service.busy_changed.connect(busy_changes.append)

        self.assertTrue(service.check())
        self._wait_idle(service)

        self.assertEqual(no_updates, [True])
        self.assertEqual(failures, [])
        self.assertEqual(busy_changes, [True, False])
        self.assertIsNone(service.current_update)
        self.assertTrue(service.is_auto_check_due())
        self.assertFalse((service.state_dir / "auto-check.json").exists())

    def test_automatic_check_marks_repository_and_enforces_throttle_in_service(self) -> None:
        factory = RecordingFactory()
        service = self.create_service(factory)
        service.configure("huifa/yt-release")

        self.assertTrue(service.is_auto_check_due())
        self.assertTrue(service.check(automatic=True))
        self._wait_idle(service)
        self.assertFalse(service.is_auto_check_due())
        self.assertTrue((service.state_dir / "auto-check.json").is_file())

        self.assertFalse(service.check(automatic=True))
        self.assertEqual(factory.instances[0].check_calls, 1)

    def test_failed_automatic_check_does_not_consume_throttle_window(self) -> None:
        factory = RecordingFactory()
        service = self.create_service(factory)
        service.configure("huifa/yt-release")
        factory.instances[0].check_error = RuntimeError("network unavailable")

        self.assertTrue(service.check(automatic=True))
        self._wait_idle(service)

        self.assertTrue(service.is_auto_check_due())
        self.assertFalse((service.state_dir / "auto-check.json").exists())

    def test_available_update_is_exposed_and_retained_for_download(self) -> None:
        factory = RecordingFactory()
        service = self.create_service(factory)
        service.configure("huifa/yt-release")
        expected = make_update()
        factory.instances[0].check_result = expected
        received: list[ApplicationUpdate] = []
        service.update_available.connect(received.append)

        self.assertTrue(service.check())
        self._wait_idle(service)

        self.assertEqual(received, [expected])
        self.assertIs(service.current_update, expected)

    def test_download_forwards_progress_and_marks_result_installable(self) -> None:
        factory = RecordingFactory()
        service = self.create_service(factory)
        service.configure("huifa/yt-release")
        selected = make_update()
        service.current_update = selected
        progress: list[int] = []
        downloaded: list[ApplicationUpdate] = []
        service.progress.connect(progress.append)
        service.downloaded.connect(downloaded.append)

        self.assertTrue(service.download())
        self._wait_idle(service)

        self.assertEqual(progress, [0, 35, 78, 100])
        self.assertEqual(factory.instances[0].download_calls, [selected])
        self.assertEqual(len(downloaded), 1)
        self.assertTrue(downloaded[0].downloaded)
        self.assertTrue(service.current_update.downloaded)
        self.assertEqual(service.current_update.token, selected.token)

    def test_startup_restore_exposes_pending_update_without_checking_or_downloading(self) -> None:
        factory = RecordingFactory()
        service = self.create_service(factory)
        service.configure("huifa/yt-release")
        updater = factory.instances[0]
        # Defensively verify the service marks an adapter result as downloaded.
        updater.pending_result = make_update(downloaded=False, version="0.3.0")
        restored: list[ApplicationUpdate] = []
        absent: list[bool] = []
        service.pending_restart_available.connect(restored.append)
        service.no_pending_restart.connect(lambda: absent.append(True))

        self.assertTrue(service.restore_pending_restart())
        self._wait_idle(service)

        self.assertEqual(updater.pending_calls, 1)
        self.assertEqual(updater.check_calls, 0)
        self.assertEqual(updater.download_calls, [])
        self.assertEqual(absent, [])
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0].version, "0.3.0")
        self.assertTrue(restored[0].downloaded)
        self.assertIs(service.current_update, restored[0])
        # A restored package is already verified by Velopack and must not be
        # sent through download_updates again.
        self.assertFalse(service.download())
        self.assertEqual(updater.download_calls, [])

    def test_startup_restore_reports_absence_and_clears_stale_state(self) -> None:
        factory = RecordingFactory()
        service = self.create_service(factory)
        service.configure("huifa/yt-release")
        service.current_update = make_update(downloaded=True)
        absent: list[bool] = []
        service.no_pending_restart.connect(lambda: absent.append(True))

        self.assertTrue(service.restore_pending_restart())
        self._wait_idle(service)

        self.assertEqual(factory.instances[0].pending_calls, 1)
        self.assertEqual(absent, [True])
        self.assertIsNone(service.current_update)

    def test_startup_restore_failure_is_signalled_and_thread_is_cleaned(self) -> None:
        factory = RecordingFactory()
        service = self.create_service(factory)
        service.configure("huifa/yt-release")
        factory.instances[0].pending_error = RuntimeError("pending metadata damaged")
        failures: list[str] = []
        service.failed.connect(failures.append)

        self.assertTrue(service.restore_pending_restart())
        self._wait_idle(service)

        self.assertEqual(failures, ["pending metadata damaged"])
        self.assertIsNone(service._thread)
        self.assertIsNone(service._worker)

    def test_check_and_download_failures_are_signalled_and_threads_are_cleaned(self) -> None:
        factory = RecordingFactory()
        service = self.create_service(factory)
        service.configure("huifa/yt-release")
        updater = factory.instances[0]
        errors: list[str] = []
        service.failed.connect(errors.append)

        updater.check_error = RuntimeError("检查通道暂时不可用")
        self.assertTrue(service.check())
        self._wait_idle(service)
        self.assertEqual(errors, ["检查通道暂时不可用"])
        self.assertIsNone(service._thread)
        self.assertIsNone(service._worker)

        updater.check_error = None
        updater.download_error = RuntimeError("下载校验失败")
        selected = make_update()
        self.assertTrue(service.download(selected))
        self._wait_idle(service)
        self.assertEqual(errors, ["检查通道暂时不可用", "下载校验失败"])
        self.assertIsNone(service._thread)
        self.assertIsNone(service._worker)

    def test_schedule_install_requires_download_and_passes_explicit_confirmation(self) -> None:
        factory = RecordingFactory()
        service = self.create_service(factory)
        service.configure("huifa/yt-release")
        updater = factory.instances[0]

        with self.assertRaises(UpdateConfirmationRequired):
            service.schedule_install_and_restart()
        with self.assertRaises(UpdateConfirmationRequired):
            service.schedule_install_and_restart(make_update(downloaded=True))
        with self.assertRaisesRegex(RuntimeError, "尚未下载完成"):
            service.schedule_install_and_restart(make_update(), confirmed=True)

        ready = make_update(downloaded=True)
        service.current_update = ready
        service.schedule_install_and_restart(confirmed=True)
        self.assertEqual(updater.schedule_calls, [(ready, True, True)])
        intent_path = service.state_dir / INSTALL_INTENT_FILENAME
        intent = json.loads(intent_path.read_text(encoding="utf-8"))
        self.assertEqual(intent["from_version"], "0.1.0")
        self.assertEqual(intent["to_version"], "0.2.0")

    def test_failed_updater_launch_removes_install_intent(self) -> None:
        factory = RecordingFactory()
        service = self.create_service(factory)
        service.configure("huifa/yt-release")
        updater = factory.instances[0]
        updater.schedule_error = RuntimeError("helper launch failed")

        with self.assertRaisesRegex(RuntimeError, "helper launch failed"):
            service.schedule_install_and_restart(make_update(downloaded=True), confirmed=True)

        self.assertFalse((service.state_dir / INSTALL_INTENT_FILENAME).exists())

    def test_shutdown_timeout_preserves_live_thread_then_cleans_after_worker_finishes(self) -> None:
        factory = RecordingFactory()
        service = self.create_service(factory)
        service.configure("huifa/yt-release")
        updater = factory.instances[0]
        updater.release_check = threading.Event()
        busy_changes: list[bool] = []
        service.busy_changed.connect(busy_changes.append)

        self.assertTrue(service.check())
        self.assertTrue(updater.check_started.wait(1.0))
        running_thread = service._thread
        running_worker = service._worker
        self.assertFalse(service.shutdown(timeout_ms=5))
        self.assertIs(service._thread, running_thread)
        self.assertIs(service._worker, running_worker)

        updater.release_check.set()
        self._wait_idle(service)
        self.assertEqual(busy_changes, [True, False])

    def test_shutdown_cooperatively_cancels_portable_download_without_failure_signal(self) -> None:
        instances: list[CancellableDownloadUpdater] = []

        def factory(config: VelopackUpdaterConfig) -> CancellableDownloadUpdater:
            updater = CancellableDownloadUpdater(config)
            instances.append(updater)
            return updater

        service = self.create_service(factory)
        service.configure("huifa/yt-release")
        failures: list[str] = []
        service.failed.connect(failures.append)

        self.assertTrue(service.download(make_update()))
        self.assertTrue(instances[0].download_started.wait(1.0))
        service.request_shutdown()
        self._wait_idle(service)

        self.assertTrue(instances[0].cancel_observed.is_set())
        self.assertEqual(failures, [])
        self.assertIsNone(service._thread)
        self.assertIsNone(service._worker)

    def test_stale_thread_cleanup_cannot_clear_a_newer_operation(self) -> None:
        factory = RecordingFactory()
        service = self.create_service(factory)
        service.configure("huifa/yt-release")
        old_thread = QThread()
        current_thread = QThread()
        marker = object()
        service._thread = current_thread
        service._worker = marker

        service._clear_thread_references(old_thread)
        self.assertIs(service._thread, current_thread)
        self.assertIs(service._worker, marker)

        service._clear_thread_references(current_thread)
        self.assertIsNone(service._thread)
        self.assertIsNone(service._worker)

    def test_thread_start_failure_reports_error_without_leaking_busy_state(self) -> None:
        factory = RecordingFactory()
        service = self.create_service(factory)
        service.configure("huifa/yt-release")
        failures: list[str] = []
        busy_changes: list[bool] = []
        service.failed.connect(failures.append)
        service.busy_changed.connect(busy_changes.append)

        with patch(
            "app.core.application_update_service.QThread.start",
            side_effect=RuntimeError("thread resource exhausted"),
        ):
            self.assertFalse(service.check())

        self.assertFalse(service.busy)
        self.assertIsNone(service._thread)
        self.assertIsNone(service._worker)
        self.assertEqual(busy_changes, [])
        self.assertEqual(len(failures), 1)
        self.assertIn("thread resource exhausted", failures[0])

    def test_thread_wiring_failure_reports_error_without_publishing_busy_state(self) -> None:
        factory = RecordingFactory()
        service = self.create_service(factory)
        service.configure("huifa/yt-release")
        failures: list[str] = []
        busy_changes: list[bool] = []
        service.failed.connect(failures.append)
        service.busy_changed.connect(busy_changes.append)

        with patch.object(
            service,
            "_connect_worker_runtime",
            side_effect=RuntimeError("signal wiring failed"),
        ):
            self.assertFalse(service.check())

        self.assertFalse(service.busy)
        self.assertIsNone(service._thread)
        self.assertIsNone(service._worker)
        self.assertEqual(busy_changes, [])
        self.assertEqual(len(failures), 1)
        self.assertIn("signal wiring failed", failures[0])

    def test_thread_cleanup_waits_for_queued_update_result(self) -> None:
        factory = RecordingFactory()
        service = self.create_service(factory)
        service.configure("huifa/yt-release")
        thread = QThread(service)
        marker = object()
        update = make_update(version="0.4.0")
        received: list[ApplicationUpdate] = []
        busy_changes: list[bool] = []
        service._thread = thread
        service._worker = marker
        service.update_available.connect(received.append)
        service.busy_changed.connect(busy_changes.append)

        self.assertTrue(service.busy)
        service._defer_thread_finished(thread)
        service._on_checked(update)
        self.app.processEvents()

        self.assertEqual(received, [update])
        self.assertIs(service.current_update, update)
        self.assertFalse(service.busy)
        self.assertIsNone(service._thread)
        self.assertIsNone(service._worker)
        self.assertEqual(busy_changes, [False])

    def test_source_build_reports_clear_not_managed_error(self) -> None:
        module = UnmanagedVelopackModule()

        def factory(config):
            return VelopackApplicationUpdater(config, velopack_module=module)

        service = self.create_service(factory)
        service.configure("huifa/yt-release")
        errors: list[str] = []
        service.failed.connect(errors.append)

        self.assertTrue(service.check())
        self._wait_idle(service)

        self.assertEqual(len(errors), 1)
        self.assertIn("不是 Velopack 安装版或便携版", errors[0])
        self.assertIn("无法执行本程序更新", errors[0])
        self.assertNotIn("Traceback", errors[0])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import errno
import tempfile
import threading
import time
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Callable
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication

from app.core.disk_capacity import (
    CapacityEstimate,
    DiskCapacityErrorCode,
    DiskReservationManager,
)
from app.core.disk_capacity_lease import DiskReservationLease
from app.core.download_service import (
    DownloadService,
    DownloadTask,
    DownloadWorker,
)
from app.core.log_service import DownloadLogService
from app.core.media_validation import MediaValidationResult
from app.storage.database import Database


MIB = 1024 * 1024


def known_peak(byte_count: int) -> CapacityEstimate:
    return CapacityEstimate(
        known=True,
        download_bytes=byte_count,
        final_bytes=byte_count,
        peak_bytes=byte_count,
        margin_bytes=0,
        entry_count=1,
        merge_entry_count=0,
        sources=("test",),
    )


def validation_result(path: Path) -> MediaValidationResult:
    return MediaValidationResult(
        file_path=str(path),
        size_bytes=path.stat().st_size,
        duration_seconds=1.0,
        container="mov,mp4,m4a,3gp,3g2,mj2",
        container_description="QuickTime / MOV",
        stream_count=2,
        video_stream_count=1,
        audio_stream_count=1,
        subtitle_stream_count=0,
        other_stream_count=0,
    )


@dataclass
class DownloadScenario:
    entries: list[dict]
    playlist: bool = False
    preview: dict | None = None
    raise_after_write: BaseException | None = None
    before_progress: Callable[[], None] | None = None
    after_write: Callable[[], None] | None = None
    start_merger: bool = False
    events: list[str] = field(default_factory=list)
    probe_options: list[dict] = field(default_factory=list)
    complete_filter_entered: threading.Event = field(default_factory=threading.Event)


class HookAwareYoutubeDL:
    scenario: DownloadScenario

    def __init__(self, options: dict):
        self.params = dict(options)
        self._post_hooks = list(options.get("post_hooks") or [])

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def add_post_hook(self, hook) -> None:
        self._post_hooks.append(hook)

    @staticmethod
    def prepare_filename(info: dict) -> str:
        return str(info.get("_filename") or "")

    def extract_info(self, _url: str, *, download: bool):
        scenario = type(self).scenario
        if not download:
            scenario.events.append("probe")
            scenario.probe_options.append(dict(self.params))
            return dict(scenario.preview or scenario.entries[0])

        completed_entries: list[dict] = []
        match_filter = self.params.get("match_filter")
        if not callable(match_filter):
            raise AssertionError("final downloader did not receive the capacity match filter")

        for source in scenario.entries:
            entry = dict(source)
            entry_id = str(entry.get("id") or "entry")
            scenario.events.append(f"incomplete:{entry_id}")
            match_filter(entry, incomplete={"format"})
            scenario.events.append(f"complete:{entry_id}")
            scenario.complete_filter_entered.set()
            match_filter(entry, incomplete=False)

            scenario.events.append(f"write:{entry_id}")
            media_path = Path(entry["_filename"])
            media_path.parent.mkdir(parents=True, exist_ok=True)
            media_path.write_bytes((entry_id + "-media").encode("utf-8"))

            if scenario.before_progress is not None:
                scenario.before_progress()

            for hook in self.params.get("progress_hooks") or []:
                hook({
                    "status": "downloading",
                    "info_dict": entry,
                    "filename": str(media_path),
                    "downloaded_bytes": media_path.stat().st_size,
                    "total_bytes": media_path.stat().st_size,
                })

            if scenario.after_write is not None:
                scenario.after_write()
            if scenario.start_merger:
                for hook in self.params.get("postprocessor_hooks") or []:
                    hook({
                        "status": "started",
                        "postprocessor": "Merger",
                        "info_dict": entry,
                    })

            if scenario.raise_after_write is not None:
                raise scenario.raise_after_write

            for hook in self._post_hooks:
                hook(str(media_path))
            entry["requested_downloads"] = [
                {
                    "filepath": str(media_path),
                    "vcodec": "h264",
                    "acodec": "aac",
                }
            ]
            completed_entries.append(entry)

        if scenario.playlist:
            return {"_type": "playlist", "entries": completed_entries}
        return completed_entries[0]


class RecordingDiskManager(DiskReservationManager):
    def __init__(self, events: list[str], *, free_bytes: int = 4 * 1024 * MIB):
        self.events = events
        self.free_bytes = free_bytes
        super().__init__(
            low_watermark_bytes=10 * MIB,
            disk_usage=lambda _path: SimpleNamespace(
                total=8 * 1024 * MIB,
                used=8 * 1024 * MIB - self.free_bytes,
                free=self.free_bytes,
            ),
            volume_resolver=lambda _path: "same-volume",
            cancel_poll_seconds=0.01,
        )

    def acquire(self, *args, **kwargs):
        self.events.append("acquire")
        return super().acquire(*args, **kwargs)

    def release(self, reservation):
        released = super().release(reservation)
        if released:
            self.events.append("release")
        return released


class DownloadDiskCapacityFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        QCoreApplication.instance() or QCoreApplication([])
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db = Database(self.root / "app.db")

    def tearDown(self) -> None:
        self.db.close()
        self.temp_dir.cleanup()

    @staticmethod
    def fake_ytdlp():
        return SimpleNamespace(
            YoutubeDL=HookAwareYoutubeDL,
            utils=SimpleNamespace(DownloadError=RuntimeError),
        )

    def make_entry(self, name: str) -> dict:
        return {
            "id": name,
            "title": name,
            "webpage_url": f"https://example.test/watch/{name}",
            "_filename": str(self.root / f"{name}.mp4"),
            "requested_formats": [
                {"format_id": "video", "filesize": 1000, "vcodec": "h264", "acodec": "none"},
                {"format_id": "audio", "filesize": 500, "vcodec": "none", "acodec": "aac"},
            ],
            "format_id": "video+audio",
        }

    def make_worker(
        self,
        task_id: str,
        manager: DiskReservationManager,
        *,
        playlist_mode: str = "single",
    ) -> tuple[DownloadWorker, DiskReservationLease]:
        task = DownloadTask(
            task_id,
            f"https://example.test/{task_id}",
            str(self.root),
            playlist_mode=playlist_mode,
            status="downloading",
        )
        self.db.upsert_download_task(task)
        lease = DiskReservationLease(manager)
        worker = DownloadWorker(
            task.id,
            task.url,
            task.output_dir,
            self.db,
            ytdlp_core_mode="builtin",
            playlist_mode=playlist_mode,
            disk_lease=lease,
        )
        worker.logs = DownloadLogService(self.root / "logs")
        return worker, lease

    def run_worker(self, worker: DownloadWorker) -> list[str]:
        failures: list[str] = []
        worker.failed.connect(lambda _task_id, error: failures.append(error))
        with patch("app.core.download_service.yt_dlp", self.fake_ytdlp()), patch(
            "app.core.download_service.deno_runtime_path", return_value=""
        ), patch(
            "app.core.download_service.ffmpeg_runtime_path", return_value="C:/tools/ffmpeg.exe"
        ), patch(
            "app.core.download_service.ffprobe_runtime_path", return_value="C:/tools/ffprobe.exe"
        ), patch(
            "app.core.download_service.validate_media_file",
            side_effect=lambda path, *_args, **_kwargs: validation_result(path),
        ):
            worker.run()
        return failures

    def test_final_filter_reserves_before_first_write_and_probe_never_reserves(self) -> None:
        events: list[str] = []
        entry = self.make_entry("single")
        HookAwareYoutubeDL.scenario = DownloadScenario(
            entries=[entry],
            preview={"id": "single", "title": "single", "_type": "video"},
            events=events,
        )
        manager = RecordingDiskManager(events)
        worker, lease = self.make_worker("auto-task", manager, playlist_mode="auto")

        failures = self.run_worker(worker)

        self.assertEqual(failures, [])
        self.assertEqual(len(HookAwareYoutubeDL.scenario.probe_options), 1)
        probe_options = HookAwareYoutubeDL.scenario.probe_options[0]
        self.assertNotIn("match_filter", probe_options)
        self.assertNotIn("post_hooks", probe_options)
        self.assertLess(events.index("probe"), events.index("complete:single"))
        self.assertLess(events.index("complete:single"), events.index("acquire"))
        self.assertLess(events.index("acquire"), events.index("write:single"))
        self.assertLess(events.index("write:single"), events.index("release"))
        self.assertEqual(events.count("acquire"), 1)
        self.assertEqual(lease.active_count, 0)

    def test_playlist_reserves_and_releases_each_entry_sequentially(self) -> None:
        events: list[str] = []
        HookAwareYoutubeDL.scenario = DownloadScenario(
            entries=[self.make_entry("one"), self.make_entry("two")],
            playlist=True,
            events=events,
        )
        manager = RecordingDiskManager(events, free_bytes=100 * MIB)
        worker, lease = self.make_worker("playlist-task", manager, playlist_mode="playlist")

        failures = self.run_worker(worker)

        self.assertEqual(failures, [])
        lifecycle = [
            event for event in events
            if event.startswith("complete:") or event.startswith("write:") or event in {"acquire", "release"}
        ]
        self.assertEqual(
            lifecycle,
            [
                "complete:one", "acquire", "write:one", "release",
                "complete:two", "acquire", "write:two", "release",
            ],
        )
        self.assertEqual(lease.active_count, 0)
        self.assertEqual(len(self.db.list_media()), 2)

    def test_enospc_after_acquire_is_storage_failure_and_finally_releases(self) -> None:
        events: list[str] = []
        no_space = OSError(errno.ENOSPC, "No space left on device")
        HookAwareYoutubeDL.scenario = DownloadScenario(
            entries=[self.make_entry("full")],
            raise_after_write=no_space,
            events=events,
        )
        manager = RecordingDiskManager(events)
        worker, lease = self.make_worker("full-task", manager)

        failures = self.run_worker(worker)

        self.assertEqual(len(failures), 1)
        self.assertEqual(DownloadLogService.classify_error(failures[0]), "磁盘/存储")
        self.assertEqual(events.count("acquire"), 1)
        self.assertEqual(events.count("release"), 1)
        self.assertEqual(lease.active_count, 0)
        self.assertEqual(self.db.list_media(), [])

    def test_low_watermark_is_rechecked_before_merge_and_releases(self) -> None:
        events: list[str] = []
        manager = RecordingDiskManager(events)
        HookAwareYoutubeDL.scenario = DownloadScenario(
            entries=[self.make_entry("merge")],
            after_write=lambda: setattr(manager, "free_bytes", 10 * MIB),
            start_merger=True,
            events=events,
        )
        worker, lease = self.make_worker("merge-task", manager)

        failures = self.run_worker(worker)

        self.assertEqual(len(failures), 1)
        self.assertEqual(DownloadLogService.classify_error(failures[0]), "磁盘/存储")
        self.assertEqual(lease.active_count, 0)
        self.assertEqual(events.count("release"), 1)
        self.assertEqual(self.db.list_media(), [])

    def test_low_watermark_drop_during_regular_progress_stops_before_completion(self) -> None:
        events: list[str] = []
        manager = RecordingDiskManager(events)
        HookAwareYoutubeDL.scenario = DownloadScenario(
            entries=[self.make_entry("progress-full")],
            events=events,
        )
        worker, lease = self.make_worker("progress-full-task", manager)

        def drop_space_after_watchdog_interval() -> None:
            manager.free_bytes = 10 * MIB
            # The production watchdog probes every two seconds or 64 MiB.
            # Move the test clock beyond that boundary so this exercises the
            # regular progress path rather than the forced startup check.
            worker._last_disk_watchdog_at = 0.0

        HookAwareYoutubeDL.scenario.before_progress = drop_space_after_watchdog_interval

        failures = self.run_worker(worker)

        self.assertEqual(len(failures), 1)
        self.assertEqual(DownloadLogService.classify_error(failures[0]), "磁盘/存储")
        self.assertEqual(lease.active_count, 0)
        self.assertEqual(events.count("release"), 1)
        self.assertEqual(self.db.list_media(), [])

    def test_network_retry_releases_unknown_reservation_before_reentry(self) -> None:
        events: list[str] = []
        manager = RecordingDiskManager(events)
        worker, lease = self.make_worker("retry-capacity", manager)

        class ImmediateCancelEvent:
            def is_set(self) -> bool:
                return False

            def wait(self, _seconds: float) -> bool:
                return False

        worker._cancel = ImmediateCancelEvent()  # type: ignore[assignment]
        info = {
            "id": "unknown",
            "webpage_url": "https://example.test/watch/unknown",
            "_filename": str(self.root / "unknown.mp4"),
        }
        ydl = SimpleNamespace(prepare_filename=lambda _info: info["_filename"])
        calls = 0

        def action():
            nonlocal calls
            calls += 1
            worker._capacity_match_filter(ydl, info, incomplete=False)
            if calls < 3:
                raise RuntimeError("Connection timed out")
            worker._capacity_post_hook(info["_filename"])
            return "ok"

        result = worker._run_with_network_retry(action, "容量重试")

        self.assertEqual(result, "ok")
        self.assertEqual(calls, 3)
        self.assertEqual(events.count("acquire"), 3)
        self.assertEqual(events.count("release"), 3)
        self.assertEqual(lease.active_count, 0)

    def test_network_retry_reuses_owned_reservation_when_cleanup_temporarily_fails(self) -> None:
        events: list[str] = []

        class FailFirstReleaseManager(RecordingDiskManager):
            def __init__(self):
                super().__init__(events)
                self.fail_next_release = True

            def release(self, reservation):
                if self.fail_next_release:
                    self.fail_next_release = False
                    raise RuntimeError("temporary capacity cleanup failure")
                return super().release(reservation)

        manager = FailFirstReleaseManager()
        worker, lease = self.make_worker("retry-retained-capacity", manager)

        class ImmediateCancelEvent:
            def is_set(self) -> bool:
                return False

            def wait(self, _seconds: float) -> bool:
                return False

        worker._cancel = ImmediateCancelEvent()  # type: ignore[assignment]
        info = {
            "id": "unknown-retained",
            "webpage_url": "https://example.test/watch/unknown-retained",
            "_filename": str(self.root / "unknown-retained.mp4"),
        }
        ydl = SimpleNamespace(prepare_filename=lambda _info: info["_filename"])
        calls = 0

        def action():
            nonlocal calls
            calls += 1
            worker._capacity_match_filter(ydl, info, incomplete=False)
            if calls == 1:
                raise RuntimeError("Connection timed out")
            worker._capacity_post_hook(info["_filename"])
            return "ok"

        result = worker._run_with_network_retry(action, "容量重试")

        self.assertEqual(result, "ok")
        self.assertEqual(calls, 2)
        self.assertEqual(events.count("acquire"), 1)
        self.assertEqual(events.count("release"), 1)
        self.assertEqual(lease.active_count, 0)
        self.assertTrue(any(
            event.get("category") == "磁盘/存储"
            and "将复用现有预留" in str(event.get("message") or "")
            for event in worker.logs.read(worker.task_id)
        ))

    def test_capacity_filter_emits_storage_and_path_preview_before_download(self) -> None:
        events: list[str] = []
        manager = RecordingDiskManager(events)
        worker, lease = self.make_worker("preview-capacity", manager)
        info = self.make_entry("preview-capacity")
        ydl = SimpleNamespace(prepare_filename=lambda _info: info["_filename"])
        payloads: list[dict] = []
        worker.progress.connect(lambda _task_id, payload: payloads.append(dict(payload)))

        worker._capacity_match_filter(ydl, info, incomplete=False)

        preview = next(payload["storage_preview"] for payload in payloads if "storage_preview" in payload)
        self.assertTrue(preview["known"])
        self.assertGreater(preview["temporary_bytes"], preview["final_bytes"])
        self.assertEqual(preview["final_dir"], str(self.root))
        self.assertEqual(preview["temporary_dir"], str(self.root))
        self.assertFalse(preview["cross_volume"])
        self.assertEqual(lease.active_count, 1)
        lease.release_all()

    def test_cross_volume_second_reservation_failure_releases_first_immediately(self) -> None:
        events: list[str] = []

        class FailSecondAcquireManager(RecordingDiskManager):
            def __init__(self):
                super().__init__(events)
                self.acquire_calls = 0

            def acquire(self, *args, **kwargs):
                self.acquire_calls += 1
                if self.acquire_calls == 2:
                    raise RuntimeError("final volume unavailable")
                return super().acquire(*args, **kwargs)

        manager = FailSecondAcquireManager()
        worker, lease = self.make_worker("cross-volume-failure", manager)
        info = self.make_entry("cross-volume-failure")
        ydl = SimpleNamespace(prepare_filename=lambda _info: info["_filename"])

        with patch(
            "app.core.download_service.same_storage_volume",
            return_value=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "final volume unavailable"):
                worker._capacity_match_filter(ydl, info, incomplete=False)

        self.assertEqual(manager.acquire_calls, 2)
        self.assertEqual(events.count("release"), 1)
        self.assertEqual(lease.active_count, 0)

    def test_capacity_filter_converts_type_error_to_non_bypassable_failure(self) -> None:
        events: list[str] = []
        manager = RecordingDiskManager(events)
        worker, lease = self.make_worker("invalid-capacity-entry", manager)
        info = self.make_entry("invalid-capacity-entry")
        ydl = SimpleNamespace(
            prepare_filename=lambda _info: (_ for _ in ()).throw(
                TypeError("invalid extractor payload")
            )
        )

        with self.assertRaisesRegex(RuntimeError, "磁盘容量检查失败"):
            worker._capacity_match_filter(ydl, info, incomplete=False)

        self.assertEqual(events.count("acquire"), 0)
        self.assertEqual(lease.active_count, 0)

    def test_all_user_stop_reasons_cancel_disk_wait_without_leak(self) -> None:
        for reason in ("pause", "cancel", "delete", "discard", "shutdown"):
            with self.subTest(reason=reason):
                events: list[str] = []
                name = f"waiting-{reason}"
                HookAwareYoutubeDL.scenario = DownloadScenario(
                    entries=[self.make_entry(name)],
                    events=events,
                )
                manager = RecordingDiskManager(events, free_bytes=200 * MIB)
                blocker = manager.acquire(self.root, known_peak(150 * MIB))
                worker, lease = self.make_worker(f"{name}-task", manager)
                failures: list[str] = []
                worker.failed.connect(lambda _task_id, error: failures.append(error))

                thread = threading.Thread(target=lambda: self.run_worker(worker))
                thread.start()
                self.assertTrue(HookAwareYoutubeDL.scenario.complete_filter_entered.wait(1))
                deadline = time.monotonic() + 1
                while worker._stage_text != "等待其他下载任务释放磁盘空间" and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(worker._stage_text, "等待其他下载任务释放磁盘空间")
                worker.cancel(reason)
                thread.join(1)

                self.assertFalse(thread.is_alive())
                self.assertEqual(failures, [])
                self.assertFalse((self.root / f"{name}.mp4").exists())
                self.assertEqual(lease.active_count, 0)
                self.assertTrue(manager.release(blocker))

    def test_service_thread_finished_fallback_releases_plain_python_lease(self) -> None:
        events: list[str] = []
        manager = RecordingDiskManager(events)
        service = DownloadService(self.db, disk_capacity_manager=manager)
        lease = DiskReservationLease(manager)
        lease.acquire(
            "orphan",
            self.root,
            known_peak(10 * MIB),
            cancel_event=threading.Event(),
        )
        service._disk_leases["orphan-task"] = lease

        service._thread_finished("orphan-task")

        self.assertEqual(lease.active_count, 0)
        replacement = manager.acquire(self.root, known_peak(10 * MIB), timeout_seconds=0)
        self.assertTrue(manager.release(replacement))
        service.shutdown(timeout_ms=0)

    def test_release_all_keeps_only_failed_reservations_for_retry(self) -> None:
        events: list[str] = []

        class FailOnceManager(RecordingDiskManager):
            def __init__(self):
                super().__init__(events)
                self.failed_token = ""

            def release(self, reservation):
                token = str(reservation.token)
                if not self.failed_token:
                    self.failed_token = token
                    raise RuntimeError("transient release failure")
                return super().release(reservation)

        manager = FailOnceManager()
        lease = DiskReservationLease(manager)
        lease.acquire(
            "first",
            self.root,
            known_peak(10 * MIB),
            cancel_event=threading.Event(),
        )
        lease.acquire(
            "second",
            self.root,
            known_peak(10 * MIB),
            cancel_event=threading.Event(),
        )

        with self.assertRaisesRegex(RuntimeError, "transient release failure"):
            lease.release_all()

        self.assertEqual(lease.active_count, 1)
        self.assertEqual(events.count("release"), 1)
        self.assertEqual(lease.release_all(), 1)
        self.assertEqual(lease.active_count, 0)
        self.assertEqual(events.count("release"), 2)

    def test_concurrent_unknown_acquire_uses_one_logical_reservation(self) -> None:
        events: list[str] = []
        first_acquired = threading.Event()
        allow_first_return = threading.Event()

        class ConcurrentAcquireManager(RecordingDiskManager):
            def acquire(self, *args, **kwargs):
                reservation = super().acquire(*args, **kwargs)
                first_acquired.set()
                allow_first_return.wait(timeout=2)
                return reservation

        manager = ConcurrentAcquireManager(events)
        lease = DiskReservationLease(manager)
        results: list[tuple[str, bool]] = []
        errors: list[BaseException] = []

        def acquire_same_key() -> None:
            try:
                reservation, created = lease.acquire(
                    "same-logical-entry",
                    self.root,
                    CapacityEstimate.unknown(),
                    cancel_event=threading.Event(),
                )
                results.append((reservation.token, created))
            except BaseException as exc:
                errors.append(exc)

        first = threading.Thread(target=acquire_same_key)
        second = threading.Thread(target=acquire_same_key)
        first.start()
        self.assertTrue(first_acquired.wait(1))
        second.start()
        time.sleep(0.05)
        self.assertEqual(events.count("acquire"), 1)
        allow_first_return.set()
        first.join(2)
        second.join(2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(len({token for token, _created in results}), 1)
        self.assertEqual(sorted(created for _token, created in results), [False, True])
        self.assertEqual(events.count("acquire"), 1)
        self.assertEqual(lease.active_count, 1)

        self.assertEqual(lease.release_all(), 1)
        self.assertEqual(lease.active_count, 0)
        self.assertEqual(events.count("release"), 1)

    def test_duplicate_waiter_can_cancel_while_first_acquire_is_in_flight(self) -> None:
        events: list[str] = []
        first_acquired = threading.Event()
        allow_first_return = threading.Event()

        class SlowAcquireManager(RecordingDiskManager):
            def acquire(self, *args, **kwargs):
                reservation = super().acquire(*args, **kwargs)
                first_acquired.set()
                allow_first_return.wait(timeout=2)
                return reservation

        manager = SlowAcquireManager(events)
        lease = DiskReservationLease(manager)
        first_result: list[tuple[object, bool]] = []
        second_errors: list[BaseException] = []
        second_cancel = threading.Event()

        first = threading.Thread(
            target=lambda: first_result.append(
                lease.acquire(
                    "same-logical-entry",
                    self.root,
                    CapacityEstimate.unknown(),
                    cancel_event=threading.Event(),
                )
            )
        )

        def acquire_cancelled_duplicate() -> None:
            try:
                lease.acquire(
                    "same-logical-entry",
                    self.root,
                    CapacityEstimate.unknown(),
                    cancel_event=second_cancel,
                )
            except BaseException as exc:
                second_errors.append(exc)

        second = threading.Thread(target=acquire_cancelled_duplicate)
        first.start()
        self.assertTrue(first_acquired.wait(1))
        second.start()
        second_cancel.set()
        second.join(1)
        allow_first_return.set()
        first.join(2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(len(first_result), 1)
        self.assertEqual(len(second_errors), 1)
        self.assertEqual(
            getattr(second_errors[0], "code", None).value,
            DiskCapacityErrorCode.CANCELLED.value,
        )
        self.assertEqual(events.count("acquire"), 1)
        self.assertEqual(lease.release_all(), 1)

    def test_completion_cleanup_failure_preserves_media_and_skips_optional_transcode(self) -> None:
        events: list[str] = []

        class FailFirstReleaseManager(RecordingDiskManager):
            def __init__(self):
                super().__init__(events)
                self.fail_next_release = True

            def release(self, reservation):
                if self.fail_next_release:
                    self.fail_next_release = False
                    raise RuntimeError("completion cleanup failed")
                return super().release(reservation)

        manager = FailFirstReleaseManager()
        worker, lease = self.make_worker("completion-retained-capacity", manager)
        worker.transcode_codec = "h264"
        worker.transcode_device = "gpu"
        worker.transcode_encoder = "h264_nvenc"
        media_path = self.root / "completion-retained-capacity.webm"
        media_path.write_bytes(b"verified-original-media")
        info = self.make_entry("completion-retained-capacity")
        info["_filename"] = str(media_path)
        lease.acquire(
            "completed-entry",
            self.root,
            known_peak(10 * MIB),
            cancel_event=threading.Event(),
        )
        completed = []
        worker.completed.connect(lambda _task_id, item: completed.append(item))

        with patch(
            "app.core.download_service.ffprobe_runtime_path",
            return_value="C:/tools/ffprobe.exe",
        ), patch(
            "app.core.download_service.validate_media_file",
            return_value=validation_result(media_path),
        ), patch(
            "app.core.download_service.prepare_transcode_media",
        ) as transcode:
            worker._complete_download_info(
                info,
                lambda _entry: str(media_path),
            )

        transcode.assert_not_called()
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0].video_path, str(media_path))
        self.assertTrue(media_path.is_file())
        self.assertEqual(lease.active_count, 1)
        row = self.db.list_download_tasks()[0]
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["media_path"], str(media_path))
        self.assertTrue(any(
            "已跳过可选格式转换" in str(event.get("message") or "")
            for event in worker.logs.read(worker.task_id)
        ))

        worker._release_disk_lease_after_run()
        self.assertEqual(lease.active_count, 0)

    def test_service_retains_failed_capacity_release_until_scheduled_retry(self) -> None:
        events: list[str] = []

        class FailOnceManager(RecordingDiskManager):
            def __init__(self):
                super().__init__(events)
                self.fail_next = True

            def release(self, reservation):
                if self.fail_next:
                    self.fail_next = False
                    raise RuntimeError("transient release failure")
                return super().release(reservation)

        manager = FailOnceManager()
        service = DownloadService(self.db, disk_capacity_manager=manager)
        lease = DiskReservationLease(manager)
        lease.acquire(
            "retry",
            self.root,
            known_peak(10 * MIB),
            cancel_event=threading.Event(),
        )
        service._disk_leases["retry-task"] = lease

        with patch("app.core.download_service.QTimer.singleShot") as retry_timer, patch.object(
            service.logs,
            "write",
            side_effect=OSError("log directory unavailable"),
        ):
            service._release_finished_download_capacity("retry-task")

        self.assertNotIn("retry-task", service._disk_leases)
        self.assertEqual(lease.active_count, 1)
        retry_timer.assert_called_once()
        self.assertEqual(retry_timer.call_args.args[0], 1000)

        replacement_lease = DiskReservationLease(manager)
        replacement_lease.acquire(
            "new-run",
            self.root,
            known_peak(10 * MIB),
            cancel_event=threading.Event(),
        )
        service._disk_leases["retry-task"] = replacement_lease
        retry_timer.call_args.args[1]()

        self.assertIs(service._disk_leases["retry-task"], replacement_lease)
        self.assertEqual(lease.active_count, 0)
        self.assertEqual(replacement_lease.active_count, 1)
        service._release_finished_download_capacity("retry-task")
        self.assertEqual(replacement_lease.active_count, 0)
        replacement = manager.acquire(self.root, known_peak(10 * MIB), timeout_seconds=0)
        self.assertTrue(manager.release(replacement))
        service.shutdown(timeout_ms=0)

    def test_transcode_release_failure_stays_owned_until_worker_finally_retry(self) -> None:
        events: list[str] = []

        class FailOnceManager(RecordingDiskManager):
            def __init__(self):
                super().__init__(events)
                self.fail_next = True

            def release(self, reservation):
                if self.fail_next:
                    self.fail_next = False
                    raise RuntimeError("transient transcode release failure")
                return super().release(reservation)

        manager = FailOnceManager()
        worker, lease = self.make_worker("transcode-release-retry", manager)
        media_path = self.root / "transcode-release-retry.mp4"
        media_path.write_bytes(b"media")
        worker._processing_workspace = self.root / "processing" / worker.task_id

        with worker._transcode_workspace(str(media_path)):
            self.assertEqual(lease.active_count, 1)

        self.assertEqual(lease.active_count, 1)
        worker._release_disk_lease_after_run()
        self.assertEqual(lease.active_count, 0)
        replacement = manager.acquire(
            self.root,
            known_peak(10 * MIB),
            timeout_seconds=0,
        )
        self.assertTrue(manager.release(replacement))

    def test_manual_conversion_thread_cleanup_retries_its_capacity_lease(self) -> None:
        events: list[str] = []

        class FailOnceManager(RecordingDiskManager):
            def __init__(self):
                super().__init__(events)
                self.fail_next = True

            def release(self, reservation):
                if self.fail_next:
                    self.fail_next = False
                    raise RuntimeError("transient manual release failure")
                return super().release(reservation)

        manager = FailOnceManager()
        service = DownloadService(self.db, disk_capacity_manager=manager)
        lease = DiskReservationLease(manager)
        lease.acquire(
            "manual-conversion",
            self.root,
            known_peak(10 * MIB),
            cancel_event=threading.Event(),
        )
        service._conversion_disk_leases["manual-task"] = lease

        with patch("app.core.download_service.QTimer.singleShot") as retry_timer, patch.object(
            service.logs,
            "write",
            side_effect=OSError("log unavailable"),
        ):
            service._conversion_thread_finished("manual-task")

        self.assertNotIn("manual-task", service._conversion_disk_leases)
        self.assertEqual(lease.active_count, 1)
        retry_timer.assert_called_once()
        retry_timer.call_args.args[1]()
        self.assertEqual(lease.active_count, 0)
        service.shutdown(timeout_ms=0)

    def test_service_gives_all_workers_the_same_capacity_manager(self) -> None:
        events: list[str] = []
        manager = RecordingDiskManager(events)
        service = DownloadService(self.db, max_concurrent=2, disk_capacity_manager=manager)
        for task_id in ("first", "second"):
            task = DownloadTask(task_id, f"https://example.test/{task_id}", str(self.root))
            self.db.upsert_download_task(task)
            service.tasks[task_id] = task
            service.queue.append(task_id)

        with patch("app.core.download_service.QThread.start", autospec=True):
            service._start_next()

        self.assertEqual(set(service._disk_leases), {"first", "second"})
        self.assertTrue(all(lease.manager is manager for lease in service._disk_leases.values()))
        service._thread_finished("first")
        service._thread_finished("second")
        service.shutdown(timeout_ms=0)


if __name__ == "__main__":
    unittest.main()

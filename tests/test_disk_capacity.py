from __future__ import annotations

import math
import tempfile
import threading
import time
import unittest
from collections import namedtuple
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from app.core.disk_capacity import (
    APPROXIMATE_SIZE_FACTOR,
    BITRATE_SIZE_FACTOR,
    DEFAULT_ESTIMATE_MARGIN_BYTES,
    FRAGMENT_SIZE_FACTOR,
    CapacityEstimate,
    DiskCapacityError,
    DiskCapacityErrorCode,
    DiskReservationManager,
    VolumeIdentity,
    _opaque_windows_fallback,
    estimate_download_capacity,
    resolve_volume_identity,
)


DiskUsage = namedtuple("DiskUsage", "total used free")


def estimate_for_peak(peak_bytes: int) -> CapacityEstimate:
    return CapacityEstimate(
        known=True,
        download_bytes=peak_bytes,
        final_bytes=peak_bytes,
        peak_bytes=peak_bytes,
        margin_bytes=0,
        entry_count=1,
        merge_entry_count=0,
        sources=("test",),
    )


class CapacityEstimatorTests(unittest.TestCase):
    def test_progressive_format_uses_exact_size_plus_fixed_margin(self) -> None:
        info = {
            "duration": 10,
            "filesize": 100 * 1024 * 1024,
            "vcodec": "h264",
            "acodec": "aac",
        }
        result = estimate_download_capacity(info)

        self.assertTrue(result.known)
        self.assertEqual(result.download_bytes, 100 * 1024 * 1024)
        self.assertEqual(result.final_bytes, 100 * 1024 * 1024)
        self.assertEqual(result.peak_bytes, 100 * 1024 * 1024 + DEFAULT_ESTIMATE_MARGIN_BYTES)
        self.assertEqual(result.entry_count, 1)
        self.assertEqual(result.merge_entry_count, 0)
        self.assertEqual(result.sources, ("filesize",))
        with self.assertRaises(FrozenInstanceError):
            result.peak_bytes = 1  # type: ignore[misc]

    def test_separate_audio_video_reserves_inputs_and_merged_output(self) -> None:
        info = {
            "duration": 30,
            "requested_formats": [
                {"filesize": 90_000_000, "vcodec": "h264", "acodec": "none"},
                {"filesize": 10_000_000, "vcodec": "none", "acodec": "aac"},
            ],
        }
        result = estimate_download_capacity(info, margin_bytes=64)

        self.assertEqual(result.download_bytes, 100_000_000)
        self.assertEqual(result.final_bytes, 100_000_000)
        self.assertEqual(result.peak_bytes, 200_000_064)
        self.assertEqual(result.merge_entry_count, 1)

    def test_size_sources_follow_exact_approx_fragment_bitrate_precedence(self) -> None:
        approx = estimate_download_capacity({"filesize_approx": 1_000}, margin_bytes=0)
        self.assertEqual(approx.final_bytes, math.ceil(1_000 * APPROXIMATE_SIZE_FACTOR))

        fragments = estimate_download_capacity(
            {
                "fragments": [
                    {"filesize": 400},
                    {"filesize_approx": 600},
                ],
                "fragment_count": 2,
            },
            margin_bytes=0,
        )
        self.assertEqual(fragments.final_bytes, math.ceil(1_000 * FRAGMENT_SIZE_FACTOR))
        self.assertEqual(fragments.sources, ("fragments",))

        bitrate = estimate_download_capacity(
            {"duration": 10, "vbr": 800, "abr": 200},
            margin_bytes=0,
        )
        expected = math.ceil(1_000 * 1000 / 8 * 10 * BITRATE_SIZE_FACTOR)
        self.assertEqual(bitrate.final_bytes, expected)
        self.assertEqual(bitrate.sources, ("bitrate",))

    def test_playlist_peak_keeps_all_finals_plus_largest_merge_inputs(self) -> None:
        info = {
            "_type": "playlist",
            "entries": [
                {"filesize": 50},
                {
                    "requested_formats": [
                        {"filesize": 70},
                        {"filesize": 30},
                    ]
                },
                {
                    "requested_formats": [
                        {"filesize": 40},
                        {"filesize": 10},
                    ]
                },
            ],
        }
        result = estimate_download_capacity(info, margin_bytes=64)

        self.assertEqual(result.download_bytes, 200)
        self.assertEqual(result.final_bytes, 200)
        self.assertEqual(result.peak_bytes, 200 + 100 + 64)
        self.assertEqual(result.entry_count, 3)
        self.assertEqual(result.merge_entry_count, 2)

    def test_incomplete_or_lazy_info_is_unknown_without_iteration(self) -> None:
        incomplete = estimate_download_capacity(
            {"requested_formats": [{"filesize": 100}, {"format_id": "audio"}]}
        )
        self.assertFalse(incomplete.known)
        self.assertEqual(incomplete.peak_bytes, 0)

        class DeferredEntries:
            def __iter__(self):
                raise AssertionError("capacity estimation must not trigger deferred extraction")

        deferred = estimate_download_capacity({"_type": "playlist", "entries": DeferredEntries()})
        self.assertFalse(deferred.known)

    def test_extreme_remote_size_metadata_degrades_to_unknown_without_overflow(self) -> None:
        cases = (
            {"filesize_approx": "1e308"},
            {"duration": "1e308", "tbr": "1e308"},
            {
                "fragments": [{"filesize": 1}],
                "fragment_count": "1e308",
            },
            {
                "entries": [
                    {"filesize": 5_000_000_000_000_000_000},
                    {"filesize": 5_000_000_000_000_000_000},
                ]
            },
        )

        for info in cases:
            with self.subTest(info=info):
                result = estimate_download_capacity(info, margin_bytes=0)
                self.assertFalse(result.known)
                self.assertEqual(result.peak_bytes, 0)


class VolumeResolutionTests(unittest.TestCase):
    def test_non_windows_identity_uses_device_number_without_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "app.core.disk_capacity._IS_WINDOWS", False
        ):
            identity = resolve_volume_identity(directory)

        self.assertEqual(identity.kind, "device-id")
        self.assertTrue(identity.key.startswith("device:"))
        self.assertNotIn(directory, identity.key)

    def test_windows_fallback_groups_drive_and_unc_share_without_exposing_them(self) -> None:
        drive_a = _opaque_windows_fallback(r"C:\one\downloads")
        drive_b = _opaque_windows_fallback(r"c:\two\videos")
        drive_d = _opaque_windows_fallback(r"D:\downloads")
        unc_a = _opaque_windows_fallback(r"\\server\share\one")
        unc_b = _opaque_windows_fallback(r"\\server\share\two")
        unc_other = _opaque_windows_fallback(r"\\server\other\two")

        self.assertEqual(drive_a, drive_b)
        self.assertNotEqual(drive_a, drive_d)
        self.assertEqual(unc_a, unc_b)
        self.assertNotEqual(unc_a, unc_other)
        self.assertNotIn("server", unc_a.lower())
        self.assertNotIn("share", unc_a.lower())


class DiskReservationManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.a = self.root / "a"
        self.b = self.root / "b"
        self.a.mkdir()
        self.b.mkdir()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def usage(free: int = 1_000) -> DiskUsage:
        return DiskUsage(total=2_000, used=2_000 - free, free=free)

    def test_different_volumes_do_not_share_reservations(self) -> None:
        manager = DiskReservationManager(
            low_watermark_bytes=100,
            disk_usage=lambda _path: self.usage(),
            volume_resolver=lambda path: VolumeIdentity(f"volume:{Path(path).name}", "test"),
        )
        first = manager.acquire(self.a, estimate_for_peak(800))
        second = manager.acquire(self.b, estimate_for_peak(800), timeout_seconds=0)

        self.assertNotEqual(first.volume, second.volume)
        self.assertTrue(manager.release(first))
        self.assertTrue(manager.release(second))

    def test_same_volume_waits_then_wakes_on_release(self) -> None:
        manager = DiskReservationManager(
            low_watermark_bytes=100,
            disk_usage=lambda _path: self.usage(),
            volume_resolver=lambda _path: "same-volume",
            cancel_poll_seconds=0.01,
        )
        first = manager.acquire(self.a, estimate_for_peak(600))
        acquired = threading.Event()
        holder: list = []

        def waiter() -> None:
            holder.append(manager.acquire(self.b, estimate_for_peak(600), timeout_seconds=2))
            acquired.set()

        thread = threading.Thread(target=waiter)
        thread.start()
        self.assertFalse(acquired.wait(0.08))
        self.assertTrue(manager.release(first))
        self.assertTrue(acquired.wait(1))
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertTrue(manager.release(holder[0]))

    def test_wait_callback_reports_contention_once_before_acquire(self) -> None:
        manager = DiskReservationManager(
            low_watermark_bytes=100,
            disk_usage=lambda _path: self.usage(),
            volume_resolver=lambda _path: "same-volume",
            cancel_poll_seconds=0.01,
        )
        first = manager.acquire(self.a, estimate_for_peak(600))
        waiting = threading.Event()
        acquired = threading.Event()
        wait_calls: list[tuple[object, int]] = []
        holder: list = []

        def waiter() -> None:
            holder.append(
                manager.acquire(
                    self.b,
                    estimate_for_peak(600),
                    timeout_seconds=2,
                    on_wait=lambda snapshot, required: (
                        wait_calls.append((snapshot, required)),
                        waiting.set(),
                    ),
                )
            )
            acquired.set()

        thread = threading.Thread(target=waiter)
        thread.start()
        self.assertTrue(waiting.wait(1))
        time.sleep(0.04)
        self.assertEqual(len(wait_calls), 1)
        snapshot, required = wait_calls[0]
        self.assertEqual(snapshot.reserved_bytes, 600)
        self.assertEqual(required, 600)
        self.assertFalse(acquired.is_set())

        self.assertTrue(manager.release(first))
        self.assertTrue(acquired.wait(1))
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertTrue(manager.release(holder[0]))

    def test_concurrent_acquire_never_overcommits_same_volume(self) -> None:
        manager = DiskReservationManager(
            low_watermark_bytes=100,
            disk_usage=lambda _path: self.usage(),
            volume_resolver=lambda _path: "same-volume",
            cancel_poll_seconds=0.005,
        )
        active = 0
        maximum_active = 0
        counter_lock = threading.Lock()
        start = threading.Barrier(9)
        failures: list[Exception] = []

        def contender() -> None:
            nonlocal active, maximum_active
            try:
                start.wait()
                reservation = manager.acquire(self.a, estimate_for_peak(600), timeout_seconds=3)
                with counter_lock:
                    active += 1
                    maximum_active = max(maximum_active, active)
                time.sleep(0.01)
                with counter_lock:
                    active -= 1
                manager.release(reservation)
            except Exception as exc:  # pragma: no cover - assertion reports details
                failures.append(exc)

        threads = [threading.Thread(target=contender) for _ in range(8)]
        for thread in threads:
            thread.start()
        start.wait()
        for thread in threads:
            thread.join(4)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(failures, [])
        self.assertEqual(maximum_active, 1)

    def test_unknown_estimate_is_exclusive_against_known_reservations(self) -> None:
        manager = DiskReservationManager(
            low_watermark_bytes=100,
            disk_usage=lambda _path: self.usage(),
            volume_resolver=lambda _path: "same-volume",
            cancel_poll_seconds=0.01,
        )
        known = manager.acquire(self.a, estimate_for_peak(200))
        unknown_acquired = threading.Event()
        release_unknown = threading.Event()
        unknown_done = threading.Event()

        def unknown_waiter() -> None:
            reservation = manager.acquire(self.a, CapacityEstimate.unknown(), timeout_seconds=2)
            unknown_acquired.set()
            release_unknown.wait(2)
            manager.release(reservation)
            unknown_done.set()

        thread = threading.Thread(target=unknown_waiter)
        thread.start()
        self.assertFalse(unknown_acquired.wait(0.08))
        manager.release(known)
        self.assertTrue(unknown_acquired.wait(1))

        with self.assertRaises(DiskCapacityError) as raised:
            manager.acquire(self.a, estimate_for_peak(100), timeout_seconds=0)
        self.assertEqual(raised.exception.code, DiskCapacityErrorCode.WAIT_TIMEOUT)

        release_unknown.set()
        self.assertTrue(unknown_done.wait(1))
        thread.join(1)
        next_known = manager.acquire(self.a, estimate_for_peak(100), timeout_seconds=0)
        self.assertTrue(manager.release(next_known))

    def test_release_is_idempotent(self) -> None:
        manager = DiskReservationManager(
            low_watermark_bytes=0,
            disk_usage=lambda _path: self.usage(),
            volume_resolver=lambda _path: "same-volume",
        )
        reservation = manager.acquire(self.a, estimate_for_peak(100))
        self.assertTrue(manager.release(reservation))
        self.assertFalse(manager.release(reservation))

    def test_release_repairs_a_missing_volume_token_without_leaking_bytes(self) -> None:
        manager = DiskReservationManager(
            low_watermark_bytes=100,
            disk_usage=lambda _path: self.usage(),
            volume_resolver=lambda _path: "same-volume",
        )
        reservation = manager.acquire(self.a, estimate_for_peak(100))
        manager._states[reservation.volume.key].tokens.remove(reservation.token)

        self.assertTrue(manager.release(reservation))
        replacement = manager.acquire(
            self.a,
            estimate_for_peak(900),
            timeout_seconds=0,
        )
        self.assertTrue(manager.release(replacement))

    def test_blocked_disk_probe_does_not_delay_releasing_another_task(self) -> None:
        block_probe = threading.Event()
        probe_entered = threading.Event()
        allow_probe = threading.Event()

        def usage(_path: str) -> DiskUsage:
            if block_probe.is_set():
                probe_entered.set()
                allow_probe.wait(2)
            return self.usage()

        manager = DiskReservationManager(
            low_watermark_bytes=100,
            disk_usage=usage,
            volume_resolver=lambda _path: "same-volume",
            cancel_poll_seconds=0.01,
        )
        first = manager.acquire(self.a, estimate_for_peak(100))
        acquired: list = []
        waiter = threading.Thread(
            target=lambda: acquired.append(
                manager.acquire(self.a, estimate_for_peak(100), timeout_seconds=2)
            )
        )
        release_result: list[bool] = []
        released = threading.Event()

        block_probe.set()
        waiter.start()
        self.assertTrue(probe_entered.wait(1))

        def release_first() -> None:
            release_result.append(manager.release(first))
            released.set()

        release_thread = threading.Thread(target=release_first)
        release_thread.start()
        try:
            self.assertTrue(
                released.wait(0.5),
                "磁盘容量探测不应占用预留状态锁并阻塞任务释放",
            )
        finally:
            allow_probe.set()
            release_thread.join(1)
            waiter.join(2)

        self.assertFalse(release_thread.is_alive())
        self.assertFalse(waiter.is_alive())
        self.assertEqual(release_result, [True])
        self.assertEqual(len(acquired), 1)
        self.assertTrue(manager.release(acquired[0]))

    def test_blocked_wait_callback_does_not_delay_releasing_another_task(self) -> None:
        manager = DiskReservationManager(
            low_watermark_bytes=100,
            disk_usage=lambda _path: self.usage(),
            volume_resolver=lambda _path: "same-volume",
            cancel_poll_seconds=0.01,
        )
        first = manager.acquire(self.a, estimate_for_peak(600))
        callback_entered = threading.Event()
        allow_callback = threading.Event()
        released = threading.Event()
        acquired: list = []
        failures: list[Exception] = []
        release_result: list[bool] = []

        def on_wait(_snapshot, _required: int) -> None:
            callback_entered.set()
            allow_callback.wait(2)

        def waiter() -> None:
            try:
                acquired.append(
                    manager.acquire(
                        self.b,
                        estimate_for_peak(600),
                        timeout_seconds=2,
                        on_wait=on_wait,
                    )
                )
            except Exception as exc:  # pragma: no cover - assertion reports details
                failures.append(exc)

        def release_first() -> None:
            release_result.append(manager.release(first))
            released.set()

        waiter_thread = threading.Thread(target=waiter)
        release_thread = threading.Thread(target=release_first)
        waiter_thread.start()
        self.assertTrue(callback_entered.wait(1))
        release_thread.start()
        try:
            self.assertTrue(
                released.wait(0.5),
                "等待通知回调不应占用预留状态锁并阻塞任务释放",
            )
        finally:
            allow_callback.set()
            release_thread.join(1)
            waiter_thread.join(2)

        self.assertFalse(release_thread.is_alive())
        self.assertFalse(waiter_thread.is_alive())
        self.assertEqual(release_result, [True])
        self.assertEqual(failures, [])
        self.assertEqual(len(acquired), 1)
        self.assertTrue(manager.release(acquired[0]))

    def test_wait_can_be_cancelled(self) -> None:
        manager = DiskReservationManager(
            low_watermark_bytes=100,
            disk_usage=lambda _path: self.usage(),
            volume_resolver=lambda _path: "same-volume",
            cancel_poll_seconds=0.01,
        )
        first = manager.acquire(self.a, estimate_for_peak(600))
        cancelled = threading.Event()
        result: list[DiskCapacityError] = []

        def waiter() -> None:
            try:
                manager.acquire(self.a, estimate_for_peak(600), cancel_event=cancelled)
            except DiskCapacityError as exc:
                result.append(exc)

        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.05)
        cancelled.set()
        thread.join(1)

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].code, DiskCapacityErrorCode.CANCELLED)
        self.assertTrue(manager.release(first))

    def test_low_watermark_and_insufficient_space_are_distinct(self) -> None:
        low = DiskReservationManager(
            low_watermark_bytes=100,
            disk_usage=lambda _path: self.usage(free=100),
            volume_resolver=lambda _path: "same-volume",
        )
        with self.assertRaises(DiskCapacityError) as low_error:
            low.check_low_watermark(self.a)
        self.assertEqual(low_error.exception.code, DiskCapacityErrorCode.LOW_DISK_SPACE)

        insufficient = DiskReservationManager(
            low_watermark_bytes=100,
            disk_usage=lambda _path: self.usage(free=500),
            volume_resolver=lambda _path: "same-volume",
        )
        with self.assertRaises(DiskCapacityError) as space_error:
            insufficient.acquire(self.a, estimate_for_peak(401))
        self.assertEqual(space_error.exception.code, DiskCapacityErrorCode.INSUFFICIENT_SPACE)
        self.assertIn("降低画质", space_error.exception.action)

    def test_disk_probe_failure_is_structured_and_does_not_leak_path(self) -> None:
        secret_path = str(self.a)

        def failed_probe(_path: str):
            raise OSError(5, f"cannot access {secret_path}")

        manager = DiskReservationManager(
            low_watermark_bytes=100,
            disk_usage=failed_probe,
            volume_resolver=lambda _path: "same-volume",
        )
        with self.assertRaises(DiskCapacityError) as raised:
            manager.acquire(self.a, estimate_for_peak(100))

        error = raised.exception
        self.assertEqual(error.code, DiskCapacityErrorCode.DISK_PROBE_FAILED)
        self.assertNotIn(secret_path, str(error))
        self.assertNotIn(secret_path, error.diagnostic)
        self.assertIsNone(error.__cause__)
        self.assertEqual(error.as_dict()["code"], "disk_probe_failed")


if __name__ == "__main__":
    unittest.main()

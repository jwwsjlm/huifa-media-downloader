from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from pathlib import Path

from app.core.disk_capacity import (
    CapacityEstimate,
    DiskCapacityError,
    DiskCapacityErrorCode,
    DiskCapacitySnapshot,
    DiskReservation,
    DiskReservationManager,
)


_ACQUIRE_POLL_SECONDS = 0.1


class DiskReservationLease:
    """Own one worker's reservations outside its Qt object lifetime.

    A stable logical key is acquired only once. Concurrent callers for the
    same key wait for that single acquisition instead of entering the manager
    twice; this is essential for unknown-size reservations, which are
    exclusive and would otherwise wait forever on their own first lease.
    """

    def __init__(self, manager: DiskReservationManager):
        self.manager = manager
        self._condition = threading.Condition(threading.RLock())
        self._reservations: dict[str, tuple[DiskReservation, str]] = {}
        self._acquiring: set[str] = set()
        self._order: deque[str] = deque()
        self._release_groups: deque[tuple[str, tuple[str, ...]]] = deque()
        self._release_group_ids: set[str] = set()

    def acquire(
        self,
        key: str,
        target_path: str | Path,
        estimate: CapacityEstimate,
        *,
        cancel_event: threading.Event,
        on_wait: Callable[[DiskCapacitySnapshot, int], None] | None = None,
    ) -> tuple[DiskReservation, bool]:
        """Acquire once for a stable yt-dlp entry key."""

        normalized_key = str(key)
        normalized_target = str(Path(target_path))
        with self._condition:
            while True:
                active = self._reservations.get(normalized_key)
                if active is not None:
                    return active[0], False
                if normalized_key not in self._acquiring:
                    self._acquiring.add(normalized_key)
                    break
                if cancel_event.is_set():
                    raise self._cancel_error()
                self._condition.wait(_ACQUIRE_POLL_SECONDS)

        try:
            reservation = self.manager.acquire(
                normalized_target,
                estimate,
                cancel_event=cancel_event,
                on_wait=on_wait,
            )
        except BaseException:
            self._finish_acquire(normalized_key)
            raise

        with self._condition:
            try:
                self._reservations[normalized_key] = (
                    reservation,
                    normalized_target,
                )
                self._order.append(normalized_key)
            finally:
                self._acquiring.discard(normalized_key)
                self._condition.notify_all()
        return reservation, True

    @staticmethod
    def _cancel_error() -> DiskCapacityError:
        return DiskCapacityError(
            DiskCapacityErrorCode.CANCELLED,
            "等待磁盘空间时任务已取消。",
            "需要时可重新开始该任务。",
        )

    def _finish_acquire(self, key: str) -> None:
        with self._condition:
            self._acquiring.discard(key)
            self._condition.notify_all()

    def queue_release_group(self, group_id: str, keys: list[str]) -> None:
        """Release all volume reservations for one yt-dlp entry together."""

        normalized = tuple(dict.fromkeys(str(key) for key in keys if str(key)))
        if not normalized:
            return
        with self._condition:
            if group_id in self._release_group_ids:
                return
            self._release_group_ids.add(group_id)
            self._release_groups.append((group_id, normalized))

    def _release_key_locked(self, key: str) -> int:
        """Release one reservation and forget it only after no exception."""

        active = self._reservations.get(key)
        if active is None:
            self._order = deque(item for item in self._order if item != key)
            return 0
        released = int(self.manager.release(active[0]))
        if self._reservations.get(key) is active:
            self._reservations.pop(key, None)
        self._order = deque(item for item in self._order if item != key)
        return released

    def _prune_release_groups_locked(self) -> None:
        active_keys = set(self._reservations)
        groups: deque[tuple[str, tuple[str, ...]]] = deque()
        group_ids: set[str] = set()
        for group_id, keys in self._release_groups:
            remaining = tuple(key for key in keys if key in active_keys)
            if not remaining:
                continue
            groups.append((group_id, remaining))
            group_ids.add(group_id)
        self._release_groups = groups
        self._release_group_ids = group_ids

    def release_next_group(self) -> int:
        with self._condition:
            if not self._release_groups:
                return int(self.release_next())
            _group_id, keys = self._release_groups[0]
            released = 0
            first_error: Exception | None = None
            for key in keys:
                try:
                    released += self._release_key_locked(key)
                except Exception as exc:
                    if first_error is None:
                        first_error = exc
            self._prune_release_groups_locked()
            if first_error is not None:
                raise first_error
            return released

    def release_next(self) -> bool:
        """Release the oldest active entry after yt-dlp's final post hook."""

        with self._condition:
            while self._order:
                key = self._order[0]
                if key not in self._reservations:
                    self._order.popleft()
                    continue
                return bool(self._release_key_locked(key))
        return False

    def release_keys(self, keys: list[str] | tuple[str, ...]) -> int:
        """Release a bounded subset while retaining any key that raises."""

        with self._condition:
            released = 0
            first_error: Exception | None = None
            for key in dict.fromkeys(str(item) for item in keys if str(item)):
                try:
                    released += self._release_key_locked(key)
                except Exception as exc:
                    if first_error is None:
                        first_error = exc
            self._prune_release_groups_locked()
            if first_error is not None:
                raise first_error
            return released

    def release_all(self) -> int:
        """Release every active entry; repeated calls are harmless."""

        with self._condition:
            keys = list(dict.fromkeys((*self._order, *self._reservations)))
            released = 0
            first_error: Exception | None = None
            for key in keys:
                try:
                    released += self._release_key_locked(key)
                except Exception as exc:
                    if first_error is None:
                        first_error = exc
            self._prune_release_groups_locked()
            if first_error is not None:
                raise first_error
            return released

    def current_target_paths(self, fallback: str | Path) -> list[str]:
        with self._condition:
            paths = [
                active[1]
                for key in self._order
                if (active := self._reservations.get(key)) is not None
            ]
        return list(dict.fromkeys(paths)) or [str(fallback)]

    @property
    def active_count(self) -> int:
        with self._condition:
            return len(self._reservations)

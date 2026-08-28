from __future__ import annotations

import hashlib
import math
import ntpath
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


MIB = 1024 * 1024
DEFAULT_ESTIMATE_MARGIN_BYTES = 64 * MIB
DEFAULT_LOW_WATERMARK_BYTES = 1024 * MIB
DEFAULT_CANCEL_POLL_SECONDS = 0.1
APPROXIMATE_SIZE_FACTOR = 1.25
FRAGMENT_SIZE_FACTOR = 1.10
BITRATE_SIZE_FACTOR = 1.35
MAX_CAPACITY_BYTES = (1 << 63) - 1
_IS_WINDOWS = os.name == "nt"


class DiskCapacityErrorCode(str, Enum):
    INVALID_ARGUMENT = "invalid_argument"
    VOLUME_RESOLUTION_FAILED = "volume_resolution_failed"
    DISK_PROBE_FAILED = "disk_probe_failed"
    LOW_DISK_SPACE = "low_disk_space"
    INSUFFICIENT_SPACE = "insufficient_space"
    WAIT_TIMEOUT = "wait_timeout"
    CANCELLED = "cancelled"


class DiskCapacityError(RuntimeError):
    """Structured, path-safe failure for capacity checks and reservations."""

    def __init__(
        self,
        code: DiskCapacityErrorCode,
        message: str,
        action: str,
        *,
        diagnostic: str = "",
    ) -> None:
        self.code = code
        self.message = str(message).strip()
        self.action = str(action).strip()
        self.diagnostic = str(diagnostic).strip()
        super().__init__(f"{self.message} {self.action}".strip())

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code.value,
            "message": self.message,
            "action": self.action,
            "diagnostic": self.diagnostic,
        }


@dataclass(frozen=True, slots=True)
class CapacityEstimate:
    """Conservative peak storage estimate derived only from existing info."""

    known: bool
    download_bytes: int
    final_bytes: int
    peak_bytes: int
    margin_bytes: int
    entry_count: int
    merge_entry_count: int
    sources: tuple[str, ...]

    def __post_init__(self) -> None:
        numeric = (
            self.download_bytes,
            self.final_bytes,
            self.peak_bytes,
            self.margin_bytes,
            self.entry_count,
            self.merge_entry_count,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            or value > MAX_CAPACITY_BYTES
            for value in numeric
        ):
            raise ValueError("容量估算字段必须是非负整数")
        if self.merge_entry_count > self.entry_count:
            raise ValueError("合并条目数不能超过总条目数")
        if self.known:
            if self.entry_count <= 0 or self.final_bytes <= 0 or self.peak_bytes < self.final_bytes:
                raise ValueError("已知容量估算必须包含有效的条目、成品和峰值大小")
        elif self.download_bytes or self.final_bytes or self.peak_bytes:
            raise ValueError("未知容量估算不能声明确定字节数")

    @classmethod
    def unknown(
        cls,
        *,
        margin_bytes: int = DEFAULT_ESTIMATE_MARGIN_BYTES,
        entry_count: int = 0,
    ) -> CapacityEstimate:
        return cls(
            known=False,
            download_bytes=0,
            final_bytes=0,
            peak_bytes=0,
            margin_bytes=max(0, int(margin_bytes)),
            entry_count=max(0, int(entry_count)),
            merge_entry_count=0,
            sources=("unknown",),
        )


@dataclass(frozen=True, slots=True)
class VolumeIdentity:
    """Opaque volume identity; it deliberately contains no user path."""

    key: str
    kind: str


@dataclass(frozen=True, slots=True)
class DiskCapacitySnapshot:
    volume: VolumeIdentity
    total_bytes: int
    used_bytes: int
    free_bytes: int
    reserved_bytes: int
    available_bytes: int
    low_watermark_bytes: int
    unknown_reservation_active: bool


@dataclass(frozen=True, slots=True)
class DiskReservation:
    token: str
    volume: VolumeIdentity
    reserved_bytes: int
    estimate_known: bool


@dataclass(frozen=True, slots=True)
class _SizeEstimate:
    bytes: int
    source: str


@dataclass(frozen=True, slots=True)
class _EntryEstimate:
    download_bytes: int
    final_bytes: int
    merge_required: bool
    sources: tuple[str, ...]


@dataclass(slots=True)
class _VolumeState:
    known_reserved_bytes: int = 0
    tokens: set[str] = field(default_factory=set)
    unknown_token: str = ""


class _ReservationOutcome(str, Enum):
    ACQUIRE = "acquire"
    WAIT = "wait"
    LOW_SPACE = "low_space"
    INSUFFICIENT_SPACE = "insufficient_space"


@dataclass(frozen=True, slots=True)
class _ReservationDecision:
    outcome: _ReservationOutcome
    required_bytes: int


def _error(
    code: DiskCapacityErrorCode,
    message: str,
    action: str,
    *,
    diagnostic: str = "",
) -> DiskCapacityError:
    return DiskCapacityError(code, message, action, diagnostic=diagnostic)


def _non_negative_integer(value: Any, *, name: str) -> int:
    if isinstance(value, bool):
        raise _error(
            DiskCapacityErrorCode.INVALID_ARGUMENT,
            f"{name}设置无效。",
            "请使用非负整数重新设置。",
        )
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        raise _error(
            DiskCapacityErrorCode.INVALID_ARGUMENT,
            f"{name}设置无效。",
            "请使用非负整数重新设置。",
        ) from None
    if parsed < 0 or parsed > MAX_CAPACITY_BYTES:
        raise _error(
            DiskCapacityErrorCode.INVALID_ARGUMENT,
            f"{name}设置无效。",
            f"请使用 0 到 {MAX_CAPACITY_BYTES} 之间的整数重新设置。",
        )
    return parsed


def _positive_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _scaled_bytes(value: Any, factor: float) -> int | None:
    parsed = _positive_float(value)
    if parsed is None:
        return None
    return _bounded_ceil(parsed * factor)


def _bounded_ceil(value: float) -> int | None:
    if not math.isfinite(value) or value <= 0 or value > MAX_CAPACITY_BYTES:
        return None
    return max(1, math.ceil(value))


def _bounded_sum(values: Sequence[int]) -> int | None:
    total = sum(values)
    return total if 0 < total <= MAX_CAPACITY_BYTES else None


def _fragment_estimate(record: Mapping[str, Any]) -> _SizeEstimate | None:
    fragments = record.get("fragments")
    if not isinstance(fragments, (list, tuple)) or not fragments:
        return None

    sizes: list[int] = []
    approximate = False
    for fragment in fragments:
        if not isinstance(fragment, Mapping):
            return None
        size = _scaled_bytes(fragment.get("filesize"), 1.0)
        if size is None:
            size = _scaled_bytes(fragment.get("filesize_approx"), 1.0)
            approximate = size is not None or approximate
        if size is None:
            return None
        sizes.append(size)

    fragment_count = _positive_float(record.get("fragment_count"))
    represented_count = len(sizes)
    raw_projected_count = fragment_count or represented_count
    if raw_projected_count > MAX_CAPACITY_BYTES:
        return None
    projected_count = max(represented_count, math.ceil(raw_projected_count))
    total = _bounded_sum(sizes)
    if total is None:
        return None
    if projected_count > represented_count:
        projected_total = _bounded_ceil(
            (total / represented_count) * projected_count
        )
        if projected_total is None:
            return None
        total = projected_total
        approximate = True
    factor = FRAGMENT_SIZE_FACTOR if approximate or len(sizes) > 1 else 1.0
    estimated_bytes = _bounded_ceil(total * factor)
    return (
        _SizeEstimate(estimated_bytes, "fragments")
        if estimated_bytes is not None
        else None
    )


def _bitrate_estimate(record: Mapping[str, Any], fallback_duration: Any) -> _SizeEstimate | None:
    duration = _positive_float(record.get("duration")) or _positive_float(fallback_duration)
    if duration is None:
        return None

    bitrate = _positive_float(record.get("tbr"))
    if bitrate is None:
        components = [
            value
            for value in (
                _positive_float(record.get("vbr")),
                _positive_float(record.get("abr")),
            )
            if value is not None
        ]
        bitrate = sum(components) if components else None
    if bitrate is None:
        return None

    # yt-dlp reports tbr/vbr/abr in kilobits per second. Account for container,
    # manifest and bitrate variance rather than treating the average as a cap.
    byte_count = _bounded_ceil(
        bitrate * 1000.0 / 8.0 * duration * BITRATE_SIZE_FACTOR
    )
    return _SizeEstimate(byte_count, "bitrate") if byte_count is not None else None


def _record_size(record: Mapping[str, Any], fallback_duration: Any) -> _SizeEstimate | None:
    exact = _scaled_bytes(record.get("filesize"), 1.0)
    if exact is not None:
        return _SizeEstimate(exact, "filesize")

    approximate = _scaled_bytes(record.get("filesize_approx"), APPROXIMATE_SIZE_FACTOR)
    if approximate is not None:
        return _SizeEstimate(approximate, "filesize_approx")

    fragments = _fragment_estimate(record)
    if fragments is not None:
        return fragments
    return _bitrate_estimate(record, fallback_duration)


def _selected_records(entry: Mapping[str, Any]) -> list[Mapping[str, Any]] | None:
    for key in ("requested_formats", "requested_downloads"):
        requested = entry.get(key)
        if requested is None:
            continue
        if not isinstance(requested, (list, tuple)) or not requested:
            return None
        if any(not isinstance(item, Mapping) for item in requested):
            return None
        return list(requested)
    return [entry]


def _estimate_entry(entry: Mapping[str, Any]) -> _EntryEstimate | None:
    selected = _selected_records(entry)
    if not selected:
        return None

    estimates = [_record_size(record, entry.get("duration")) for record in selected]
    if any(estimate is None for estimate in estimates):
        return None
    known_estimates = [estimate for estimate in estimates if estimate is not None]
    download_bytes = _bounded_sum(
        [estimate.bytes for estimate in known_estimates]
    )
    if download_bytes is None:
        return None

    merge_required = len(selected) > 1
    final_bytes = download_bytes
    # When yt-dlp also provides a final aggregate size, never choose a smaller
    # final-file estimate than that aggregate.
    if merge_required:
        aggregate = _record_size(entry, entry.get("duration"))
        if aggregate is not None:
            final_bytes = max(final_bytes, aggregate.bytes)
    sources = tuple(dict.fromkeys(estimate.source for estimate in known_estimates))
    return _EntryEstimate(download_bytes, final_bytes, merge_required, sources)


def _concrete_entries(info: Mapping[str, Any]) -> list[Mapping[str, Any]] | None:
    if "entries" not in info:
        return [info]
    # Do not iterate yt-dlp LazyList or another deferred sequence here: doing
    # so could unexpectedly perform more extraction/network work.
    entries = info.get("entries")
    if not isinstance(entries, (list, tuple)) or not entries:
        return None
    if any(not isinstance(entry, Mapping) for entry in entries):
        return None
    return list(entries)


def estimate_download_capacity(
    info: Mapping[str, Any],
    *,
    margin_bytes: int = DEFAULT_ESTIMATE_MARGIN_BYTES,
) -> CapacityEstimate:
    """Estimate peak disk use without extracting or requesting more metadata.

    Exact/approximate file sizes, complete fragment sizes, and finally
    bitrate × duration are considered in that order. Separate requested
    formats reserve both their input files and merged output at the merge peak.
    For playlists, already completed outputs are retained while the largest
    remaining merge is in progress.
    """

    margin = _non_negative_integer(margin_bytes, name="容量估算余量")
    if not isinstance(info, Mapping):
        raise _error(
            DiskCapacityErrorCode.INVALID_ARGUMENT,
            "下载信息格式无效，无法估算磁盘容量。",
            "请重新解析链接后重试。",
        )

    entries = _concrete_entries(info)
    if entries is None:
        return CapacityEstimate.unknown(margin_bytes=margin)
    estimates = [_estimate_entry(entry) for entry in entries]
    if any(estimate is None for estimate in estimates):
        return CapacityEstimate.unknown(margin_bytes=margin, entry_count=len(entries))

    known = [estimate for estimate in estimates if estimate is not None]
    download_bytes = _bounded_sum([estimate.download_bytes for estimate in known])
    final_bytes = _bounded_sum([estimate.final_bytes for estimate in known])
    if download_bytes is None or final_bytes is None:
        return CapacityEstimate.unknown(
            margin_bytes=margin,
            entry_count=len(entries),
        )
    # A merged output coexists with its separate input streams. Completed
    # playlist entries also remain on disk, hence total finals + largest merge
    # input is a conservative operation-wide peak.
    merge_input_peak = max(
        (estimate.download_bytes for estimate in known if estimate.merge_required),
        default=0,
    )
    peak_bytes = _bounded_sum([final_bytes, merge_input_peak, margin])
    if peak_bytes is None:
        return CapacityEstimate.unknown(
            margin_bytes=margin,
            entry_count=len(entries),
        )
    sources = tuple(dict.fromkeys(source for estimate in known for source in estimate.sources))
    return CapacityEstimate(
        known=True,
        download_bytes=download_bytes,
        final_bytes=final_bytes,
        peak_bytes=peak_bytes,
        margin_bytes=margin,
        entry_count=len(known),
        merge_entry_count=sum(1 for estimate in known if estimate.merge_required),
        sources=sources,
    )


def _nearest_existing_path(target_path: str | Path) -> Path:
    try:
        candidate = Path(target_path).expanduser().absolute()
    except (TypeError, ValueError, OSError):
        raise _error(
            DiskCapacityErrorCode.INVALID_ARGUMENT,
            "下载保存位置无效。",
            "请在设置中重新选择下载保存目录。",
        ) from None

    while True:
        try:
            if candidate.exists():
                if candidate.is_file():
                    return candidate.parent
                return candidate
        except OSError:
            pass
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    raise _error(
        DiskCapacityErrorCode.VOLUME_RESOLUTION_FAILED,
        "无法识别下载保存位置所在磁盘。",
        "请确认磁盘已连接，并重新选择下载保存目录。",
    )


def _opaque_windows_fallback(path_value: str) -> str:
    normalized = ntpath.normcase(ntpath.normpath(path_value))
    drive, _tail = ntpath.splitdrive(normalized)
    stable_root = drive or ntpath.dirname(normalized) or normalized
    digest = hashlib.sha256(stable_root.encode("utf-8", "surrogatepass")).hexdigest()[:32]
    return f"windows-fallback:{digest}"


def _windows_volume_key(path_value: str) -> tuple[str, str]:
    """Use the volume GUID where available; hash drive/UNC roots on fallback."""

    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        volume_path_buffer = ctypes.create_unicode_buffer(32768)
        get_volume_path = kernel32.GetVolumePathNameW
        get_volume_path.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        get_volume_path.restype = wintypes.BOOL
        if get_volume_path(path_value, volume_path_buffer, len(volume_path_buffer)):
            mount_point = volume_path_buffer.value
            volume_name_buffer = ctypes.create_unicode_buffer(32768)
            get_volume_name = kernel32.GetVolumeNameForVolumeMountPointW
            get_volume_name.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
            get_volume_name.restype = wintypes.BOOL
            if get_volume_name(mount_point, volume_name_buffer, len(volume_name_buffer)):
                volume_name = volume_name_buffer.value.strip().rstrip("\\/").casefold()
                if volume_name:
                    return f"windows-volume:{volume_name}", "windows-volume-guid"
            return _opaque_windows_fallback(mount_point), "windows-mount-fallback"
    except (AttributeError, OSError, ValueError):
        pass
    return _opaque_windows_fallback(path_value), "windows-path-fallback"


def resolve_volume_identity(target_path: str | Path) -> VolumeIdentity:
    """Resolve a stable, path-free volume identity for an output location."""

    probe_path = _nearest_existing_path(target_path)
    if _IS_WINDOWS:
        key, kind = _windows_volume_key(str(probe_path))
        return VolumeIdentity(key=key, kind=kind)
    try:
        device = probe_path.stat().st_dev
    except OSError:
        raise _error(
            DiskCapacityErrorCode.VOLUME_RESOLUTION_FAILED,
            "无法识别下载保存位置所在磁盘。",
            "请确认磁盘已连接，并重新选择下载保存目录。",
        ) from None
    return VolumeIdentity(key=f"device:{int(device)}", kind="device-id")


class DiskReservationManager:
    """Atomically reserve estimated peak bytes per filesystem volume."""

    def __init__(
        self,
        *,
        low_watermark_bytes: int = DEFAULT_LOW_WATERMARK_BYTES,
        disk_usage: Callable[[str], Any] | None = None,
        volume_resolver: Callable[[str | Path], VolumeIdentity | str] | None = None,
        cancel_poll_seconds: float = DEFAULT_CANCEL_POLL_SECONDS,
    ) -> None:
        self.low_watermark_bytes = _non_negative_integer(
            low_watermark_bytes,
            name="磁盘安全余量",
        )
        try:
            poll_seconds = float(cancel_poll_seconds)
        except (TypeError, ValueError, OverflowError):
            poll_seconds = 0.0
        if not math.isfinite(poll_seconds) or poll_seconds <= 0:
            raise _error(
                DiskCapacityErrorCode.INVALID_ARGUMENT,
                "磁盘预留取消检查间隔无效。",
                "请将检查间隔设置为大于 0 的秒数。",
            )
        self._disk_usage = disk_usage or shutil.disk_usage
        self._volume_resolver = volume_resolver or resolve_volume_identity
        self._cancel_poll_seconds = poll_seconds
        self._condition = threading.Condition(threading.RLock())
        self._states: dict[str, _VolumeState] = {}
        self._reservations: dict[str, DiskReservation] = {}

    @staticmethod
    def _cancelled(cancel_event: threading.Event | None) -> bool:
        return cancel_event is not None and cancel_event.is_set()

    @staticmethod
    def _cancel_error() -> DiskCapacityError:
        return _error(
            DiskCapacityErrorCode.CANCELLED,
            "等待磁盘空间时任务已取消。",
            "需要时可重新开始该任务。",
        )

    @staticmethod
    def _deadline(timeout_seconds: float | None) -> float | None:
        if timeout_seconds is None:
            return None
        try:
            timeout = float(timeout_seconds)
        except (TypeError, ValueError, OverflowError):
            timeout = -1.0
        if not math.isfinite(timeout) or timeout < 0:
            raise _error(
                DiskCapacityErrorCode.INVALID_ARGUMENT,
                "磁盘预留等待时间无效。",
                "请将等待时间设置为非负秒数。",
            )
        return time.monotonic() + timeout

    def _volume(self, target_path: str | Path) -> tuple[VolumeIdentity, Path]:
        probe_path = _nearest_existing_path(target_path)
        try:
            resolved = self._volume_resolver(probe_path)
        except DiskCapacityError:
            raise
        except Exception:
            raise _error(
                DiskCapacityErrorCode.VOLUME_RESOLUTION_FAILED,
                "无法识别下载保存位置所在磁盘。",
                "请确认磁盘已连接，并重新选择下载保存目录。",
            ) from None
        if isinstance(resolved, VolumeIdentity):
            volume = resolved
        elif isinstance(resolved, str) and resolved.strip():
            volume = VolumeIdentity(resolved.strip(), "custom")
        else:
            raise _error(
                DiskCapacityErrorCode.VOLUME_RESOLUTION_FAILED,
                "无法识别下载保存位置所在磁盘。",
                "请确认磁盘已连接，并重新选择下载保存目录。",
            )
        if not volume.key:
            raise _error(
                DiskCapacityErrorCode.VOLUME_RESOLUTION_FAILED,
                "无法识别下载保存位置所在磁盘。",
                "请确认磁盘已连接，并重新选择下载保存目录。",
            )
        return volume, probe_path

    def _usage(self, probe_path: Path) -> tuple[int, int, int]:
        try:
            usage = self._disk_usage(str(probe_path))
            if all(hasattr(usage, name) for name in ("total", "used", "free")):
                total, used, free = usage.total, usage.used, usage.free
            else:
                total, used, free = usage[0], usage[1], usage[2]
            total, used, free = int(total), int(used), int(free)
            if total <= 0 or min(used, free) < 0:
                raise ValueError("invalid disk usage")
        except Exception as exc:
            error_number = getattr(exc, "errno", None)
            diagnostic = f"系统错误码：{error_number}" if error_number else "磁盘容量探测未返回有效数据"
            raise _error(
                DiskCapacityErrorCode.DISK_PROBE_FAILED,
                "无法读取下载磁盘的剩余空间。",
                "请确认磁盘已连接且可访问，然后重试。",
                diagnostic=diagnostic,
            ) from None
        return total, used, free

    def _snapshot(
        self,
        volume: VolumeIdentity,
        usage: tuple[int, int, int],
        state: _VolumeState | None,
    ) -> DiskCapacitySnapshot:
        total, used, free = usage
        reserved = state.known_reserved_bytes if state is not None else 0
        unknown_active = bool(state and state.unknown_token)
        available = max(0, free - reserved - self.low_watermark_bytes)
        return DiskCapacitySnapshot(
            volume=volume,
            total_bytes=total,
            used_bytes=used,
            free_bytes=free,
            reserved_bytes=reserved,
            available_bytes=available,
            low_watermark_bytes=self.low_watermark_bytes,
            unknown_reservation_active=unknown_active,
        )

    def _volume_state_locked(self, volume_key: str) -> _VolumeState:
        """Return a cache rebuilt from the authoritative reservation map."""

        active = [
            reservation
            for reservation in self._reservations.values()
            if reservation.volume.key == volume_key
        ]
        tokens = {reservation.token for reservation in active}
        known_reserved_bytes = sum(
            reservation.reserved_bytes
            for reservation in active
            if reservation.estimate_known
        )
        unknown_tokens = [
            reservation.token
            for reservation in active
            if not reservation.estimate_known
        ]
        if not active:
            self._states.pop(volume_key, None)
            return _VolumeState()
        state = self._states.get(volume_key)
        if state is None:
            state = _VolumeState()
            self._states[volume_key] = state
        expected_unknown_token = unknown_tokens[0] if unknown_tokens else ""
        if (
            state.tokens != tokens
            or state.known_reserved_bytes != known_reserved_bytes
            or state.unknown_token != expected_unknown_token
        ):
            state.tokens = tokens
            state.known_reserved_bytes = known_reserved_bytes
            state.unknown_token = expected_unknown_token
        return state

    def _snapshot_locked(
        self,
        volume: VolumeIdentity,
        usage: tuple[int, int, int],
    ) -> DiskCapacitySnapshot:
        state = self._volume_state_locked(volume.key)
        return self._snapshot(volume, usage, state)

    @staticmethod
    def _decision(
        snapshot: DiskCapacitySnapshot,
        estimate: CapacityEstimate,
    ) -> _ReservationDecision:
        required = estimate.peak_bytes if estimate.known else 0
        if snapshot.free_bytes <= snapshot.low_watermark_bytes:
            return _ReservationDecision(_ReservationOutcome.LOW_SPACE, required)
        if estimate.known and snapshot.free_bytes - required < snapshot.low_watermark_bytes:
            return _ReservationDecision(_ReservationOutcome.INSUFFICIENT_SPACE, required)
        if estimate.known:
            can_acquire = (
                not snapshot.unknown_reservation_active
                and snapshot.available_bytes >= required
            )
        else:
            can_acquire = (
                snapshot.reserved_bytes == 0
                and not snapshot.unknown_reservation_active
            )
        return _ReservationDecision(
            _ReservationOutcome.ACQUIRE if can_acquire else _ReservationOutcome.WAIT,
            required,
        )

    def _commit_locked(
        self,
        volume: VolumeIdentity,
        decision: _ReservationDecision,
        estimate: CapacityEstimate,
    ) -> DiskReservation:
        token = uuid.uuid4().hex
        reservation = DiskReservation(
            token=token,
            volume=volume,
            reserved_bytes=decision.required_bytes,
            estimate_known=estimate.known,
        )
        self._reservations[token] = reservation
        self._volume_state_locked(volume.key)
        return reservation

    @staticmethod
    def _raise_for_decision(
        snapshot: DiskCapacitySnapshot,
        decision: _ReservationDecision,
    ) -> None:
        if decision.outcome is _ReservationOutcome.LOW_SPACE:
            raise _error(
                DiskCapacityErrorCode.LOW_DISK_SPACE,
                "下载磁盘剩余空间已低于安全阈值。",
                "请清理磁盘空间或更换下载保存目录后重试。",
                diagnostic=(
                    f"剩余 {snapshot.free_bytes} 字节；"
                    f"安全阈值 {snapshot.low_watermark_bytes} 字节"
                ),
            )
        if decision.outcome is _ReservationOutcome.INSUFFICIENT_SPACE:
            raise _error(
                DiskCapacityErrorCode.INSUFFICIENT_SPACE,
                "下载磁盘空间不足，无法安全开始该任务。",
                "请清理磁盘空间、降低画质或更换下载保存目录。",
                diagnostic=(
                    f"需要 {decision.required_bytes} 字节；"
                    f"剩余 {snapshot.free_bytes} 字节；"
                    f"安全阈值 {snapshot.low_watermark_bytes} 字节"
                ),
            )

    def _wait_seconds(self, deadline: float | None) -> float:
        if deadline is None:
            return self._cancel_poll_seconds
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _error(
                DiskCapacityErrorCode.WAIT_TIMEOUT,
                "等待其他下载任务释放磁盘空间超时。",
                "请稍后重试，或降低并行下载数。",
            )
        return min(self._cancel_poll_seconds, remaining)

    def check_low_watermark(self, target_path: str | Path) -> DiskCapacitySnapshot:
        """Probe physical free space and fail before the volume reaches danger."""

        volume, probe_path = self._volume(target_path)
        usage = self._usage(probe_path)
        with self._condition:
            snapshot = self._snapshot_locked(volume, usage)
        if snapshot.free_bytes <= self.low_watermark_bytes:
            raise _error(
                DiskCapacityErrorCode.LOW_DISK_SPACE,
                "下载磁盘剩余空间已低于安全阈值。",
                "请清理磁盘空间或更换下载保存目录后重试。",
                diagnostic=(
                    f"剩余 {snapshot.free_bytes} 字节；"
                    f"安全阈值 {self.low_watermark_bytes} 字节"
                ),
            )
        return snapshot

    def acquire(
        self,
        target_path: str | Path,
        estimate: CapacityEstimate,
        *,
        cancel_event: threading.Event | None = None,
        timeout_seconds: float | None = None,
        on_wait: Callable[[DiskCapacitySnapshot, int], None] | None = None,
    ) -> DiskReservation:
        """Wait for and atomically acquire one same-volume reservation.

        Unknown estimates are exclusive because their peak cannot be safely
        added to another task. Known estimates may run concurrently while the
        sum of reservations leaves the configured low-watermark intact.
        """

        if not isinstance(estimate, CapacityEstimate):
            raise _error(
                DiskCapacityErrorCode.INVALID_ARGUMENT,
                "下载容量估算无效。",
                "请重新解析链接后重试。",
            )
        deadline = self._deadline(timeout_seconds)

        if self._cancelled(cancel_event):
            raise self._cancel_error()
        volume, probe_path = self._volume(target_path)

        wait_notified = False
        while True:
            if self._cancelled(cancel_event):
                raise self._cancel_error()

            # Disk probing may block on a sleeping, disconnected, or busy
            # volume. Keep that system I/O outside the shared reservation
            # lock so unrelated completions can still release their leases.
            usage = self._usage(probe_path)
            wait_callback_payload: tuple[DiskCapacitySnapshot, int] | None = None
            with self._condition:
                if self._cancelled(cancel_event):
                    raise self._cancel_error()

                snapshot = self._snapshot_locked(volume, usage)
                decision = self._decision(snapshot, estimate)
                self._raise_for_decision(snapshot, decision)
                if decision.outcome is _ReservationOutcome.ACQUIRE:
                    return self._commit_locked(volume, decision, estimate)

                if not wait_notified and on_wait is not None:
                    wait_notified = True
                    wait_callback_payload = (snapshot, decision.required_bytes)
                else:
                    self._condition.wait(self._wait_seconds(deadline))

            if wait_callback_payload is not None:
                # Call arbitrary observers outside the reservation lock. A UI
                # bridge, logger, or future integration may block; release()
                # must remain available so the contention can actually clear.
                on_wait(*wait_callback_payload)

    def release(self, reservation: DiskReservation) -> bool:
        """Release a reservation once; repeated or foreign releases are safe."""

        if not isinstance(reservation, DiskReservation):
            return False
        with self._condition:
            active = self._reservations.get(reservation.token)
            if active is None or active != reservation:
                return False
            # The reservation map is authoritative. Rebuilding before and
            # after removal repairs any stale derived volume cache instead of
            # losing the only token that can release the reserved bytes.
            self._volume_state_locked(active.volume.key)
            self._reservations.pop(active.token)
            self._volume_state_locked(active.volume.key)
            self._condition.notify_all()
            return True

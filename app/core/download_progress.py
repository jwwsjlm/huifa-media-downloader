from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any, Mapping


def optional_non_negative_int(value: Any) -> int | None:
    """Return a finite non-negative integer, or ``None`` when invalid."""

    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number >= 0 else None


def non_negative_int(value: Any, default: Any = 0) -> int:
    for candidate in (value, default, 0):
        normalized = optional_non_negative_int(candidate)
        if normalized is not None:
            return normalized
    return 0


def optional_non_negative_float(value: Any) -> float | None:
    """Return a finite non-negative float, or ``None`` when invalid."""

    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if isfinite(number) and number >= 0.0 else None


def non_negative_float(value: Any, default: Any = 0.0) -> float:
    for candidate in (value, default, 0.0):
        normalized = optional_non_negative_float(candidate)
        if normalized is not None:
            return normalized
    return 0.0


def bounded_percent(value: Any, default: Any = 0.0) -> float:
    return min(100.0, non_negative_float(value, default))


def format_speed(bytes_per_second: Any) -> str:
    speed = non_negative_float(bytes_per_second)
    if speed <= 0.0:
        return ""
    if speed >= 1024 ** 2:
        return f"{speed / 1024 ** 2:.2f} MiB/s"
    return f"{speed / 1024:.0f} KiB/s"


def format_eta(seconds: Any) -> str:
    remaining = non_negative_float(seconds)
    if remaining <= 0.0:
        return ""
    whole_seconds = int(remaining)
    hours, remainder = divmod(whole_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return (
        f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        if hours else f"{minutes:02d}:{seconds:02d}"
    )


@dataclass(frozen=True, slots=True)
class StageProgressState:
    stage: str = "queued"
    stage_text: str = ""
    stage_progress: float = 0.0
    retry_count: int = 0
    retry_total: int = 0
    reconnect_message: str = ""
    elapsed_seconds: float = 0.0
    stage_elapsed_seconds: float = 0.0
    transcode_encoder: str = ""


@dataclass(frozen=True, slots=True)
class StageProgressMerge:
    state: StageProgressState
    stage_changed: bool = False
    reset_transfer_rate: bool = False


def merge_stage_progress(
    current: StageProgressState,
    payload: Mapping[str, Any],
) -> StageProgressMerge:
    """Merge a worker stage payload without letting malformed values leak in.

    Stage progress belongs only to its stage. A new stage therefore starts at
    zero when the producer has no valid percentage. Invalid values on the same
    stage preserve the last trustworthy value instead of making the UI jump.
    """

    raw_stage = payload.get("stage")
    if not raw_stage:
        return StageProgressMerge(current)

    stage = str(raw_stage)
    stage_changed = stage != current.stage
    raw_text = payload.get("stage_text")
    stage_text = (
        str(raw_text)
        if raw_text not in (None, "")
        else stage if stage_changed else str(current.stage_text or stage)
    )

    progress = optional_non_negative_float(payload.get("stage_progress"))
    if progress is None:
        progress = 0.0 if stage_changed else bounded_percent(current.stage_progress)
    else:
        progress = min(100.0, progress)

    retry_count = optional_non_negative_int(payload.get("retry_count"))
    if retry_count is None:
        retry_count = non_negative_int(current.retry_count)
    retry_total = optional_non_negative_int(payload.get("retry_total"))
    if retry_total is None:
        retry_total = non_negative_int(current.retry_total)

    elapsed = optional_non_negative_float(payload.get("elapsed_seconds"))
    if elapsed is None:
        elapsed = non_negative_float(current.elapsed_seconds)
    stage_elapsed = optional_non_negative_float(payload.get("stage_elapsed_seconds"))
    if stage_elapsed is None:
        stage_elapsed = 0.0 if stage_changed else non_negative_float(
            current.stage_elapsed_seconds
        )

    if stage == "reconnecting":
        reconnect_delay = optional_non_negative_int(payload.get("reconnect_delay"))
        reconnect_message = (
            f"{reconnect_delay} 秒后重试" if reconnect_delay else "正在重试"
        )
    else:
        reconnect_message = ""

    if stage != "transcoding":
        transcode_encoder = ""
    elif "transcode_encoder" in payload:
        transcode_encoder = str(payload.get("transcode_encoder") or "")
    elif stage_changed:
        transcode_encoder = ""
    else:
        transcode_encoder = str(current.transcode_encoder or "")

    return StageProgressMerge(
        state=StageProgressState(
            stage=stage,
            stage_text=stage_text,
            stage_progress=progress,
            retry_count=retry_count,
            retry_total=retry_total,
            reconnect_message=reconnect_message,
            elapsed_seconds=elapsed,
            stage_elapsed_seconds=stage_elapsed,
            transcode_encoder=transcode_encoder,
        ),
        stage_changed=stage_changed,
        # A stage transition either leaves network transfer or starts a new
        # stream. In both cases the old rolling speed and ETA are no longer a
        # valid estimate for the current activity.
        reset_transfer_rate=stage_changed,
    )


@dataclass(frozen=True, slots=True)
class TransferCounterState:
    progress: float = 0.0
    downloaded_bytes: int = 0
    total_bytes: int = 0
    visible_progress: float = 0.0
    visible_downloaded_bytes: int = 0
    visible_total_bytes: int = 0


def _payload_percent(payload: Mapping[str, Any]) -> float | None:
    value = payload.get("_percent")
    if value is None:
        value = payload.get("percent")
    if value is None:
        value = payload.get("_percent_str")
    if value is None:
        return None
    return optional_non_negative_float(str(value).replace("%", "").strip())


def merge_transfer_counters(
    current: TransferCounterState,
    payload: Mapping[str, Any],
) -> TransferCounterState:
    """Merge monotonic transfer counters from a yt-dlp progress callback."""

    progress = bounded_percent(current.progress)
    downloaded = non_negative_int(current.downloaded_bytes)
    total = non_negative_int(current.total_bytes)

    raw_total = payload.get("total_bytes")
    if raw_total is None:
        raw_total = payload.get("total_bytes_estimate")
    incoming_total = optional_non_negative_int(raw_total)
    incoming_done = optional_non_negative_int(payload.get("downloaded_bytes"))
    if incoming_total:
        total = max(total, incoming_total)
    if incoming_done:
        downloaded = max(downloaded, incoming_done)
    if total > 0 and incoming_done:
        progress = max(progress, min(100.0, downloaded * 100.0 / total))

    payload_percent = _payload_percent(payload)
    if payload_percent is not None:
        progress = max(progress, min(100.0, payload_percent))

    return TransferCounterState(
        progress=progress,
        downloaded_bytes=downloaded,
        total_bytes=total,
        visible_progress=max(
            bounded_percent(current.visible_progress),
            progress,
        ),
        visible_downloaded_bytes=max(
            non_negative_int(current.visible_downloaded_bytes),
            downloaded,
        ),
        visible_total_bytes=max(
            non_negative_int(current.visible_total_bytes),
            total,
        ),
    )


def merge_stream_progress(current: Any, incoming: Any) -> float:
    normalized = optional_non_negative_float(incoming)
    if normalized is None:
        return bounded_percent(current)
    return min(100.0, normalized)


__all__ = [
    "StageProgressMerge",
    "StageProgressState",
    "TransferCounterState",
    "bounded_percent",
    "format_eta",
    "format_speed",
    "merge_stage_progress",
    "merge_stream_progress",
    "merge_transfer_counters",
    "non_negative_float",
    "non_negative_int",
    "optional_non_negative_float",
    "optional_non_negative_int",
]

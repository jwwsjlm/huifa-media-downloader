from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class FinishedDownloadState:
    status: str
    error: str
    stage: str
    stage_text: str
    progress: float | None = None
    stage_progress: float = 0.0
    reconnect_message: str = ""


def download_runtime_signal_is_current(
    sender: Any,
    current_worker: Any,
    *,
    allow_finished_runtime: bool = False,
) -> bool:
    """Reject queued signals from an old worker after a replacement starts."""

    if sender is None:
        # Direct service calls and unit tests have no Qt sender and remain
        # valid. Real queued worker signals always carry a QObject sender.
        return True
    if current_worker is None:
        return bool(allow_finished_runtime)
    return sender is current_worker


def finished_download_state(
    *,
    status: str,
    error: str,
    pause_requested: bool,
    cancel_requested: bool,
    completion_warning: str = "",
) -> FinishedDownloadState:
    """Derive one terminal presentation without touching service state."""

    normalized_status = str(status or "")
    normalized_error = str(error or "")
    if normalized_status == "completed":
        return FinishedDownloadState(
            status="completed",
            error="",
            stage="completed",
            stage_text=(
                f"下载完成；{completion_warning}"
                if completion_warning else "下载完成"
            ),
            progress=100.0,
            stage_progress=100.0,
        )
    if pause_requested or normalized_status == "paused":
        return FinishedDownloadState(
            status="paused",
            error="",
            stage="paused",
            stage_text="已暂停",
        )
    if cancel_requested or normalized_status == "canceled":
        return FinishedDownloadState(
            status="canceled",
            error="",
            stage="canceled",
            stage_text="已取消",
        )
    if normalized_error or normalized_status == "failed":
        failure = normalized_error or "下载线程异常结束，未提供失败原因"
        return FinishedDownloadState(
            status="failed",
            error=failure,
            stage="failed",
            stage_text=f"下载失败：{failure[:120]}",
        )
    failure = "下载线程异常结束，未收到完成或失败结果"
    return FinishedDownloadState(
        status="failed",
        error=failure,
        stage="failed",
        stage_text=f"下载失败：{failure}",
    )

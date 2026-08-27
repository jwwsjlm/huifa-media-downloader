from __future__ import annotations

import math
from typing import Any

from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtWidgets import QLabel, QSizePolicy, QStatusBar, QWidget

from app.ui.i18n import format_text as ui_format
from app.ui.i18n import text as ui_text


def format_transfer_speed(speed_bps: object) -> str:
    try:
        value = float(speed_bps or 0.0)
    except (TypeError, ValueError, OverflowError):
        value = 0.0
    if not math.isfinite(value) or value < 0:
        value = 0.0
    units = ("B/s", "KiB/s", "MiB/s", "GiB/s")
    index = 0
    while value >= 1024.0 and index < len(units) - 1:
        value /= 1024.0
        index += 1
    return (
        f"{value:.0f} {units[index]}"
        if index == 0
        else f"{value:.2f} {units[index]}"
    )


def _nonnegative_count(stats: Any, key: str) -> int:
    try:
        return max(0, int(stats.get(key, 0) or 0))
    except (AttributeError, TypeError, ValueError, OverflowError):
        return 0


class TaskStatusSummaryController(QObject):
    """Own the aggregate task label and coalesced refresh lifecycle."""

    REFRESH_INTERVAL_MS = 500

    def __init__(
        self,
        parent: QWidget,
        status_bar: QStatusBar,
        download_service: Any,
    ) -> None:
        super().__init__(parent)
        self._download_service = download_service
        self.label = QLabel(ui_text(
            "Tasks 0 · Active 0 · Queued 0 · Paused 0 · Completed 0 · Needs attention 0 · Total speed: 0 B/s",
        ))
        self.label.setObjectName("taskSummaryStatus")
        self.label.setMinimumWidth(0)
        self.label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        status_bar.addPermanentWidget(self.label)

        self._last_text = self.label.text()
        self._stopped = False
        self._periodic_timer = QTimer(self)
        self._periodic_timer.setInterval(self.REFRESH_INTERVAL_MS)
        self._periodic_timer.timeout.connect(self.refresh)
        self._scheduled_refresh = QTimer(self)
        self._scheduled_refresh.setSingleShot(True)
        self._scheduled_refresh.timeout.connect(self.refresh)

    @property
    def running(self) -> bool:
        return self._periodic_timer.isActive() and not self._stopped

    def start(self) -> None:
        self._stopped = False
        self.refresh()
        self._periodic_timer.start()

    def stop(self) -> None:
        self._stopped = True
        self._periodic_timer.stop()
        self._scheduled_refresh.stop()

    def schedule_refresh(self, *_args: object) -> None:
        if self._stopped or self._scheduled_refresh.isActive():
            return
        self._scheduled_refresh.start(0)

    def refresh(self) -> None:
        if self._stopped:
            return
        try:
            stats = self._download_service.task_statistics()
            speed = self._download_service.total_speed_bps()
        except Exception:
            # Keep the last known-good summary. A transient service/database
            # failure must not terminate a Qt timer callback permanently.
            return

        active = _nonnegative_count(stats, "active") + _nonnegative_count(
            stats,
            "processing",
        )
        text = ui_format(
            "Tasks {tasks} · Active {active} · Queued {queued} · Paused {paused} · Completed {completed} · Attention {failed} · Total speed: {speed}",
            tasks=_nonnegative_count(stats, "total"),
            active=active,
            queued=_nonnegative_count(stats, "queued"),
            paused=_nonnegative_count(stats, "paused"),
            completed=_nonnegative_count(stats, "completed"),
            failed=_nonnegative_count(stats, "failed"),
            speed=format_transfer_speed(speed),
        )
        if text == self._last_text:
            return
        try:
            self.label.setText(text)
        except RuntimeError:
            self.stop()
            return
        self._last_text = text


__all__ = [
    "TaskStatusSummaryController",
    "format_transfer_speed",
]

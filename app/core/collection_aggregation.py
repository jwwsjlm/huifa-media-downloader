from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Mapping

from app.core.download_progress import (
    bounded_percent,
    format_eta,
    non_negative_float,
    non_negative_int,
)


ACTIVE_CHILD_STATUSES = frozenset({
    "downloading",
    "processing",
    "waiting_selection",
    "parsing_collection",
    "canceling",
})


@dataclass(frozen=True, slots=True)
class CollectionChildContribution:
    parent_id: str
    status: str
    speed_bps: float
    known_size: int
    known_done: float
    unknown_fraction: float
    downloaded_bytes: int
    total_bytes: int


@dataclass(slots=True)
class CollectionAggregate:
    statuses: Counter[str] = field(default_factory=Counter)
    child_count: int = 0
    speed_bps: float = 0.0
    known_count: int = 0
    known_total: int = 0
    known_done: float = 0.0
    unknown_count: int = 0
    unknown_progress_sum: float = 0.0
    downloaded_bytes: int = 0
    total_bytes: int = 0

    def apply(self, contribution: CollectionChildContribution, direction: int) -> None:
        """Apply or remove one previously captured child contribution."""

        step = 1 if direction > 0 else -1
        status = contribution.status
        self.statuses[status] += step
        if self.statuses[status] <= 0:
            self.statuses.pop(status, None)
        self.child_count += step
        self.speed_bps += step * contribution.speed_bps
        self.downloaded_bytes += step * contribution.downloaded_bytes
        self.total_bytes += step * contribution.total_bytes
        if contribution.known_size > 0:
            self.known_count += step
            self.known_total += step * contribution.known_size
            self.known_done += step * contribution.known_done
        else:
            self.unknown_count += step
            self.unknown_progress_sum += step * contribution.unknown_fraction

        # Add/remove updates can accumulate tiny binary floating-point residue.
        # Leaving it in the cache makes an idle parent report a non-zero speed.
        if abs(self.speed_bps) < 1e-6:
            self.speed_bps = 0.0
        if abs(self.known_done) < 1e-6:
            self.known_done = 0.0
        if abs(self.unknown_progress_sum) < 1e-9:
            self.unknown_progress_sum = 0.0


@dataclass(frozen=True, slots=True)
class CollectionSummary:
    status: str
    stage_text: str
    progress: float
    speed_bps: float
    downloaded_bytes: int
    total_bytes: int
    eta: str
    metadata: dict[str, int]
    terminal: bool


def collection_child_contribution(
    *,
    parent_id: Any,
    status: Any,
    speed_bps: Any,
    total_bytes: Any,
    downloaded_bytes: Any,
    progress: Any,
) -> CollectionChildContribution | None:
    normalized_parent = str(parent_id or "")
    if not normalized_parent:
        return None
    normalized_status = str(status or "queued")
    total = non_negative_int(total_bytes)
    downloaded = non_negative_int(downloaded_bytes)
    if total > 0:
        known_done = float(
            total if normalized_status == "completed" else min(total, downloaded)
        )
        unknown_fraction = 0.0
    else:
        known_done = 0.0
        unknown_fraction = (
            1.0
            if normalized_status == "completed"
            else bounded_percent(progress) / 100.0
        )
    return CollectionChildContribution(
        parent_id=normalized_parent,
        status=normalized_status,
        speed_bps=(
            non_negative_float(speed_bps)
            if normalized_status == "downloading" else 0.0
        ),
        known_size=total,
        known_done=known_done,
        unknown_fraction=unknown_fraction,
        downloaded_bytes=downloaded,
        total_bytes=total,
    )


def collection_status_counts(aggregate: CollectionAggregate) -> dict[str, int]:
    counts = aggregate.statuses
    completed = non_negative_int(counts.get("completed"))
    partial_failed = non_negative_int(counts.get("partial_failed"))
    missing = non_negative_int(counts.get("deleted"))
    failed = (
        non_negative_int(counts.get("failed")) + partial_failed + missing
    )
    canceled = non_negative_int(counts.get("canceled"))
    paused = non_negative_int(counts.get("paused")) + non_negative_int(
        counts.get("暂停中")
    )
    active = sum(
        non_negative_int(counts.get(status)) for status in ACTIVE_CHILD_STATUSES
    )
    queued = non_negative_int(counts.get("queued"))
    return {
        "completed": completed,
        "partial_failed": partial_failed,
        "failed": failed,
        "canceled": canceled,
        "paused": paused,
        "active": active,
        "queued": queued,
    }


def collection_parent_status(
    child_count: int,
    counts: Mapping[str, int],
) -> tuple[str, str, bool]:
    count = non_negative_int(child_count)
    completed = non_negative_int(counts.get("completed"))
    failed = non_negative_int(counts.get("failed"))
    canceled = non_negative_int(counts.get("canceled"))
    terminal = count > 0 and completed + failed + canceled >= count
    if terminal:
        if completed == count:
            return "completed", "全部下载完成", True
        if completed or non_negative_int(counts.get("partial_failed")):
            return "partial_failed", "部分任务失败", True
        if canceled == count:
            return "canceled", "已取消", True
        return "failed", "下载失败", True
    if non_negative_int(counts.get("active")):
        return "downloading", "正在下载子任务", False
    if (
        non_negative_int(counts.get("paused"))
        and not non_negative_int(counts.get("queued"))
    ):
        return "paused", "已暂停", False
    return "queued", "子任务排队中", False


def _collection_transfer_summary(
    aggregate: CollectionAggregate,
) -> tuple[float, float, int, int, str]:
    known_count = non_negative_int(aggregate.known_count)
    unknown_count = non_negative_int(aggregate.unknown_count)
    known_total = non_negative_int(aggregate.known_total)
    known_done = min(
        float(known_total),
        non_negative_float(aggregate.known_done),
    )
    unknown_progress = min(
        float(unknown_count),
        non_negative_float(aggregate.unknown_progress_sum),
    )
    fallback_weight = (
        max(1.0, known_total / known_count) if known_count > 0 else 1.0
    )
    weighted_total = known_total + unknown_count * fallback_weight
    weighted_done = known_done + unknown_progress * fallback_weight
    progress = (
        min(100.0, weighted_done * 100.0 / weighted_total)
        if weighted_total else 0.0
    )
    speed_bps = non_negative_float(aggregate.speed_bps)
    downloaded = non_negative_int(aggregate.downloaded_bytes)
    # Any unknown child makes a combined byte total and ETA misleading.
    total = 0 if unknown_count else non_negative_int(aggregate.total_bytes)
    if total:
        downloaded = min(downloaded, total)
    remaining = max(0, total - downloaded)
    eta = format_eta(remaining / speed_bps) if speed_bps > 0 and remaining else ""
    return progress, speed_bps, downloaded, total, eta


def summarize_collection(
    aggregate: CollectionAggregate,
    parsed_count: Any,
) -> CollectionSummary:
    child_count = non_negative_int(aggregate.child_count)
    counts = collection_status_counts(aggregate)
    status, stage_text, terminal = collection_parent_status(child_count, counts)
    progress, speed_bps, downloaded, total, eta = _collection_transfer_summary(
        aggregate
    )
    return CollectionSummary(
        status=status,
        stage_text=stage_text,
        progress=progress,
        speed_bps=speed_bps,
        downloaded_bytes=downloaded,
        total_bytes=total,
        eta=eta,
        metadata={
            "selected": child_count,
            "completed": counts["completed"],
            "failed": counts["failed"],
            "canceled": counts["canceled"],
            "paused": counts["paused"],
            "active": counts["active"],
            "queued": counts["queued"],
            "skipped": max(0, non_negative_int(parsed_count) - child_count),
        },
        terminal=terminal,
    )


__all__ = [
    "CollectionAggregate",
    "CollectionChildContribution",
    "CollectionSummary",
    "collection_child_contribution",
    "collection_parent_status",
    "collection_status_counts",
    "summarize_collection",
]

from __future__ import annotations

import os
import math
from typing import Any


DownloadPerformance = tuple[int, int, float]


def normalize_download_performance_mode(value: object) -> str:
    return "manual" if str(value or "").strip().casefold() == "manual" else "smart"


def normalize_download_performance_values(
    max_concurrent: object,
    fragment_concurrent: object,
    request_delay: object,
) -> DownloadPerformance:
    """Return the one bounded performance snapshot used by UI and services."""
    return (
        _bounded_int(max_concurrent, 3, 1, 8),
        _bounded_int(fragment_concurrent, 12, 1, 32),
        _bounded_float(request_delay, 0.0, 0.0, 60.0),
    )


def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _bounded_float(value: object, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    if not math.isfinite(parsed):
        parsed = default
    return max(minimum, min(parsed, maximum))


def smart_download_performance(logical_processors: int | None = None) -> DownloadPerformance:
    """Return a conservative automatic download-concurrency profile."""

    processors = _bounded_int(
        logical_processors if logical_processors is not None else (os.cpu_count() or 4),
        default=4,
        minimum=1,
        maximum=1_000_000,
    )
    if processors <= 2:
        return 1, 6, 0.5
    if processors <= 4:
        return 2, 8, 0.0
    if processors <= 8:
        return 3, 8, 0.0
    return 4, 8, 0.0


def effective_download_performance(
    settings: Any,
    logical_processors: int | None = None,
) -> DownloadPerformance:
    """Resolve the automatic profile or validated saved manual values."""

    if normalize_download_performance_mode(settings.get("download_performance_mode")) == "smart":
        return smart_download_performance(logical_processors)
    return normalize_download_performance_values(
        settings.get("max_concurrent"),
        settings.get("fragment_concurrent"),
        settings.get("request_delay"),
    )

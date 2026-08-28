from __future__ import annotations

import re
from collections.abc import Callable

from app.core.log_service import DownloadLogService


_ANSI_ESCAPE_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)?)"
)
_CONTROL_CHARACTER_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_LOG_LEVELS = frozenset({"debug", "info", "warning", "error"})


def normalize_ytdlp_log_message(message: object) -> str:
    """Return one stable, printable line for logs and stage detection."""

    try:
        raw = str(message or "")
    except Exception:
        raw = f"<{type(message).__name__}>"
    cleaned = _ANSI_ESCAPE_RE.sub("", raw)
    cleaned = _CONTROL_CHARACTER_RE.sub("", cleaned)
    return re.sub(r"[\r\n]+", " ", cleaned).strip()


def ytdlp_log_level(message: object, default: str = "debug") -> str:
    """Infer standalone yt-dlp severity after removing terminal styling."""

    normalized = normalize_ytdlp_log_message(message).casefold()
    if re.search(r"(?:^|\s|\])error\s*:", normalized):
        return "error"
    if re.search(r"(?:^|\s|\])warning\s*:", normalized):
        return "warning"
    try:
        fallback = str(default or "debug").strip().casefold()
    except Exception:
        fallback = "debug"
    return fallback if fallback in _LOG_LEVELS else "debug"


class YtdlpLogger:
    """Bridge yt-dlp diagnostics into a non-fatal structured log callback."""

    def __init__(self, callback: Callable[[str, str, str], None]) -> None:
        self._callback = callback
        self._debug_count = 0

    def _emit(self, level: str, category: str, message: str) -> None:
        # yt-dlp invokes logger methods inside extraction and download paths.
        # Diagnostics must never turn an otherwise successful download into a
        # failure when a filesystem or presentation callback is unavailable.
        try:
            self._callback(level, category, message)
        except Exception:
            return

    def debug(self, message: str) -> None:
        normalized = normalize_ytdlp_log_message(message)
        if (
            normalized
            and not normalized.startswith("[debug] Progress")
            and self._debug_count < 300
        ):
            self._debug_count += 1
            self._emit("debug", "yt-dlp", normalized)

    def info(self, message: str) -> None:
        normalized = normalize_ytdlp_log_message(message)
        if normalized:
            self._emit("info", "yt-dlp", normalized)

    def warning(self, message: str) -> None:
        normalized = normalize_ytdlp_log_message(message)
        if normalized:
            self._emit(
                "warning",
                DownloadLogService.classify_error(normalized),
                normalized,
            )

    def error(self, message: str) -> None:
        normalized = normalize_ytdlp_log_message(message)
        if normalized:
            self._emit(
                "error",
                DownloadLogService.classify_error(normalized),
                normalized,
            )


class SilentYtdlpProbeLogger:
    """Suppress duplicate extractor chatter during adaptive Cookie probes."""

    def debug(self, _message: str) -> None:
        return

    def info(self, _message: str) -> None:
        return

    def warning(self, _message: str) -> None:
        return

    def error(self, _message: str) -> None:
        return

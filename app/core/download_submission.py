from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import urlparse

from app.core.cookie_sources import (
    COOKIE_SOURCE_NONE,
    normalize_cookie_browser,
    normalize_cookie_source,
)
from app.core.paths import resolve_portable_path
from app.core.subtitles import normalize_subtitle_language
from app.core.transcode_service import (
    normalize_transcode_encoder,
    transcode_encoder_codec,
    transcode_encoder_device,
)


DOWNLOAD_SUBMIT_DEBOUNCE_SECONDS = 1.2
VALID_PROXY_SCHEMES = frozenset({"http", "https", "socks4", "socks5", "socks5h"})


class DownloadSubmissionSettingsError(ValueError):
    """A stable validation failure that the UI can present in its language."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def build_download_request_context(
    settings: Any,
    *,
    options_json: Mapping[str, object],
) -> dict[str, object]:
    """Snapshot settings for a new task without probing tools or writing to disk."""

    raw_output_path = str(settings.get("download_dir") or "").strip()
    if not raw_output_path:
        raise DownloadSubmissionSettingsError("missing_download_dir")

    proxy = str(settings.get("proxy") or "").strip()
    if proxy:
        parsed_proxy = urlparse(proxy)
        if (
            parsed_proxy.scheme.lower() not in VALID_PROXY_SCHEMES
            or not parsed_proxy.netloc
        ):
            raise DownloadSubmissionSettingsError("invalid_proxy")

    playlist_mode = str(settings.get("playlist_mode") or "auto")
    if playlist_mode not in {"auto", "single", "playlist"}:
        playlist_mode = "auto"

    cookie_file = str(settings.get_resolved_path("download_cookie_file") or "")
    cookie_source = normalize_cookie_source(settings.get("download_cookie_source"))

    transcode_encoder = normalize_transcode_encoder(settings.get("transcode_encoder"))
    return {
        "output_dir": str(resolve_portable_path(raw_output_path)),
        "proxy": proxy,
        "cookie_file": cookie_file,
        "cookie_source": cookie_source,
        "cookie_browser": normalize_cookie_browser(settings.get("download_cookie_browser")),
        "cookie_profile": str(settings.get("download_cookie_profile") or "").strip(),
        "cookie_keyring": str(settings.get("download_cookie_keyring") or "").strip(),
        "cookie_container": str(settings.get("download_cookie_container") or "").strip(),
        "quality": str(settings.get("quality") or "best"),
        "filename_template": str(settings.get("filename_template") or ""),
        "organize_task_folder": settings.get_bool("organize_task_folder", False),
        "ffmpeg_path": str(settings.get("ffmpeg_path") or ""),
        "download_album": playlist_mode == "playlist",
        "playlist_mode": playlist_mode,
        "transcode_encoder": transcode_encoder,
        "transcode_codec": transcode_encoder_codec(transcode_encoder),
        "transcode_device": transcode_encoder_device(transcode_encoder),
        "subtitle_language": normalize_subtitle_language(settings.get("subtitle_language")),
        "prepend_cover_enabled": settings.get_bool("prepend_cover_enabled", False),
        "prepend_cover_frames": settings.get_int(
            "prepend_cover_frames",
            3,
            1,
            300,
        ),
        "options_json": dict(options_json),
    }


def submission_playlist_mode(
    context: Mapping[str, object],
    *,
    collection_mode: str,
) -> str:
    """Resolve one playlist mode for duplicate detection and service enqueue."""

    configured = str(context.get("playlist_mode") or "auto")
    if configured not in {"auto", "single", "playlist"}:
        configured = "auto"
    if configured == "single" or collection_mode == "single":
        return "single"
    return configured


def service_task_arguments(
    context: Mapping[str, object],
    *,
    playlist_mode: str,
) -> dict[str, object]:
    """Map one immutable task snapshot onto DownloadService arguments."""

    return {
        "proxy": str(context.get("proxy") or ""),
        "cookie_file": str(context.get("cookie_file") or ""),
        "cookie_source": str(context.get("cookie_source") or COOKIE_SOURCE_NONE),
        "cookie_browser": str(context.get("cookie_browser") or "chrome"),
        "cookie_profile": str(context.get("cookie_profile") or ""),
        "cookie_keyring": str(context.get("cookie_keyring") or ""),
        "cookie_container": str(context.get("cookie_container") or ""),
        "quality": str(context.get("quality") or "best"),
        "filename_template": str(context.get("filename_template") or ""),
        "organize_task_folder": bool(context.get("organize_task_folder", False)),
        "ffmpeg_path": str(context.get("ffmpeg_path") or ""),
        "download_album": playlist_mode == "playlist",
        "playlist_mode": playlist_mode,
        "transcode_codec": str(context.get("transcode_codec") or "original"),
        "transcode_device": str(context.get("transcode_device") or "auto"),
        "transcode_encoder": str(context.get("transcode_encoder") or "original"),
        "subtitle_language": str(context.get("subtitle_language") or "none"),
        "prepend_cover_enabled": bool(context.get("prepend_cover_enabled", False)),
        "prepend_cover_frames": int(context.get("prepend_cover_frames") or 3),
        "options_json": dict(context.get("options_json") or {}),
    }


@dataclass(slots=True)
class DownloadSubmissionDebouncer:
    interval_seconds: float = DOWNLOAD_SUBMIT_DEBOUNCE_SECONDS
    last_signature: tuple[str, ...] = field(default_factory=tuple)
    last_submitted_at: float = 0.0
    guard_until: float = 0.0

    def rejects(self, links: list[str], *, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else float(now)
        signature = tuple(links)
        if (
            signature
            and signature == self.last_signature
            and current - self.last_submitted_at < self.interval_seconds
        ):
            return True
        self.last_signature = signature
        self.last_submitted_at = current
        self.guard_until = current + self.interval_seconds
        return False

    def suppresses_empty_followup(self, *, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else float(now)
        return current < self.guard_until

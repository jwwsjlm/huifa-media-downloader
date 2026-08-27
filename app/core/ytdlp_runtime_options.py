from __future__ import annotations

"""Pure construction of bounded yt-dlp download options.

Path discovery, runtime probing and logging belong to the worker.  This module
only combines already-resolved values so builtin and standalone yt-dlp receive
the same option snapshot.
"""

from dataclasses import dataclass, field
from typing import Any, Mapping

from app.core.download_options import DownloadOptions
from app.core.download_performance import normalize_download_performance_values
from app.core.subtitles import subtitle_ytdlp_options


@dataclass(frozen=True, slots=True)
class YtdlpDownloadOptionRequest:
    output_template: str
    output_dir: str
    quality: str
    download_options: DownloadOptions
    subtitle_language: str = "none"
    playlist_mode: str = "auto"
    fragment_concurrent: int = 1
    processing_workspace: str = ""
    filename_limit: int | None = None
    request_delay: float = 0.0
    ffmpeg_location: str = ""
    proxy: str = ""
    ejs_options: Mapping[str, Any] = field(default_factory=dict)
    remove_remux_postprocessor: bool = False
    windows_filenames: bool = False


def _advanced_options(request: YtdlpDownloadOptionRequest) -> dict[str, Any]:
    options = dict(request.download_options.ytdlp_options())
    processors = [
        dict(processor)
        for processor in (options.get("postprocessors") or ())
        if isinstance(processor, Mapping)
    ]
    if request.remove_remux_postprocessor:
        options.pop("merge_output_format", None)
        processors = [
            processor
            for processor in processors
            if str(processor.get("key") or "") != "FFmpegVideoRemuxer"
        ]
    if processors:
        options["postprocessors"] = processors
    else:
        options.pop("postprocessors", None)
    return options


def build_ytdlp_download_options(
    request: YtdlpDownloadOptionRequest,
) -> dict[str, Any]:
    """Return Python-API options shared by builtin and external core modes."""

    advanced = _advanced_options(request)
    _, fragment_concurrent, request_delay = normalize_download_performance_values(
        1,
        request.fragment_concurrent,
        request.request_delay,
    )
    options: dict[str, Any] = {
        "outtmpl": str(request.output_template),
        "windowsfilenames": bool(request.windows_filenames),
        "format": request.download_options.video_format_selector(request.quality),
        "writethumbnail": True,
        "writeinfojson": True,
        **subtitle_ytdlp_options(request.subtitle_language),
        "noplaylist": request.playlist_mode == "single",
        "concurrent_fragment_downloads": fragment_concurrent,
        "retries": 5,
        "fragment_retries": 10,
        "extractor_retries": 3,
        "file_access_retries": 3,
        "socket_timeout": 30,
        "continuedl": True,
        "quiet": True,
        "no_warnings": True,
    }
    if request.processing_workspace:
        options["paths"] = {
            "home": str(request.output_dir),
            "temp": str(request.processing_workspace),
        }
    if request.filename_limit:
        options["trim_file_name"] = int(request.filename_limit)

    options.update({
        key: value
        for key, value in advanced.items()
        if key not in {
            "format",
            "format_sort",
            "postprocessors",
            # This display-oriented value is translated to ``ratelimit`` by
            # DownloadOptions and is not a YoutubeDL Python API parameter.
            "ratelimit_text",
        }
    })
    if request.download_options.content_mode == "audio":
        options["format"] = advanced.get("format", "bestaudio/best")
        options.pop("format_sort", None)
    else:
        options["format_sort"] = request.download_options.format_sort(request.quality)
    if advanced.get("postprocessors"):
        options["postprocessors"] = list(advanced["postprocessors"])

    options.update(dict(request.ejs_options))
    if request_delay > 0:
        options["sleep_interval_requests"] = request_delay
    if request.ffmpeg_location:
        options["ffmpeg_location"] = str(request.ffmpeg_location)
    if request.proxy:
        options["proxy"] = str(request.proxy)
    return options

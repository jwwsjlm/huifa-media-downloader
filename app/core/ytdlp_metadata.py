from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from math import isfinite
import re
from typing import Any, Iterable, Mapping


YTDLP_COLLECTION_RESULT_TYPES = frozenset({"playlist", "multi_video"})


def is_ytdlp_collection_result(info: Mapping[str, Any]) -> bool:
    """Return whether a yt-dlp result is an aggregate media container."""

    return (
        str(info.get("_type") or "").strip().casefold()
        in YTDLP_COLLECTION_RESULT_TYPES
    )


def _iter_mappings(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        return (value,)
    try:
        items = iter(value or ())
    except TypeError:
        return ()
    return (item for item in items if isinstance(item, Mapping))


def _number(value: Any) -> float:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, number) if isfinite(number) else 0.0


def _integer(value: Any) -> int:
    return int(_number(value))


def _quality_dimension(width: int, height: int) -> int:
    """Return the conventional ``p`` dimension for landscape or portrait media."""

    if width > 0 and height > 0:
        return min(width, height)
    return max(width, height)


def _format_dimensions(item: Mapping[str, Any]) -> tuple[int, int]:
    width = _integer(item.get("width"))
    height = _integer(item.get("height"))
    if width > 0 and height > 0:
        return width, height
    resolution = str(item.get("resolution") or "").strip()
    match = re.search(r"(?i)(\d{2,5})\s*[x×]\s*(\d{2,5})", resolution)
    if match:
        return width or int(match.group(1)), height or int(match.group(2))
    if height <= 0:
        note = f"{resolution} {item.get('format_note') or ''}"
        match = re.search(r"(?i)(\d{3,5})\s*p", note)
        if match:
            height = int(match.group(1))
    return width, height


def _resolution_rank(item: Mapping[str, Any]) -> tuple[int, int]:
    width, height = _format_dimensions(item)
    quality = _quality_dimension(width, height)
    pixels = width * height
    if pixels <= 0 and quality > 0:
        pixels = quality * quality * 16 // 9
    return quality, pixels


def _format_is_hdr(item: Mapping[str, Any]) -> bool:
    dynamic_range = str(item.get("dynamic_range") or "").strip().casefold()
    format_note = str(item.get("format_note") or "").strip().casefold()
    return dynamic_range not in {"", "sdr", "sdr tv"} or "hdr" in format_note


def _format_codec(item: Mapping[str, Any], key: str) -> str:
    return str(item.get(key) or "").strip()


def _has_codec(item: Mapping[str, Any], key: str) -> bool:
    return _format_codec(item, key).casefold() not in {"", "none"}


def _video_format_rank(item: Mapping[str, Any]) -> tuple[int, int, float, float]:
    return (
        *_resolution_rank(item),
        _number(item.get("fps")),
        _number(item.get("tbr") or item.get("vbr")),
    )


def _resolution_label(item: Mapping[str, Any]) -> str:
    quality = _quality_dimension(*_format_dimensions(item))
    for minimum, label in (
        (6_000, "12K"),
        (4_000, "8K"),
        (2_800, "5K"),
        (2_000, "4K"),
    ):
        if quality >= minimum:
            return label
    if quality:
        return f"{quality}p"
    return str(item.get("resolution") or item.get("format_note") or "").strip()


def _video_codec_label(item: Mapping[str, Any]) -> str:
    raw_codec = _format_codec(item, "vcodec")
    codec_key = raw_codec.split(".", 1)[0].casefold()
    return {
        "av01": "AV1",
        "av1": "AV1",
        "vp9": "VP9",
        "vp09": "VP9",
        "avc1": "H.264",
        "h264": "H.264",
        "hev1": "H.265",
        "hvc1": "H.265",
        "hevc": "H.265",
        "h265": "H.265",
    }.get(codec_key, codec_key.upper())


def _dynamic_range_label(item: Mapping[str, Any]) -> str:
    dynamic_range = str(item.get("dynamic_range") or "").strip().upper()
    if dynamic_range and dynamic_range not in {"SDR", "SDR TV"}:
        return dynamic_range
    return "HDR" if "hdr" in str(item.get("format_note") or "").casefold() else ""


def _walk_nested_mappings(
    root: Mapping[str, Any],
    child_keys: tuple[str, ...],
    *,
    max_items: int,
) -> Iterable[Mapping[str, Any]]:
    """Breadth-first mapping traversal with identity-cycle and size guards."""

    pending: deque[Mapping[str, Any]] = deque([root])
    seen: set[int] = set()
    yielded = 0
    limit = max(1, int(max_items))
    while pending and yielded < limit:
        current = pending.popleft()
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        yielded += 1
        yield current
        for key in child_keys:
            pending.extend(_iter_mappings(current.get(key)))


def media_source_key(info: Mapping[str, Any] | None) -> str:
    """Build a stable extractor/id identity for one yt-dlp media result."""

    if not isinstance(info, Mapping):
        return ""
    extractor = str(
        info.get("extractor_key") or info.get("extractor") or "generic"
    ).strip().casefold()
    identifier = str(info.get("id") or info.get("display_id") or "").strip()
    return f"{extractor}:{identifier}" if extractor and identifier else ""


def selected_video_quality(info: Mapping[str, Any] | None) -> str:
    """Describe the actual video format selected by yt-dlp for task display."""

    if not isinstance(info, Mapping):
        return ""
    candidates = _walk_nested_mappings(
        info,
        ("requested_formats", "requested_downloads"),
        max_items=128,
    )
    video = max(
        (
            item for item in candidates
            if _has_codec(item, "vcodec")
        ),
        key=_video_format_rank,
        default=None,
    )
    if video is None:
        return ""

    width, height = _format_dimensions(video)
    parts = [_resolution_label(video)]
    if width and height:
        parts.append(f"{width}×{height}")
    fps = _number(video.get("fps"))
    if fps > 0:
        fps_text = str(int(fps)) if fps.is_integer() else f"{fps:g}"
        parts.append(f"{fps_text} FPS")

    codec = _video_codec_label(video)
    if codec:
        parts.append(codec)
    parts.append(_dynamic_range_label(video))
    return " · ".join(dict.fromkeys(part for part in parts if part))


@dataclass(frozen=True, slots=True)
class MediaCapabilityProfile:
    """Comparable media capability summary for anonymous/Cookie probes."""

    usable: bool = False
    collection_items: int = 0
    playable_formats: int = 0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    hdr: bool = False
    video_bitrate: float = 0.0
    audio_bitrate: float = 0.0

    @property
    def score(self) -> tuple[int, ...]:
        pixels = self.width * self.height
        quality = _quality_dimension(self.width, self.height)
        if pixels <= 0 and quality > 0:
            pixels = quality * quality * 16 // 9
        return (
            1 if self.usable else 0,
            max(0, self.collection_items),
            max(0, quality),
            max(0, pixels),
            max(0, int(round(self.fps * 10))),
            1 if self.hdr else 0,
            max(0, int(self.video_bitrate // 100)),
            max(0, int(self.audio_bitrate // 16)),
        )

    def as_log_details(self) -> dict[str, Any]:
        return {
            "usable": self.usable,
            "collection_items": self.collection_items,
            "playable_formats": self.playable_formats,
            "width": self.width,
            "height": self.height,
            "fps": round(self.fps, 3),
            "hdr": self.hdr,
            "video_bitrate_kbps": round(self.video_bitrate, 3),
            "audio_bitrate_kbps": round(self.audio_bitrate, 3),
        }


def _iter_capability_formats(
    entries: Iterable[Mapping[str, Any]],
    max_formats: int,
) -> Iterable[tuple[int, Mapping[str, Any]]]:
    inspected = 0
    limit = max(0, int(max_formats))
    for entry_index, entry in enumerate(entries):
        raw_formats = entry.get("formats")
        candidates = (entry,) if raw_formats is None else _iter_mappings(raw_formats)
        for item in candidates:
            if inspected >= limit:
                return
            inspected += 1
            yield entry_index, item


def _format_playability(
    item: Mapping[str, Any],
    *,
    allow_direct_media: bool,
) -> tuple[bool, bool] | None:
    if item.get("has_drm"):
        return None
    protocol = str(item.get("protocol") or "").casefold()
    extension = str(item.get("ext") or "").casefold()
    format_id = str(item.get("format_id") or "").casefold()
    if (
        extension == "mhtml"
        or protocol in {"mhtml", "images"}
        or format_id.startswith("sb")
    ):
        return None
    has_video = _has_codec(item, "vcodec")
    has_audio = _has_codec(item, "acodec")
    if not has_video and not has_audio and not (
        allow_direct_media and bool(item.get("url"))
    ):
        return None
    return has_video, has_audio


def _video_capability_rank(
    item: Mapping[str, Any],
) -> tuple[int, int, float, int, float, int, int]:
    width, height = _format_dimensions(item)
    quality, pixels = _resolution_rank(item)
    return (
        quality,
        pixels,
        _number(item.get("fps")),
        1 if _format_is_hdr(item) else 0,
        _number(item.get("tbr")),
        width,
        height,
    )


def media_capability_profile(
    info: Mapping[str, Any] | None,
    *,
    max_entries: int = 100,
    max_formats: int = 2_000,
) -> MediaCapabilityProfile:
    """Summarize the genuinely playable media exposed by an extractor."""

    if not isinstance(info, Mapping):
        return MediaCapabilityProfile()

    root_type = str(info.get("_type") or "").strip().casefold()
    is_collection = is_ytdlp_collection_result(info)
    walked = list(_walk_nested_mappings(
        info,
        ("entries",),
        max_items=max(1, max_entries) + (1 if is_collection else 0),
    ))
    entries = walked[1:] if is_collection else walked[:max_entries]

    playable_formats = 0
    playable_entries: set[int] = set()
    best_video: tuple[int, int, float, int, float, int, int] | None = None
    best_audio = 0.0
    allow_direct_media = root_type not in {"url", "url_transparent"}
    for entry_index, item in _iter_capability_formats(entries, max_formats):
        playability = _format_playability(
            item,
            allow_direct_media=allow_direct_media,
        )
        if playability is None:
            continue
        has_video, has_audio = playability
        playable_formats += 1
        playable_entries.add(entry_index)
        if has_audio:
            best_audio = max(
                best_audio,
                _number(item.get("abr")),
                _number(item.get("tbr")),
            )
        if has_video:
            candidate = _video_capability_rank(item)
            if best_video is None or candidate[:5] > best_video[:5]:
                best_video = candidate

    collection_items = len(playable_entries) if is_collection else (1 if entries else 0)
    if best_video is None:
        return MediaCapabilityProfile(
            usable=playable_formats > 0,
            collection_items=collection_items,
            playable_formats=playable_formats,
            audio_bitrate=best_audio,
        )
    _, _, fps, hdr, video_bitrate, width, height = best_video
    return MediaCapabilityProfile(
        usable=playable_formats > 0,
        collection_items=collection_items,
        playable_formats=playable_formats,
        width=width,
        height=height,
        fps=fps,
        hdr=bool(hdr),
        video_bitrate=video_bitrate,
        audio_bitrate=best_audio,
    )


def _video_choice_identity(
    item: Mapping[str, Any],
) -> tuple[int, int, int, int, str, str, str, str, str, bool, bool]:
    width, height = _format_dimensions(item)
    return (
        _quality_dimension(width, height),
        width,
        height,
        _integer(item.get("fps")),
        _format_codec(item, "vcodec").split(".", 1)[0].casefold(),
        _format_codec(item, "acodec").split(".", 1)[0].casefold(),
        str(item.get("ext") or "?").casefold(),
        str(item.get("language") or "").strip().casefold(),
        str(item.get("format_note") or "").strip().casefold(),
        _has_codec(item, "acodec"),
        _format_is_hdr(item),
    )


def _build_video_choice(item: Mapping[str, Any]) -> dict[str, Any] | None:
    width, source_height = _format_dimensions(item)
    quality = _quality_dimension(width, source_height)
    format_id = str(item.get("format_id") or "").strip()
    if not quality or not format_id or not _has_codec(item, "vcodec"):
        return None
    ext = str(item.get("ext") or "?")
    fps = _integer(item.get("fps"))
    vcodec = _format_codec(item, "vcodec").split(".", 1)[0]
    has_audio = _has_codec(item, "acodec")
    hdr = _format_is_hdr(item)
    format_note = str(item.get("format_note") or "")
    language = str(item.get("language") or "").strip()
    parts = [f"{quality}p"]
    if width and source_height:
        parts[0] += f" ({width}×{source_height})"
    parts.extend((ext, f"{fps or '?'}fps", vcodec))
    if hdr and "hdr" not in format_note.casefold():
        parts.append("HDR")
    if has_audio:
        parts.append("audio")
    parts.append(language)
    parts.append(format_note)
    return {
        "kind": "video",
        "label": "  ·  ".join(part for part in parts if part),
        "selector": format_id if has_audio else f"{format_id}+bestaudio/best",
        "height": quality,
        "width": width,
        "source_height": source_height,
        "ext": ext,
        "fps": str(fps or ""),
        "codec": vcodec,
        "has_audio": has_audio,
        "hdr": hdr,
        "language": language,
        "format_note": format_note,
        "filesize": _integer(item.get("filesize") or item.get("filesize_approx")),
    }


def _video_format_choices(
    available_formats: Iterable[Mapping[str, Any]],
    *,
    limit: int = 48,
) -> list[dict[str, Any]]:
    formats = sorted(available_formats, key=_video_format_rank, reverse=True)
    choices: list[dict[str, Any]] = []
    seen: set[
        tuple[int, int, int, int, str, str, str, str, str, bool, bool]
    ] = set()
    for item in formats:
        choice = _build_video_choice(item)
        if choice is None:
            continue
        identity = _video_choice_identity(item)
        if identity in seen:
            continue
        seen.add(identity)
        choices.append(choice)
        if len(choices) >= max(0, int(limit)):
            break
    return choices


def _best_audio_choice() -> dict[str, Any]:
    return {
        "kind": "audio",
        "label": "Best available audio",
        "selector": "bestaudio/best",
        "ext": "auto",
        "codec": "",
        "abr": 0,
        "language": "",
        "filesize": 0,
    }


def _audio_format_choices(
    available_formats: Iterable[Mapping[str, Any]],
    *,
    limit: int = 24,
) -> list[dict[str, Any]]:
    audio_formats = sorted(
        (
            item for item in available_formats
            if item.get("format_id")
            and not _has_codec(item, "vcodec")
            and _has_codec(item, "acodec")
        ),
        key=lambda item: _number(item.get("abr") or item.get("tbr")),
        reverse=True,
    )
    choices: list[dict[str, Any]] = []
    seen_audio: set[tuple[str, int, str, str]] = set()
    for item in audio_formats:
        acodec = str(item.get("acodec") or "unknown")
        ext = str(item.get("ext") or "?")
        abr = int(round(_number(item.get("abr") or item.get("tbr"))))
        language = str(item.get("language") or "")
        identity = (acodec.casefold(), abr, language.casefold(), ext.casefold())
        if identity in seen_audio:
            continue
        seen_audio.add(identity)
        label = f"{ext}  ·  {acodec}"
        if abr:
            label += f"  ·  {abr} kbps"
        if language:
            label += f"  ·  {language}"
        choices.append({
            "kind": "audio",
            "label": label,
            "selector": str(item["format_id"]),
            "ext": ext,
            "codec": acodec,
            "abr": abr,
            "language": language,
            "filesize": _integer(item.get("filesize") or item.get("filesize_approx")),
        })
        if len(choices) >= max(0, int(limit)):
            break
    return choices


def build_format_choices(info: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Build bounded, de-duplicated manual video/audio format choices."""

    available_formats = list(_iter_mappings(info.get("formats")))
    return [
        *_video_format_choices(available_formats),
        _best_audio_choice(),
        *_audio_format_choices(available_formats),
    ]

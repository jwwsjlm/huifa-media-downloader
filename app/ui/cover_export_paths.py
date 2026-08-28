from __future__ import annotations

from pathlib import Path

from app.core.filename_rules import windows_filename_stem
from app.storage.models import MediaItem

_MAX_DEFAULT_STEM_LENGTH = 120


def safe_cover_stem(media: MediaItem) -> str:
    video_path = str(media.video_path or "").strip()
    if video_path:
        raw_stem = Path(video_path).stem
    else:
        raw_stem = str(media.title or "cover").strip()
    return windows_filename_stem(
        raw_stem,
        fallback="cover",
        max_length=_MAX_DEFAULT_STEM_LENGTH,
    )


def default_cover_export_path(
    media: MediaItem,
    *,
    width: int,
    height: int,
) -> Path:
    video_path = str(media.video_path or "").strip()
    thumbnail_path = str(media.thumbnail_path or "").strip()
    directory = (
        Path(video_path).parent
        if video_path
        else Path(thumbnail_path).parent
    )
    return directory / f"{safe_cover_stem(media)}-{int(width)}x{int(height)}.jpg"


def normalized_jpeg_target(target: str | Path) -> Path:
    path = Path(target)
    if path.suffix.casefold() not in {".jpg", ".jpeg"}:
        path = path.with_suffix(".jpg")
    return path

from __future__ import annotations

import unicodedata
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def normalize_source_url(value: object) -> str:
    """Return a stable comparison form without changing the requested URL."""

    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = urlsplit(text)
    except ValueError:
        return text
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return text
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    return urlunsplit((
        parsed.scheme.casefold(),
        parsed.netloc.casefold(),
        path,
        parsed.query,
        "",
    ))


def normalize_media_title(value: object) -> str:
    """Normalize a video title for lightweight duplicate detection."""

    text = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(text.split()).casefold()


def normalize_source_key(value: object) -> str:
    """Normalize only the extractor part; platform IDs may be case-sensitive."""

    text = str(value or "").strip()
    if not text:
        return ""
    extractor, separator, identifier = text.partition(":")
    if not separator:
        return text
    extractor = extractor.strip().casefold()
    identifier = identifier.strip()
    return f"{extractor}:{identifier}" if extractor and identifier else ""


def media_identity(source_url: object, title: object, video_path: object = "") -> str:
    """Identify media by source link, falling back to its visible video name."""

    normalized_url = normalize_source_url(source_url)
    if normalized_url:
        return f"url:{normalized_url}"
    normalized_title = normalize_media_title(title)
    if normalized_title:
        return f"title:{normalized_title}"
    path_text = str(video_path or "").strip()
    if path_text:
        normalized_name = normalize_media_title(Path(path_text).stem)
        if normalized_name:
            return f"title:{normalized_name}"
    return "unknown"

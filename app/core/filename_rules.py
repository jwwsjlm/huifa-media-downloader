from __future__ import annotations

import hashlib
import re


_WINDOWS_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_SAFE_ASCII_COMPONENT = re.compile(r"[^A-Za-z0-9_.-]")
_WINDOWS_RESERVED_STEMS = frozenset({
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
})


def is_windows_reserved_stem(value: object) -> bool:
    stem = str(value or "").split(".", 1)[0].rstrip(" .").upper()
    return stem in _WINDOWS_RESERVED_STEMS


def windows_filename_stem(
    value: object,
    *,
    fallback: str,
    max_length: int,
) -> str:
    stem = _WINDOWS_INVALID_FILENAME.sub("_", str(value or fallback))
    stem = " ".join(stem.split()).strip(" .") or fallback
    stem = stem[:max_length].rstrip(" .") or fallback
    return f"_{stem}" if is_windows_reserved_stem(stem) else stem


def stable_ascii_component(
    value: object,
    *,
    fallback: str,
    digest_threshold: int,
    max_length: int,
) -> str:
    raw = str(value or fallback)
    readable = _SAFE_ASCII_COMPONENT.sub("_", raw) or fallback
    needs_digest = (
        readable != raw
        or len(readable) > digest_threshold
        or raw != raw.casefold()
        or is_windows_reserved_stem(readable)
        or readable in {".", ".."}
        or readable.rstrip(" .") != readable
    )
    if not needs_digest:
        return readable
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    prefix_length = max(1, max_length - len(digest) - 1)
    prefix = readable[:prefix_length].rstrip(" .") or fallback
    return f"{prefix}-{digest}"
